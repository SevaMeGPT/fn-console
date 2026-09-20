"""FN Console — free-hosted chat + coding agent + encrypted session archive.

Tabs: CHAT (casual) | CODE (agentic: files + sandbox commands) | ADMIN
(encrypted, password-gated log of every exchange, compressed storage,
downloadable). Providers: OneProvider key + OpenRouter :free models via env.

Env: APP_PASSWORD (access), ADMIN_PASSWORD (log viewer, separate),
ONEPROVIDER_KEY, OPENROUTER_KEY (optional), PORT (default 7860).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOCK_FILE = Path(__file__).parent / ".fn_console.lock"


def _acquire_lock(lock: Path) -> bool:
    """Single instance: O_EXCL create; a held lock older than 45s (dead
    process — live instances touch it every 20s) is broken automatically."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - lock.stat().st_mtime > 45:
                lock.unlink(missing_ok=True)
                continue
            return False
    return False


def _lock_heartbeat(lock: Path, stop: threading.Event):
    while not stop.wait(20):
        try:
            lock.touch()
        except Exception:
            return


PORT = int(os.environ.get("PORT", 7860))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin-change-me")
STATIC_API_KEY = os.environ.get("FN_STATIC_KEY", "")
OP_KEY = os.environ.get("ONEPROVIDER_KEY", "")
OR_KEY = os.environ.get("OPENROUTER_KEY", "")
WORKSPACE = Path(os.environ.get("WORKSPACE", "/app/workspace"))
ARCHIVE = Path(os.environ.get("ARCHIVE_DIR", str(Path(__file__).parent / "archive")))

# .env beside this file: APP_PASSWORD / ADMIN_PASSWORD / ONEPROVIDER_KEY /
# OPENROUTER_KEY / PORT — overrides defaults, works local and hosted
_envf = Path(__file__).parent / ".env"
if _envf.exists():
    for line in _envf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    PORT = int(os.environ.get("PORT", 7860))
    APP_PASSWORD = os.environ.get("APP_PASSWORD", APP_PASSWORD)
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", ADMIN_PASSWORD)
    OP_KEY = os.environ.get("ONEPROVIDER_KEY", OP_KEY)
    OR_KEY = os.environ.get("OPENROUTER_KEY", OR_KEY)
    STATIC_API_KEY = os.environ.get("FN_STATIC_KEY", STATIC_API_KEY)
SALT = b"fn-console-salt-v1"

TOKENS: dict[str, float] = {}                       # access tokens -> expiry
ADMIN_OK: dict[str, float] = {}                     # admin tokens -> expiry
MAX_TOKENS_REPLY = 2000
MAX_CMD_SECONDS = 60
MAX_OUT = 6000

PROVIDERS = {
    "oneprovider": {"key": OP_KEY, "base": "https://api.oneprovider.dev",
                    "style": "anthropic",
                    "models": ["claude-sonnet-5", "claude-opus-4-8"]},
    "openrouter": {"key": OR_KEY, "base": "https://openrouter.ai/api",
                   "style": "openai",
                   "models": ["deepseek/deepseek-chat:free",
                              "meta-llama/llama-3.3-70b-instruct:free"]},
}
ALL_MODELS = [f"{p}:{m}" for p, pv in PROVIDERS.items() if pv["key"]
              for m in pv["models"]]

