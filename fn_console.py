"""SevaMeGPT — free-hosted multi-user chat + coding agent (ex FN Console).

Tabs: CHAT (SevaMeGPT persona: Memon-Mumbai hinglish humour, switches to
proper English on request) | CODE (agentic: files + sandbox commands) |
ADMIN (archive + user management + global token budget).
Users: users.json (seeded admin "seva"/APP_PASSWORD). Non-admin users get a
global token budget: limit tokens per window_h hours (admin sets both).
Providers: OneProvider + OpenRouter :free models (via env).

Env: APP_PASSWORD (admin seed password), ADMIN_PASSWORD (archive decrypt),
ONEPROVIDER_KEY, OPENROUTER_KEY (optional, enables free models),
FN_STATIC_KEY (machine auth), PORT (default 7860).
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
import urllib.error
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
DATA = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data_console")))

# .env beside this file overrides defaults, works local and hosted
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
DATA.mkdir(parents=True, exist_ok=True)
USERS_F = DATA / "users.json"
USAGE_F = DATA / "usage.json"

TOKENS: dict[str, dict] = {}            # token -> {exp, user, role}
ADMIN_OK: dict[str, float] = {}         # admin-verified tokens -> expiry
MAX_TOKENS_REPLY = 2000
MAX_CMD_SECONDS = 60
MAX_OUT = 6000

PERSONA = (
    "You are SevaMeGPT, a witty assistant with Memon-Muslim Mumbai humour. "
    "Default voice: warm, playful roman-Hinglish Mumbai slang — bhai, ekdum, "
    "bindaas, chal, sahi hai, mazaa aagaya — with light Memon-style teasing "
    "humour. Keep it friendly and understandable, never rude or offensive. "
    "If the user asks for proper English (or the task is formal), switch to "
    "clean professional English. Code, math and technical output always in "
    "plain English. Keep answers tight unless asked to elaborate.")


def _h(pw: str) -> str:
    return hashlib.sha256(SALT + pw.encode()).hexdigest()


def _load_users() -> dict:
    if USERS_F.exists():
        return json.loads(USERS_F.read_text())
    d = {"users": [{"u": "seva", "h": _h(APP_PASSWORD), "role": "admin"}],
         "limit": 10000, "window_h": 24}
    _save_users(d)
    return d


def _save_users(d: dict):
    tmp = USERS_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(USERS_F)


def _load_usage() -> dict:
    if USAGE_F.exists():
        return json.loads(USAGE_F.read_text())
    return {}


def _save_usage(d: dict):
    tmp = USAGE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(USAGE_F)


USERS = _load_users()          # {"users": [...], "limit": int, "window_h": int}
USAGE: dict = _load_usage()    # user -> {"tokens": n, "win": epoch_start}

# Render storage is ephemeral: accounts created via the panel die on redeploys.
# SEED_USERS env ("raju:pw1,ali:pw2") re-creates them at every boot.
_seed = os.environ.get("SEED_USERS", "")
if _seed:
    _added = False
    for pair in _seed.split(","):
        _u, _, _pw = pair.partition(":")
        _u = _u.strip()
        if _u and _pw and not any(x["u"] == _u for x in USERS["users"]):
            USERS["users"].append({"u": _u, "h": _h(_pw), "role": "user"})
            _added = True
    if _added:
        _save_users(USERS)


def _usage_check(user: str, role: str) -> str | None:
    """Return a rejection reason if user is over budget, else None."""
    if role == "admin":
        return None
    limit, wh = int(USERS.get("limit", 10000)), float(USERS.get("window_h", 24))
    rec = USAGE.get(user)
    now = time.time()
    if not rec or now - rec.get("win", 0) > wh * 3600:
        return None                                   # fresh window on use
    if rec.get("tokens", 0) >= limit:
        left_h = (wh - (now - rec["win"]) / 3600)
        return (f"token budget khatam, bhai! {limit:,} tokens used in this "
                f"{wh:g}h window — try again in {max(left_h, 0.1):.1f}h "
                f"(admin can raise the limit)")
    return None


def _usage_record(user: str, role: str, tokens: int):
    if role == "admin":
        return
    rec = USAGE.get(user)
    now = time.time()
    wh = float(USERS.get("window_h", 24))
    if not rec or now - rec.get("win", 0) > wh * 3600:
        USAGE[user] = {"tokens": tokens, "win": now}
    else:
        rec["tokens"] = rec.get("tokens", 0) + tokens
    _save_usage(USAGE)


PROVIDERS = {
    "oneprovider": {"key": OP_KEY, "base": "https://api.oneprovider.dev",
                    "style": "anthropic",
                    "models": ["claude-sonnet-5", "claude-opus-4-8"]},
    "openrouter": {"key": OR_KEY, "base": "https://openrouter.ai/api",
                   "style": "openai",
                   "models": ["deepseek/deepseek-chat-v3.1:free",
                              "meta-llama/llama-3.3-70b-instruct:free",
                              "qwen/qwen3-coder:free",
                              "google/gemma-3-27b-it:free",
                              "mistralai/mistral-small-3.2-24b-instruct:free"]},
}


def _refresh_openrouter():
    """Swap the curated :free list for OpenRouter's live free catalogue."""
    if not OR_KEY:
        return
    try:
        rq = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"authorization": f"Bearer {OR_KEY}"})
        with urllib.request.urlopen(rq, timeout=20) as r:
            data = json.loads(r.read().decode())
        free = [m["id"] for m in data.get("data", [])
                if str(m.get("id", "")).endswith(":free")]
        if free:
            PROVIDERS["openrouter"]["models"] = sorted(free)[:30]
    except Exception:  # noqa: BLE001
        pass                                    # curated list stays