TOOLS = [
    {"name": "list_dir", "description": "List files in a directory.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "read_file", "description": "Read a text file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write/create a text file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "run_cmd", "description": "Run a shell command in the workspace "
                                       "sandbox and get its output.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
]

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport
content="width=device-width,initial-scale=1"><title>FN Console</title><style>
body{font-family:system-ui;margin:0;background:#0d1117;color:#e6edf3}
header{display:flex;background:#010409;padding:0 12px;border-bottom:1px solid #21262d;align-items:center}
header .t{color:#58a6ff;font-weight:600;margin-right:14px;font-size:15px}
header button{background:none;border:0;color:#8b949e;padding:14px 18px;font-size:14px;cursor:pointer}
header button.on{color:#fff;border-bottom:2px solid #2f81f7}
.wrap{max-width:860px;margin:0 auto;padding:14px}
.msg{margin:8px 0;padding:10px 14px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.you{background:#121d2f;margin-left:12%}.bot{background:#122117}
.step{background:#161b22;border-left:3px solid #1f6feb;padding:6px 10px;margin:6px 0;
font-family:ui-monospace,monospace;font-size:12px;color:#9fb3c8;white-space:pre-wrap;word-break:break-word}
.row{display:flex;gap:8px;position:sticky;bottom:0;background:#0d1117;padding:10px 0}
#in,#task{flex:1;padding:11px;font-size:15px;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:8px}
button.go{padding:10px 18px;border-radius:8px;border:0;background:#238636;color:#fff;cursor:pointer;font-size:15px}
select,input{padding:9px;background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:8px}
#spin{display:none;color:#2f81f7;padding:6px}
.login{max-width:360px;margin:80px auto;text-align:center}
.login input{width:80%;margin:8px 0}
.hide{display:none}
pre{white-space:pre-wrap;word-break:break-word}
</style></head><body>
<div id=gate class=login><h2>FN Console</h2>
<input id=pw type=password placeholder="access password"><br>
<button class=go onclick=login()>Enter</button><div id=lerr style=color:#f85149></div></div>
<div id=app class=hide><header><span class=t>FN Console</span>
<button class=on id=tb-chat onclick="tab('chat',this)">Chat</button>
<button id=tb-code onclick="tab('code',this)">Code Agent</button>
<button id=tb-admin onclick="tab('admin',this)">Admin</button>
<span id=cd style="margin-left:auto;font-size:12px;color:#8b949e;white-space:nowrap"></span>
<select id=m style="margin-left:12px"></select></header>
<div class=wrap id=p-chat><div id=log></div><div id=spin style=display:none>working…</div>
<div class=row><input id=in placeholder="ask anything…"><button class=go onclick=send()>Send</button></div></div>
<div id=p-code class=hide><div class=row style=margin-bottom:8px>working dir:
<input id=cwd value="/app/workspace"></div>
<div id=clog></div><div id=cspin style=display:none>agent working…</div>
<div class=row><input id=task placeholder="describe the coding task…"><button class=go onclick=code()>Run</button></div></div>
<div id=p-admin class=hide>
<div id=alock><h3>Admin area</h3>
<div class=row><input id=apw0 type=password placeholder="admin password"><button class=go onclick=admUnlock()>Unlock</button></div>
<p id=aerr style=color:#f85149;font-size:12px></p>
<p style=color:#8b949e;font-size:12px>The session archive is hidden until the admin
password is validated by the server.</p></div>
<div id=apanel class=hide><h3>Session archive</h3>
<div class=row><input id=apw type=password placeholder="admin password"><button class=go onclick=decryptLog()>Decrypt today</button>
<button class=go onclick="downloadGz()">Download .gz</button></div>
<pre id=alog style="background:#010409;padding:12px;max-height:60vh;overflow:auto">—</pre>
<p style=color:#8b949e;font-size:12px>Every chat and coding session is logged here,
encrypted (counter-mode stream cipher, key derived from the admin password via
PBKDF2-200k) and gzip-compressed. Download gives the raw encrypted file.</p></div></div>
</div>
<div id=reauth style="display:none;position:fixed;inset:0;background:rgba(1,4,9,.75);
align-items:center;justify-content:center;z-index:9">
<div style="background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:24px;min-width:320px">
<h3 style=margin-top:0>Session expired</h3>
<p style=color:#8b949e;font-size:12px>Your session timed out. Enter the access
password to continue — your chat history is preserved.</p>
<div class=row><input id=rpw type=password placeholder="access password"
onkeydown="if(event.key==='Enter')continueSess()"><button class=go onclick=continueSess()>Continue</button></div>
<p id=rerr style=color:#f85149;font-size:12px></p>
</div></div>
<script>
let TAB="chat", SESS=null;
function esc(s){const d=document.createElement("div");d.textContent=s??"";return d.innerHTML}
function tab(t,b){TAB=t;
document.querySelectorAll("header button[id^=tb]").forEach(x=>x.classList.remove("on"));
b.classList.add("on");
for(const id of ["p-chat","p-code","p-admin"])document.getElementById(id).classList.add("hide");
document.getElementById("p-"+t).classList.remove("hide")}
function saveSess(){try{SESS?sessionStorage.setItem("fn_sess",JSON.stringify(SESS))
:sessionStorage.removeItem("fn_sess")}catch(e){}}
function startCountdown(){if(window._cdT)clearInterval(window._cdT);
const el=document.getElementById("cd");
const paint=()=>{if(!SESS){el.textContent="";return}
const left=Math.max(0,SESS.exp-Date.now()/1000);
const h=String(Math.floor(left/3600)).padStart(2,"0"),
m=String(Math.floor(left%3600/60)).padStart(2,"0"),
s=String(Math.floor(left%60)).padStart(2,"0");
el.textContent="session "+h+":"+m+":"+s;
el.style.color=left<300?"#f85149":(left<1800?"#d29922":"#8b949e");
if(left<=0)showReauth()};
paint();window._cdT=setInterval(paint,1000)}
function showReauth(){const o=document.getElementById("reauth");
if(o.style.display!=="flex"){o.style.display="flex";
const r=document.getElementById("rpw");r.value="";r.focus()}}
async function continueSess(){const pw=document.getElementById("rpw").value;
const r=await fetch("/login",{method:"POST",headers:{"content-type":"application/json"},
body:JSON.stringify({pw})});
if(r.ok){const d=await r.json();SESS={tok:d.token,exp:d.expires};saveSess();
document.getElementById("reauth").style.display="none";
document.getElementById("rerr").textContent="";loadModels();startCountdown()}
else document.getElementById("rerr").textContent="wrong password"}
async function login(){const pw=document.getElementById("pw").value;
const r=await fetch("/login",{method:"POST",headers:{"content-type":"application/json"},
body:JSON.stringify({pw})});
if(r.ok){const d=await r.json();SESS={tok:d.token,exp:d.expires};saveSess();
document.getElementById("gate").classList.add("hide");
document.getElementById("app").classList.remove("hide");
loadModels();startCountdown()}
else document.getElementById("lerr").textContent="wrong password"}
async function loadModels(){const r=await fetch("/models",{method:"POST",
headers:{"content-type":"application/json","x-fn-token":(SESS?SESS.tok:"")},
body:"{}"});
if(r.status===401){showReauth();return}
if(!r.ok)return;const ms=(await r.json()).models;
const sel=document.getElementById("m");sel.innerHTML="";
for(const m of ms){const o=document.createElement("option");o.value=m.id;
o.textContent=m.label;sel.appendChild(o)}}
async function post(path,body){const r=await fetch(path,{method:"POST",
headers:{"content-type":"application/json","x-fn-token":(SESS?SESS.tok:"")},
body:JSON.stringify(body)});
if(r.status===401){showReauth();throw 0}return r.json()}
async function send(){const i=document.getElementById("in");const t=i.value.trim();
if(!t)return;i.value="";
const log=document.getElementById("log");
log.insertAdjacentHTML("beforeend",`<div class="msg you">${esc(t)}</div>`);
const sp=document.getElementById("spin");sp.style.display="block";
const d=await post("/task",{tab:"chat",model:document.getElementById("m").value,message:t});
sp.style.display="none";
log.insertAdjacentHTML("beforeend",`<div class="msg bot">${esc(d.reply||d.error||"(empty)")}</div>`)}
async function code(){const i=document.getElementById("task");const t=i.value.trim();
if(!t)return;i.value="";const cl=document.getElementById("clog");
const sp=document.getElementById("cspin");sp.style.display="block";
const d=await post("/task",{tab:"code",model:document.getElementById("m").value,
message:t,cwd:document.getElementById("cwd").value});
sp.style.display="none";
for(const s of (d.steps||[]))cl.insertAdjacentHTML("beforeend",`<div class="step">${esc(s)}</div>`);
cl.insertAdjacentHTML("beforeend",`<div class="msg bot">${esc(d.reply||d.error||"")}</div>`)}
async function admUnlock(){const pw=document.getElementById("apw0").value;
const r=await fetch("/admin/verify",{method:"POST",
headers:{"content-type":"application/json"},body:JSON.stringify({pw})});
if(r.ok){document.getElementById("apw").value=pw;
document.getElementById("alock").classList.add("hide");
document.getElementById("apanel").classList.remove("hide")}
else document.getElementById("aerr").textContent="wrong admin password"}
async function decryptLog(){const pw=document.getElementById("apw").value;
const r=await fetch("/admin/log",{method:"POST",
headers:{"content-type":"application/json"},
body:JSON.stringify({pw})});
if(!r.ok){alert("wrong admin password");return}
const text=await r.text();
document.getElementById("alog").textContent=text||"(empty)"}
async function downloadGz(){const pw=document.getElementById("apw").value;
const r=await fetch("/admin/log/download",{method:"POST",
headers:{"content-type":"application/json"},
body:JSON.stringify({pw})});
if(!r.ok){alert("wrong admin password");return}
const b=await r.blob();const a=document.createElement("a");
a.href=URL.createObjectURL(b);
a.download="fn_console_log_"+new Date().toISOString().slice(0,10)+".jsonl.enc.gz";
a.click();URL.revokeObjectURL(a.href)}
(function(){try{const s=JSON.parse(sessionStorage.getItem("fn_sess")||"null");
if(s&&s.exp>Date.now()/1000){SESS=s;
document.getElementById("gate").classList.add("hide");
document.getElementById("app").classList.remove("hide");
loadModels();startCountdown()}
else if(s){SESS=null;saveSess()}}catch(e){}})();
</script></body></html>"""


def _keystream(key: bytes, length: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < length:
        out += hashlib.sha256(key + ctr.to_bytes(8, "big")).digest()
        ctr += 1
    return bytes(out[:length])


def _xcrypt(data: bytes, key: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, _keystream(key, len(data))))




def upstream_call(provider: str, model: str, messages: list, tools=None) -> dict:
    p = PROVIDERS[provider]
    key = p["key"]
    if not key:
        raise RuntimeError(f"no API key configured for {provider}")
    body: dict = {"model": model, "max_tokens": MAX_TOKENS_REPLY,
                  "messages": messages}
    if tools:
        if p["style"] == "anthropic":
            body["tools"] = tools
        else:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
            body["tool_choice"] = "auto"
    url = (p["base"] + ("/v1/messages" if p["style"] == "anthropic"
                        else "/v1/chat/completions"))
    headers = {"content-type": "application/json"}
    if p["style"] == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["authorization"] = f"Bearer {key}"
    rq = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                headers=headers)
    with urllib.request.urlopen(rq, timeout=180) as r:
        data = json.loads(r.read().decode())
    if p["style"] == "anthropic":
        return data
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content: list = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        content.append({"type": "tool_use", "id": tc.get("id", ""),
                        "name": fn.get("name"),
                        "input": json.loads(fn.get("arguments") or "{}")})
    return {"content": content,
            "stop_reason": "tool_use" if msg.get("tool_calls") else "end_turn"}


def resolve(cwd: str, path: str):
    p = Path(path)
    return p if p.is_absolute() else Path(cwd) / p


def run_tool(name: str, inp: dict, cwd: str) -> str:
    try:
        if name == "list_dir":
            t = resolve(cwd, inp.get("path", "."))
            return "\n".join(f"{'D' if x.is_dir() else 'F'} {x.name}"
                             for x in sorted(t.iterdir())[:200]) or "(empty)"
        if name == "read_file":
            txt = resolve(cwd, inp["path"]).read_text(encoding="utf-8",
                                                      errors="replace")
            return txt[:MAX_OUT]
        if name == "write_file":
            f = resolve(cwd, inp["path"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(inp["content"], encoding="utf-8")
            return f"written {len(inp['content'])} chars -> {f}"
        if name == "run_cmd":
            r = subprocess.run(inp["command"], shell=True, capture_output=True,
                               text=True, timeout=MAX_CMD_SECONDS, cwd=cwd)
            out = ((r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
                   )[:MAX_OUT]
            return f"{out}\n[exit {r.returncode}]"
        return f"unknown tool {name}"
    except Exception as e:  # noqa: BLE001
        return f"TOOL ERROR: {type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj=None, html: str | None = None,
              headers: dict | None = None):
        if html is not None:
            body = html.encode()
            ctype = "text/html; charset=utf-8"
        else:
            body = json.dumps(obj).encode()
            ctype = "application/json"
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, html=PAGE)
        if self.path == "/models":
            models = [{"id": f"{p}:{m}", "label": f"{m} · {p}"}
                      for p, pv in PROVIDERS.items() if pv["key"]
                      for m in pv["models"]]
            return self._send(200, {"models": models})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n)) if n else {}

        if self.path == "/login":
            if req.get("pw") == APP_PASSWORD:
                tok = secrets.token_hex(16)
                TOKENS[tok] = time.time() + 12 * 3600
                for k in [k for k, v in TOKENS.items() if v < time.time()]:
                    TOKENS.pop(k)
                return self._send(200, {"token": tok,
                                        "expires": TOKENS[tok]})
            return self._send(401, {"error": "wrong password"})

        if self.headers.get("x-api-key", "") == STATIC_API_KEY:
            tok = "*"  # machine-to-machine: static key bypasses sessions
        else:
            tok = self.headers.get("x-fn-token", req.get("token", ""))
            if TOKENS.get(tok, 0) < time.time():
                return self._send(401, {"error": "session expired — re-login"})

        if self.path == "/models":
            models = [{"id": f"{p}:{m}", "label": f"{m} · {p}"}
                      for p, pv in PROVIDERS.items() if pv["key"]
                      for m in pv["models"]]
            return self._send(200, {"models": models})

        if self.path == "/task":
            tab = req.get("tab", "chat")
            model_id = req.get("model", "")
            provider, _, model = model_id.partition(":")
            message = req.get("message", "")
            t0 = time.time()
            try:
                if tab == "chat":
                    reply = self._chat(provider, model, message)
                    steps = []
                else:
                    reply, steps = self._code(provider, model, message,
                                              req.get("cwd", str(WORKSPACE)))
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            self._archive({"kind": tab, "provider": provider, "model": model,
                           "request": message, "reply": reply,
                           "steps": steps, "secs": round(time.time() - t0, 1)})
            return self._send(200, {"reply": reply, "steps": steps})

        if self.path == "/admin/verify":
            if req.get("pw") != ADMIN_PASSWORD:
                return self._send(401, {"error": "wrong admin password"})
            tok = secrets.token_hex(16)
            ADMIN_OK[tok] = time.time() + 3600
            return self._send(200, {"ok": True, "admin_token": tok})

        if self.path == "/admin/log":
            if req.get("pw") != ADMIN_PASSWORD:
                return self._send(401, {"error": "wrong admin password"})
            key = hashlib.pbkdf2_hmac("sha256", ADMIN_PASSWORD.encode(), SALT,
                                      200_000, dklen=32)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            f = ARCHIVE / f"{day}.jsonl.enc.gz"
            if not f.exists():
                return self._send(200, {"log": "(no sessions yet today)"})
            raw = gzip.decompress(_xcrypt(f.read_bytes(), key))
            records = json.loads(raw)
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
            return self._send(200, {"day": day, "log": text})

        if self.path == "/admin/log/download":
            if req.get("pw") != ADMIN_PASSWORD:
                return self._send(401, {"error": "wrong admin password"})
            day = time.strftime("%Y-%m-%d", time.gmtime())
            f = ARCHIVE / f"{day}.jsonl.enc.gz"
            if not f.exists():
                return self._send(404, {"error": "no archive for today"})
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("content-length", str(len(body)))
            self.send_header("content-disposition",
                             f'attachment; filename="{day}.fnlog.gz"')
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send(404, {"error": "not found"})

    def _chat(self, provider: str, model: str, message: str) -> str:
        data = upstream_call(provider, model,
                             [{"role": "user", "content": message}])
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text") or "(empty reply)"

    def _code(self, provider: str, model: str, task: str,
              cwd: str = "") -> tuple[str, list]:
        cwd = cwd or str(WORKSPACE)
        Path(cwd).mkdir(parents=True, exist_ok=True)
        messages = [{"role": "user", "content":
                     f"Working directory: {cwd}\n\nTask: {task}\n\n"
                     "Use the tools to complete the task. Verify your work by "
                     "running it when possible. Then summarize briefly."}]
        steps, final = [], None
        for _ in range(20):
            data = upstream_call(provider, model, messages, tools=TOOLS)
            blocks = data.get("content", [])
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text")
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if text:
                steps.append(text)
            if not tool_uses:
                final = text or "(done)"
                break
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for tu in tool_uses:
                out = run_tool(tu["name"], tu.get("input", {}), cwd)
                steps.append(f"→ {tu['name']} {json.dumps(tu.get('input', {}))[:120]}"
                             f"\n  {str(out)[:400]}")
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": str(out)[:MAX_OUT]})
            messages.append({"role": "user", "content": results})
        return final or "max steps reached", steps

    def _archive(self, record: dict):
        key = hashlib.pbkdf2_hmac("sha256", ADMIN_PASSWORD.encode(), SALT,
                                  200_000, dklen=32)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        f = ARCHIVE / f"{day}.jsonl.enc.gz"
        records = []
        if f.exists():
            try:
                raw = gzip.decompress(_xcrypt(f.read_bytes(), key))
                records = json.loads(raw)
            except Exception:
                records = []
        records.append({"ts": time.strftime("%H:%M:%S", time.gmtime()),
                        **{k: v for k, v in record.items()}})
        blob = gzip.compress(json.dumps(records, indent=1).encode(), 6)
        f.write_bytes(_xcrypt(blob, key))


if __name__ == "__main__":
    if not _acquire_lock(LOCK_FILE):
        print("another console instance holds the lock — exiting")
        raise SystemExit(0)
    stop = threading.Event()
    threading.Thread(target=_lock_heartbeat, args=(LOCK_FILE, stop),
                     daemon=True).start()
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    class Server(HTTPServer):
        allow_reuse_address = False   # Windows: blocks silent double-binds
    srv = Server(("0.0.0.0", PORT), Handler)
    print(f"FN Console → :{PORT}")
    srv.serve_forever()