threading.Thread(target=_refresh_openrouter, daemon=True).start()


def _model_label(pid: str, mid: str) -> str:
    """Dropdown label: short name · free/provider · code/chat."""
    short = mid.split("/")[-1].replace(":free", "").replace("-", " ").strip()
    kind = "code" if any(k in mid.lower() for k in ("coder", "code", "dev")) else "chat"
    tag = "free" if (":free" in mid or mid.endswith("-free")) else f"{pid} (Limited use)"
    return f"{short} · {tag} · {kind}"


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

MASCOT = """<svg id="pehlwan" viewBox="0 0 240 240" xmlns="http://www.w3.org/2000/svg">
<style>#pehlwan #head{transform-box:fill-box;transform-origin:50% 78%;
transition:transform .35s cubic-bezier(.34,1.56,.64,1);
animation:idle 3.4s ease-in-out infinite}
#pehlwan #beard{transform-box:fill-box;transform-origin:50% 4%}
#pehlwan.swing #beard{animation:bw .75s cubic-bezier(.36,.07,.19,.97)}
#pehlwan #eyes{transform-box:fill-box;transform-origin:center;animation:blink 4.2s infinite}
#pehlwan #pupils{transition:transform .18s ease-out}
@keyframes bw{0%,100%{transform:rotate(0)}22%{transform:rotate(12deg)}
48%{transform:rotate(-9deg)}72%{transform:rotate(5deg)}}
@keyframes idle{0%,100%{transform:translateY(0)}50%{transform:translateY(3px) rotate(-1.4deg)}}
@keyframes blink{0%,91%,100%{transform:scaleY(1)}94%{transform:scaleY(.12)}}
</style>
<g id="body">
<path d="M48 240 C48 190 76 168 120 168 C164 168 192 190 192 240 Z" fill="#f2e9d8"/>
<path d="M48 240 C48 190 76 168 120 168 L120 240 Z" fill="#e6dac2"/>
<path d="M104 168 L120 186 L136 168" fill="none" stroke="#c9b892"
stroke-width="4" stroke-linecap="round"/>
</g>
<g id="head">
<ellipse cx="120" cy="100" rx="46" ry="48" fill="#e0a370"/>


<g id="eyes">
<ellipse cx="102" cy="90" rx="8" ry="9" fill="#fff"/>
<ellipse cx="138" cy="90" rx="8" ry="9" fill="#fff"/>
<g id="pupils">
<circle cx="103.5" cy="91.5" r="4.4" fill="#141414"/>
<circle cx="139.5" cy="91.5" r="4.4" fill="#141414"/>
<circle cx="105" cy="90" r="1.5" fill="#fff"/>
<circle cx="141" cy="90" r="1.5" fill="#fff"/>
</g>
</g>
<g id="beard">
<path d="M73 110 C71 124 71 138 75 152 C79 178 90 204 104 216
C110 222 116 224 120 224 C124 224 130 222 136 216 C150 204 161 178 165 152
C169 138 169 124 167 110 C165 117 157 123 147 125 C139 132 131 135 120 135
C109 135 101 132 93 125 C83 123 75 117 73 110 Z" fill="#4a2c17"/>
<path d="M81 126 C81 148 88 174 100 194" stroke="#5d3a22" stroke-width="4.5"
fill="none" stroke-linecap="round" opacity=".9"/>
<path d="M97 132 C96 152 100 178 110 200" stroke="#6b4426" stroke-width="3"
fill="none" stroke-linecap="round" opacity=".8"/>
<path d="M159 126 C159 148 152 174 140 194" stroke="#5d3a22" stroke-width="4.5"
fill="none" stroke-linecap="round" opacity=".9"/>
<path d="M143 132 C144 152 140 178 130 200" stroke="#6b4426" stroke-width="3"
fill="none" stroke-linecap="round" opacity=".8"/>
<path d="M120 136 L120 206" stroke="#5d3a22" stroke-width="3" fill="none"
stroke-linecap="round" opacity=".5"/>
</g>
<g id="safa">
<path d="M62 58 C56 12 184 12 178 58 C178 70 164 77 120 77 C76 77 62 70 62 58 Z"
fill="#ff9950"/>
<path d="M62 56 C68 30 92 20 120 20 C148 20 172 30 178 56" fill="#ffb26e"/>
<path d="M148 22 C160 14 176 20 178 34 C179 44 172 50 166 48 C158 42 152 32 148 22 Z"
fill="#ff9950"/>
<path d="M148 22 C160 14 176 20 178 34" fill="none" stroke="#e2702c"
stroke-width="3" stroke-linecap="round"/>
<path d="M120 20 L120 62" stroke="#e2702c" stroke-width="3.5" stroke-linecap="round"
opacity=".45"/>
<path d="M88 24 C80 36 76 46 77 58" stroke="#e2702c" stroke-width="3.5"
stroke-linecap="round" opacity=".45" fill="none"/>
<rect x="74" y="52" width="92" height="11" rx="5.5" fill="#f6c453"/>
<circle cx="120" cy="57.5" r="7" fill="#f6c453"/>
<circle cx="120" cy="57.5" r="3.4" fill="#d64545"/>
<circle cx="118.6" cy="56.1" r="1.1" fill="#fff"/>
</g>
</g></svg>"""

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport
content="width=device-width,initial-scale=1"><title>SevaMeGPT</title><style>
:root{--bg:#0a0e14;--card:#11161f;--edge:#1f2733;--mut:#8b98a9;--acc:#4f8ff7;--grn:#2ea653}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto;margin:0;background:
radial-gradient(1200px 700px at 70% -10%,#12203a 0%,var(--bg) 55%);color:#e8eef6;min-height:100vh}
header{display:flex;background:rgba(4,8,14,.85);backdrop-filter:blur(8px);padding:0 12px;
border-bottom:1px solid var(--edge);align-items:center;position:sticky;top:0;z-index:5}
header .t{color:var(--acc);font-weight:700;margin-right:10px;font-size:15px;letter-spacing:.3px;
display:flex;align-items:center;gap:7px}
header .t svg{width:26px;height:27px}
header button{background:none;border:0;color:var(--mut);padding:14px 14px;font-size:14px;cursor:pointer}
header button.on{color:#fff;border-bottom:2px solid var(--acc)}
#cd{font-size:11.5px;color:var(--mut);white-space:nowrap;font-variant-numeric:tabular-nums}
.wrap{max-width:880px;margin:0 auto;padding:14px}
.msg{margin:8px 0;padding:10px 14px;border-radius:12px;white-space:pre-wrap;word-break:break-word;
line-height:1.45}
.you{background:#14243d;margin-left:10%}
.bot{background:#12241a}
.step{background:#141a24;border-left:3px solid var(--acc);padding:6px 10px;margin:6px 0;
font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;color:#9fb3c8;
white-space:pre-wrap;word-break:break-word;border-radius:0 8px 8px 0}
.row{display:flex;gap:8px;position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 30%);
padding:10px 0}
input,select{padding:11px;background:var(--card);color:#e8eef6;border:1px solid #2a3646;
border-radius:10px;font-size:15px;outline:none}
input:focus,select:focus{border-color:var(--acc)}
#in,#task,#rpw{flex:1}
button.go{padding:10px 18px;border-radius:10px;border:0;cursor:pointer;font-size:15px;color:#fff;
background:linear-gradient(135deg,#2ea653,#238636)}
button.go.bl{background:linear-gradient(135deg,#3a6fd8,#2f5cc0)}
.spin{display:none;color:var(--acc);padding:6px;font-size:13px}
.hide{display:none!important}
pre{white-space:pre-wrap;word-break:break-word}
.card{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:18px}
.gate{max-width:380px;margin:6vh auto 0;text-align:center}
.gate svg{width:150px;height:158px;filter:drop-shadow(0 6px 22px rgba(255,140,66,.25))}
.gate h1{font-size:24px;margin:6px 0 2px}
.gate p{color:var(--mut);font-size:13px;margin:0 0 14px}
.gate input{width:100%;margin:7px 0;text-align:center}
.gate button.go{width:100%;margin-top:10px;padding:12px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td,th{padding:7px 8px;border-bottom:1px solid var(--edge);text-align:left}
h3{margin:2px 0 12px}
.hint{color:var(--mut);font-size:12px;line-height:1.5}
@media(max-width:640px){.you{margin-left:4%}.wrap{padding:10px}
header button{padding:12px 9px;font-size:13px}#cd{display:none}}
</style></head><body>
<div id=gate class=gate>__MASCOT__
<h1>SevaMeGPT</h1><p>bindaas chats • code karo • mast</p>
<input id=u placeholder="username" autocomplete=username>
<input id=pw type=password placeholder="password" onkeydown="if(event.key==='Enter')login()">
<button class=go onclick=login()>Enter</button><div id=lerr style=color:#f85149;font-size:13px></div>
</div>
<div id=app class=hide><header><span class=t><svg viewBox="0 0 200 210" width=26 height=27>
<path d="M55 200 C55 158 75 142 100 142 C125 142 145 158 145 200 Z" fill="#1f6feb"/>
<ellipse cx="100" cy="88" rx="30" ry="31" fill="#d99b66"/>
<path d="M72 84 C70 106 78 126 100 132 C122 126 130 106 128 84
C120 78 80 78 72 84 Z" fill="#2e1a10"/>
<circle cx="89" cy="76" r="4" fill="#141414"/><circle cx="111" cy="76" r="4" fill="#141414"/>
<path d="M66 58 C66 30 134 30 134 58 C134 66 66 66 66 58 Z" fill="#ff8c42"/>
<rect x="74" y="52" width="52" height="7" rx="3.5" fill="#f6c453"/>
</svg>
SevaMeGPT</span>
<button class=on id=tb-chat onclick="tab('chat',this)">Chat</button>
<button id=tb-code onclick="tab('code',this)">Code Agent</button>
<button id=tb-admin onclick="tab('admin',this)">Admin</button>
<span id=cd style="margin-left:auto"></span>
<select id=m style="margin-left:12px;max-width:34vw"></select></header>
<div class=wrap id=p-chat><div id=log></div><div id=spin class=spin>seva soch raha hai…</div>
<div class=row><input id=in placeholder="ask anything…"><button class=go onclick=send()>Send</button></div></div>
<div id=p-code class=hide><div class=row style=margin-bottom:8px>working dir:
<input id=cwd value="/app/workspace" style=flex:1></div>
<div id=clog></div><div id=cspin class=spin>agent working…</div>
<div class=row><input id=task placeholder="describe the coding task…"><button class=go onclick=code()>Run</button></div></div>
<div id=p-admin class=hide>
<div id=alock class=card style="max-width:420px;margin:20px auto"><h3>Admin area</h3>
<div class=row><input id=apw0 type=password placeholder="admin password" style=flex:1
onkeydown="if(event.key==='Enter')admUnlock()"><button class=go onclick=admUnlock()>Unlock</button></div>
<p id=aerr style=color:#f85149;font-size:13px></p>
<p class=hint>The session archive, user management and token budget are hidden
until the admin password is validated by the server.</p></div>
<div id=apanel class=hide>
<div class=card style=margin:14px 0><h3>Session archive (today)</h3>
<div class=row><input id=apw type=password placeholder="admin password" style=flex:1>
<button class=go onclick=decryptLog()>Decrypt</button>
<button class="go bl" onclick=downloadGz()>Download .gz</button></div>
<pre id=alog style="background:#0a0e14;padding:12px;max-height:46vh;overflow:auto;border-radius:10px">—</pre>
<p class=hint>Every chat and coding session is logged here, encrypted
(counter-mode stream cipher, key derived from the admin password via PBKDF2-200k)
and gzip-compressed. Download gives the raw encrypted file.</p></div>
<div class=card style=margin:14px 0><h3>Users</h3>
<table id=utab></table>
<div class=row style=position:static;background:none;padding:8px 0 0>
<input id=nu placeholder="new username" style=flex:1><input id=npw placeholder="password" style=flex:1
onkeydown="if(event.key==='Enter')addUser()"><button class=go onclick=addUser()>Add user</button></div>
<p id=uerr style=color:#f85149;font-size:13px></p></div>
<div class=card style=margin:14px 0><h3>Token budget (non-admin users)</h3>
<div class=row style=position:static;background:none;padding:0>
<input id=lim type=number placeholder="tokens per window"><input id=wh type=number placeholder="window hours">
<button class="go bl" onclick=saveLimits()>Save</button></div>
<p class=hint>Each non-admin user can use this many upstream tokens per window.
Admins are unlimited.</p></div>
</div></div>
</div>
<div id=reauth style="display:none;position:fixed;inset:0;background:rgba(2,6,12,.78);
align-items:center;justify-content:center;z-index:9">
<div class=card style="min-width:300px;max-width:92vw;text-align:center">
<h3 style=margin-top:0>Session expired</h3>
<p class=hint>Your session timed out. Enter your password to continue —
your chat history is preserved.</p>
<div class=row style=position:static;background:none><input id=rpw type=password placeholder="password"
onkeydown="if(event.key==='Enter')continueSess()"><button class=go onclick=continueSess()>Continue</button></div>
<p id=rerr style=color:#f85149;font-size:13px></p>
</div></div>
<script>
let TAB="chat", SESS=null;
function esc(s){const d=document.createElement("div");d.textContent=s??"";return d.innerHTML}
function saveSess(){try{SESS?sessionStorage.setItem("fn_sess",JSON.stringify(SESS))
:sessionStorage.removeItem("fn_sess")}catch(e){}}
function tab(t,b){TAB=t;
document.querySelectorAll("header button[id^=tb]").forEach(x=>x.classList.remove("on"));
b.classList.add("on");
for(const id of ["p-chat","p-code","p-admin"])document.getElementById(id).classList.add("hide");
document.getElementById("p-"+t).classList.remove("hide")}
function startCountdown(){if(window._cdT)clearInterval(window._cdT);
const el=document.getElementById("cd");
const paint=()=>{if(!SESS){el.textContent="";return}
const left=Math.max(0,SESS.exp-Date.now()/1000);
const h=String(Math.floor(left/3600)).padStart(2,"0"),
m=String(Math.floor(left%3600/60)).padStart(2,"0"),
s=String(Math.floor(left%60)).padStart(2,"0");
el.textContent="session "+h+":"+m+":"+s;
el.style.color=left<300?"#f85149":(left<1800?"#d29922":"var(--mut)");
if(left<=0)showReauth()};
paint();window._cdT=setInterval(paint,1000)}
function showReauth(){const o=document.getElementById("reauth");
if(o.style.display!=="flex"){o.style.display="flex";
const r=document.getElementById("rpw");r.value="";r.focus()}}
async function continueSess(){const pw=document.getElementById("rpw").value;
const r=await fetch("/login",{method:"POST",headers:{"content-type":"application/json"},
body:JSON.stringify({u:SESS?SESS.u:"",pw})});
if(r.ok){const d=await r.json();SESS={tok:d.token,exp:d.expires,u:d.user,role:d.role};saveSess();
document.getElementById("reauth").style.display="none";
document.getElementById("rerr").textContent="";loadModels();startCountdown()}
else document.getElementById("rerr").textContent="wrong username/password"}
async function login(){const u=document.getElementById("u").value.trim();
const pw=document.getElementById("pw").value;
const r=await fetch("/login",{method:"POST",headers:{"content-type":"application/json"},
body:JSON.stringify({u,pw})});
if(r.ok){const d=await r.json();SESS={tok:d.token,exp:d.expires,u:d.user,role:d.role};saveSess();
document.getElementById("gate").classList.add("hide");
document.getElementById("app").classList.remove("hide");
if(d.role!=="admin")document.getElementById("tb-admin").style.display="none";
loadModels();startCountdown()}
else document.getElementById("lerr").textContent="wrong username/password"}
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
if(r.status===401){showReauth();throw 0}
const d=await r.json();if(r.status===429){alert(d.error||"limit reached");throw 0}return d}
async function send(){const i=document.getElementById("in");const t=i.value.trim();
if(!t)return;i.value="";
const log=document.getElementById("log");
log.insertAdjacentHTML("beforeend",`<div class="msg you">${esc(t)}</div>`);
const sp=document.getElementById("spin");sp.style.display="block";
try{const d=await post("/task",{tab:"chat",model:document.getElementById("m").value,message:t});
log.insertAdjacentHTML("beforeend",`<div class="msg bot">${esc(d.reply||d.error||"(empty)")}</div>`)}
catch(e){if(e!==0)log.insertAdjacentHTML("beforeend",
`<div class="msg bot">upar se hawa lag gayi — try again</div>`)}
sp.style.display="none"}
async function code(){const i=document.getElementById("task");const t=i.value.trim();
if(!t)return;i.value="";const cl=document.getElementById("clog");
const sp=document.getElementById("cspin");sp.style.display="block";
try{const d=await post("/task",{tab:"code",model:document.getElementById("m").value,
message:t,cwd:document.getElementById("cwd").value});
for(const s of (d.steps||[]))cl.insertAdjacentHTML("beforeend",`<div class="step">${esc(s)}</div>`);
cl.insertAdjacentHTML("beforeend",`<div class="msg bot">${esc(d.reply||d.error||"")}</div>`)}
catch(e){if(e!==0)cl.insertAdjacentHTML("beforeend",
`<div class="msg bot">agent ne haath khada kar diya — try again</div>`)}
sp.style.display="none"}
async function admUnlock(){const pw=document.getElementById("apw0").value;
const r=await fetch("/admin/verify",{method:"POST",
headers:{"content-type":"application/json","x-fn-token":(SESS?SESS.tok:"")},
body:JSON.stringify({pw})});
if(r.ok){document.getElementById("apw").value=pw;
document.getElementById("alock").classList.add("hide");
document.getElementById("apanel").classList.remove("hide");adminLoad()}
else document.getElementById("aerr").textContent="wrong admin password"}
async function adminLoad(){const d=await post("/admin/users",{});
if(!d.users)return;
const L=d.limit,W=d.window_h;
document.getElementById("lim").value=L;document.getElementById("wh").value=W;
document.getElementById("utab").innerHTML="<tr><th>user</th><th>role</th><th>used / limit</th><th></th></tr>"+
d.users.map(u=>`<tr><td>${esc(u.u)}</td><td>${esc(u.role)}</td>`+
`<td>${u.role==="admin"?"unlimited":u.used.toLocaleString()+" / "+L.toLocaleString()}</td>`+
`<td>${u.u==="seva"?"":`<button class="go bl" style="padding:4px 10px;font-size:12px"
onclick="delUser('${esc(u.u)}')">remove</button>`}</td></tr>`).join("")}
async function addUser(){const u=document.getElementById("nu").value.trim();
const pw=document.getElementById("npw").value;if(!u||!pw)return;
const d=await post("/admin/users",{action:"add",u,pw});
if(d.error){document.getElementById("uerr").textContent=d.error;return}
document.getElementById("nu").value="";document.getElementById("npw").value="";
document.getElementById("uerr").textContent="";adminLoad()}
async function delUser(u){await post("/admin/users",{action:"del",u});adminLoad()}
async function saveLimits(){const lim=parseInt(document.getElementById("lim").value)||10000;
const wh=parseFloat(document.getElementById("wh").value)||24;
await post("/admin/users",{action:"limits",limit:lim,window_h:wh});adminLoad()}
async function decryptLog(){const pw=document.getElementById("apw").value;
const r=await fetch("/admin/log",{method:"POST",
headers:{"content-type":"application/json","x-fn-token":(SESS?SESS.tok:"")},
body:JSON.stringify({pw})});
if(!r.ok){alert("wrong admin password");return}
const text=await r.text();
document.getElementById("alog").textContent=text||"(empty)"}
async function downloadGz(){const pw=document.getElementById("apw").value;
const r=await fetch("/admin/log/download",{method:"POST",
headers:{"content-type":"application/json","x-fn-token":(SESS?SESS.tok:"")},
body:JSON.stringify({pw})});
if(!r.ok){alert("wrong admin password");return}
const b=await r.blob();const a=document.createElement("a");
a.href=URL.createObjectURL(b);
a.download="SevaMeGPT_log_"+new Date().toISOString().slice(0,10)+".jsonl.enc.gz";
a.click();URL.revokeObjectURL(a.href)}
(function(){try{const s=JSON.parse(sessionStorage.getItem("fn_sess")||"null");
if(s&&s.exp>Date.now()/1000){SESS=s;
document.getElementById("gate").classList.add("hide");
document.getElementById("app").classList.remove("hide");
if(s.role!=="admin")document.getElementById("tb-admin").style.display="none";
loadModels();startCountdown()}
else if(s){SESS=null;saveSess()}}catch(e){}})();
(function(){const sv=document.getElementById("pehlwan");if(!sv)return;
const head=sv.querySelector("#head");
let raf=null;
function aim(dx,dy){const a=Math.max(-14,Math.min(14,dx*.05));
const y=Math.max(-6,Math.min(6,dy*.04));
head.style.transform=`translate(${Math.max(-5,Math.min(5,dx*.02))}px,${y}px) rotate(${a}deg)`;
const pu=document.getElementById("pupils");
if(pu)pu.style.transform=`translate(${Math.max(-3,Math.min(3,dx*.012))}px,${Math.max(-2.5,Math.min(2.5,dy*.012))}px)`}
function swing(){sv.classList.remove("swing");void sv.offsetWidth;sv.classList.add("swing")}
window.addEventListener("mousemove",e=>{if(raf)return;raf=requestAnimationFrame(()=>{
const r=sv.getBoundingClientRect();
aim(e.clientX-(r.left+r.width/2),e.clientY-(r.top+r.height*.45));swing();raf=null})},{passive:true});
window.addEventListener("deviceorientation",e=>{
if(e.gamma==null)return;
aim(Math.max(-40,Math.min(40,e.gamma*1.2)),Math.max(-40,Math.min(40,(e.beta-45)*.8)))},{passive:true});
})();
</script></body></html>""".replace("__MASCOT__", MASCOT)


def _keystream(key: bytes, length: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < length:
        out += hashlib.sha256(key + ctr.to_bytes(8, "big")).digest()
        ctr += 1
    return bytes(out[:length])


def _xcrypt(data: bytes, key: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, _keystream(key, len(data))))


def upstream_call(provider: str, model: str, messages: list, tools=None,
                  system: str | None = None) -> tuple[dict, int]:
    """Call the upstream chat API. Returns (normalized_response, tokens_used)."""
    p = PROVIDERS[provider]
    key = p["key"]
    if not key:
        raise RuntimeError(f"no API key configured for {provider}")
    body: dict = {"model": model, "max_tokens": MAX_TOKENS_REPLY}
    if system:
        if p["style"] == "anthropic":
            body["system"] = system
        else:
            messages = [{"role": "system", "content": system}] + messages
    body["messages"] = messages
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
    data = None
    for attempt in range(3):                    # upstreams blip; ride it out
        try:
            with urllib.request.urlopen(rq, timeout=180) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
            rq = urllib.request.Request(url, data=json.dumps(body).encode(),
                                        method="POST", headers=headers)
    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens")
                 or (int(usage.get("input_tokens") or 0)
                     + int(usage.get("output_tokens") or 0)))
    if not tokens:                                   # estimate as fallback
        blob = json.dumps(data)
        tokens = max(1, len(blob) // 4)
    if p["style"] == "anthropic":
        return data, tokens
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
    return ({"content": content,
             "stop_reason": "tool_use" if msg.get("tool_calls") else "end_turn"},
            tokens)


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

    def _sess(self, req: dict) -> dict | None:
        """Resolve the caller to {user, role} or None."""
        if STATIC_API_KEY and self.headers.get("x-api-key", "") == STATIC_API_KEY:
            return {"user": "static", "role": "admin"}
        tok = self.headers.get("x-fn-token", req.get("token", ""))
        rec = TOKENS.get(tok)
        if not rec or rec["exp"] < time.time():
            return None
        return {"user": rec["user"], "role": rec["role"]}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, html=PAGE)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n)) if n else {}

        if self.path == "/login":
            u = str(req.get("u", "")).strip()
            pw = str(req.get("pw", ""))
            user = next((x for x in USERS["users"] if x["u"] == u), None)
            if not user or user["h"] != _h(pw):
                # legacy single-password fallback maps to the admin account
                if u == "seva" and pw == APP_PASSWORD:
                    user = {"u": "seva", "role": "admin"}
                else:
                    return self._send(401, {"error": "wrong username/password"})
            tok = secrets.token_hex(16)
            TOKENS[tok] = {"exp": time.time() + 12 * 3600, "user": user["u"],
                           "role": user.get("role", "user")}
            for k in [k for k, v in TOKENS.items() if v["exp"] < time.time()]:
                TOKENS.pop(k)
            return self._send(200, {"token": tok, "expires": TOKENS[tok]["exp"],
                                    "user": user["u"],
                                    "role": user.get("role", "user")})

        sess = self._sess(req)
        if sess is None:
            return self._send(401, {"error": "session expired — re-login"})
        tok = self.headers.get("x-fn-token", req.get("token", ""))

        if self.path == "/models":
            models = [{"id": f"{p}:{m}", "label": _model_label(p, m)}
                      for p, pv in PROVIDERS.items() if pv["key"]
                      for m in pv["models"]]
            return self._send(200, {"models": models})

        if self.path == "/admin/verify":
            if sess["role"] != "admin":
                return self._send(403, {"error": "admin only"})
            if req.get("pw") != ADMIN_PASSWORD:
                return self._send(401, {"error": "wrong admin password"})
            atok = secrets.token_hex(16)
            ADMIN_OK[atok] = time.time() + 3600
            return self._send(200, {"ok": True, "admin_token": atok})

        if self.path == "/admin/users":
            if sess["role"] != "admin":
                return self._send(403, {"error": "admin only"})
            act = req.get("action", "list")
            if act == "add":
                nu = str(req.get("u", "")).strip()
                if not nu or any(x["u"] == nu for x in USERS["users"]):
                    return self._send(400, {"error": "bad or duplicate username"})
                USERS["users"].append({"u": nu, "h": _h(str(req.get("pw", ""))),
                                       "role": "user"})
                _save_users(USERS)
            elif act == "del":
                if req.get("u") == "seva":
                    return self._send(400, {"error": "cannot remove seva"})
                USERS["users"] = [x for x in USERS["users"]
                                  if x["u"] != req.get("u")]
                _save_users(USERS)
            elif act == "limits":
                USERS["limit"] = max(100, int(req.get("limit", 10000)))
                USERS["window_h"] = max(0.5, float(req.get("window_h", 24)))
                _save_users(USERS)
            usage = _load_usage()
            return self._send(200, {"users": [{"u": x["u"], "role": x.get("role", "user"),
                                               "used": usage.get(x["u"], {}).get("tokens", 0)}
                                              for x in USERS["users"]],
                                    "limit": USERS["limit"],
                                    "window_h": USERS["window_h"]})

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
                             f'attachment; filename="{day}.jsonl.enc.gz"')
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/task":
            tab = req.get("tab", "chat")
            model_id = req.get("model", "")
            provider, _, model = model_id.partition(":")
            message = req.get("message", "")
            t0 = time.time()
            over = _usage_check(sess["user"], sess["role"])
            if over:
                return self._send(429, {"error": over})
            try:
                if tab == "chat":
                    reply, tokens = self._chat(provider, model, message)
                    steps = []
                else:
                    reply, steps, tokens = self._code(provider, model, message,
                                                      req.get("cwd", str(WORKSPACE)))
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            _usage_record(sess["user"], sess["role"], tokens)
            self._archive({"kind": tab, "user": sess["user"], "provider": provider,
                           "model": model, "request": message, "reply": reply,
                           "steps": steps, "secs": round(time.time() - t0, 1),
                           "tokens": tokens})
            return self._send(200, {"reply": reply, "steps": steps,
                                    "tokens_used": tokens})

        return self._send(404, {"error": "not found"})

    def _chat(self, provider: str, model: str, message: str):
        try:
            data, tokens = upstream_call(provider, model,
                                         [{"role": "user", "content": message}],
                                         system=PERSONA)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise
            # some upstreams choke on the system field — degrade gracefully
            data, tokens = upstream_call(provider, model,
                                         [{"role": "user", "content": message}])
        reply = "".join(b.get("text", "") for b in data.get("content", [])
                        if b.get("type") == "text") or "(empty reply)"
        return reply, tokens

    def _code(self, provider: str, model: str, task: str,
              cwd: str = ""):
        cwd = cwd or str(WORKSPACE)
        Path(cwd).mkdir(parents=True, exist_ok=True)
        messages = [{"role": "user", "content":
                     f"Working directory: {cwd}\n\nTask: {task}\n\n"
                     "Use the tools to complete the task. Verify your work by "
                     "running it when possible. Then summarize briefly."}]
        steps, final, used = [], None, 0
        for _ in range(20):
            data, tokens = upstream_call(provider, model, messages, tools=TOOLS)
            used += tokens
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
        return final or "max steps reached", steps, used

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
    print(f"SevaMeGPT → :{PORT}")
    srv.serve_forever()
