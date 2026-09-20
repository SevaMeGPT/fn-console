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
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
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
         "limit": 10000, "window_h": 24, "max_upload_mb": 10}
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
            PROVIDERS["openrouter"]["models"] = sorted(free)[:100]
    except Exception:  # noqa: BLE001
        pass                                    # curated list stays


threading.Thread(target=_refresh_openrouter, daemon=True).start()


def _model_label(pid: str, mid: str) -> str:
    """Dropdown label: short name · free/provider — CODE tag only for specialists."""
    short = mid.split("/")[-1].replace(":free", "").replace("-", " ").strip()
    low = mid.lower()
    tag = "free" if (":free" in low or low.endswith("-free")) else f"{pid} (Limited use)"
    codey = any(k in low for k in ("coder", "code", "codestral", "devstral", "starcoder"))
    return f"{short} · {tag} · CODE" if codey else f"{short} · {tag}"


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

MASCOT = """<svg id="pehlwan" viewBox="0 0 240 250" xmlns="http://www.w3.org/2000/svg">
<style>#pehlwan #head{transform-box:fill-box;transform-origin:50% 78%;
transition:transform .35s cubic-bezier(.34,1.56,.64,1);
animation:idle 3.4s ease-in-out infinite}
#pehlwan #beard{transform-box:fill-box;transform-origin:50% 4%}
#pehlwan.swing #beard{animation:bw .75s cubic-bezier(.36,.07,.19,.97)}
@keyframes bw{0%,100%{transform:rotate(0)}22%{transform:rotate(11deg)}
48%{transform:rotate(-8deg)}72%{transform:rotate(4deg)}}
@keyframes idle{0%,100%{transform:translateY(0)}50%{transform:translateY(3px) rotate(-1.3deg)}}
</style>
<g id="head" fill="#f2e9d8">
<path id="vest" d="M44 250 C44 224 60 210 82 206 L98 226 L120 202
L142 226 L158 206 C180 210 196 224 196 250 Z"/>
<path id="beard" d="M60 134 C70 128 82 130 92 138 C98 144 106 148 112 148
C116 148 118 145 120 145 C122 145 124 148 128 148 C134 148 142 144 148 138
C158 130 170 128 180 134 C190 146 192 162 189 178 C185 204 172 226 154 238
C142 246 128 248 120 248 C112 248 98 246 86 238 C68 226 55 204 51 178
C48 162 50 146 60 134 Z"/>
<path d="M80 152 C78 174 86 200 100 216" stroke="#0d1117" stroke-width="4"
fill="none" stroke-linecap="round" opacity=".18"/>
<path d="M160 152 C162 174 154 200 140 216" stroke="#0d1117" stroke-width="4"
fill="none" stroke-linecap="round" opacity=".18"/>
<path d="M120 152 L120 224" stroke="#0d1117" stroke-width="3.5" fill="none"
stroke-linecap="round" opacity=".14"/>
<g id="glasses" fill="none" stroke="#f2e9d8" stroke-width="6">
<rect x="70" y="112" width="38" height="36" rx="11"/>
<rect x="132" y="112" width="38" height="36" rx="11"/>
<path d="M108 124 L132 124" stroke-linecap="round"/>
<path d="M70 124 L54 118" stroke-linecap="round"/>
<path d="M170 124 L186 118" stroke-linecap="round"/>
</g>
<path id="safa" d="M54 88 C34 38 66 4 116 2 C160 0 196 20 197 56
C198 76 186 88 166 90 L82 94 C66 95 56 95 54 88 Z"/>
<path id="safa-knot" d="M150 12 C164 0 184 6 186 24 C187 38 178 46 168 42
C158 36 153 25 150 12 Z"/>
<path id="plume" d="M182 18 C196 4 216 2 226 10 C230 22 222 34 206 38
C216 28 218 20 212 16 C202 10 190 12 182 18 Z"/>
<path id="safa-fold" d="M118 4 C108 28 104 52 106 90" stroke="#0d1117"
stroke-width="4" fill="none" stroke-linecap="round" opacity=".16"/>
<path id="safa-fold2" d="M150 8 C160 26 164 46 162 88" stroke="#0d1117"
stroke-width="4" fill="none" stroke-linecap="round" opacity=".12"/>
</g></svg>"""

MINI_PEHLWAN = '<svg viewBox="0 0 240 250" width="110" xmlns="http://www.w3.org/2000/svg"><g fill="#eef6f0"><path d="M48 240 C48 190 76 168 120 168 C164 168 192 190 192 240 Z"/><path d="M104 168 L120 186 L136 168" fill="none" stroke="#0b3d31" stroke-width="6" stroke-linecap="round"/><path d="M74 118 C70 112 76 106 84 110 C94 116 146 116 156 110 C164 106 170 112 166 118 C172 148 164 196 146 216 C138 226 128 232 120 232 C112 232 102 226 94 216 C76 196 68 148 74 118 Z" opacity=".92"/><circle cx="102" cy="90" r="8" fill="#0b3d31"/><circle cx="138" cy="90" r="8" fill="#0b3d31"/><path d="M52 86 C36 36 68 2 116 0 C160 -2 196 18 197 54 C198 74 186 86 166 88 L80 92 C64 93 54 92 52 86 Z"/><path d="M150 10 C164 -2 184 4 186 22 C187 38 178 46 168 42 C158 36 153 25 150 10 Z"/></g><g fill="none" stroke="#0b3d31" stroke-width="7"><rect x="70" y="112" width="38" height="36" rx="11"/><rect x="132" y="112" width="38" height="36" rx="11"/><path d="M108 124 L132 124" stroke-linecap="round"/></g></svg>'

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport
content="width=device-width,initial-scale=1"><title>SevaMeGPT</title><style>
:root{--bg:#032b23;--card:#0a3d31;--edge:#1a5643;--mut:#9cc4b4;--acc:#8fd6b4;
--grn:#2ea653;--cream:#eef6f0;--gold:#d9b45b}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto;margin:0;background:
radial-gradient(1200px 700px at 72% -10%,#0a4638 0%,var(--bg) 55%);color:var(--cream);min-height:100vh}
::webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#1a5643;border-radius:8px}
::-webkit-scrollbar-track{background:transparent}
header{display:flex;gap:4px;align-items:center;background:rgba(2,40,32,.92);
backdrop-filter:blur(10px);padding:8px 14px;border-bottom:1px solid var(--edge);
position:sticky;top:0;z-index:5;box-shadow:0 4px 24px rgba(0,0,0,.35)}
header .t{color:var(--cream);font-weight:700;margin-right:8px;font-size:16px;letter-spacing:.2px;
display:flex;align-items:center;gap:8px}
header .t img.brandlogo{height:30px;display:block;filter:drop-shadow(0 2px 8px rgba(0,0,0,.35))}
header button{background:none;border:0;color:var(--mut);padding:9px 15px;font-size:14px;
cursor:pointer;border-radius:9px;transition:.15s}
header button:hover{color:var(--cream);background:#0f4a3a}
header button.on{color:#04291f;background:var(--acc);font-weight:600}
#cd{margin-left:auto;font-size:11.5px;color:var(--mut);white-space:nowrap;
font-variant-numeric:tabular-nums;background:#0b3d31;border:1px solid var(--edge);
padding:5px 10px;border-radius:99px}
.wrap{max-width:860px;margin:0 auto;padding:18px 16px 8px}
.msg{margin:10px 0;padding:11px 15px;border-radius:14px;white-space:pre-wrap;
word-break:break-word;line-height:1.5;border:1px solid var(--edge);max-width:88%}
.you{background:linear-gradient(160deg,#0f4a3a,#0b3d31);margin-left:auto;border-color:#1a5c49}
.bot{background:#0d4436;border-color:#13614d}
.step{background:#0a3329;border-left:3px solid var(--acc);padding:7px 11px;margin:6px 0;
font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;color:#a9cdbd;
white-space:pre-wrap;word-break:break-word;border-radius:0 9px 9px 0}
.row{display:flex;gap:8px;position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 35%);
padding:12px 0}
#inputbar{display:flex;gap:8px;flex:1;background:var(--card);border:1px solid var(--edge);
border-radius:14px;padding:6px 6px 6px 4px;align-items:center;transition:border-color .15s}
#inputbar:focus-within{border-color:var(--acc)}
#inputbar input{border:0;background:transparent;flex:1;padding:9px 6px;font-size:15px;color:var(--cream)}
button.clip{padding:10px 11px;border-radius:10px;border:1px solid var(--edge);cursor:pointer;
font-size:15px;background:transparent;color:var(--mut);transition:.15s}
button.clip:hover{color:var(--cream);background:#0f4a3a}
button.clip.rec{color:#fff;background:#b02a37;border-color:#b02a37;animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.55}}
.chip{display:inline-block;background:#0b3d31;color:#a9d8c2;border-radius:8px;
padding:3px 10px;font-size:12px;margin:4px 4px 0 0;border:1px solid var(--edge)}
input,select{padding:11px;background:var(--card);color:var(--cream);border:1px solid #1d5a47;
border-radius:10px;font-size:15px;outline:none;transition:border-color .15s}
input:focus,select:focus{border-color:var(--acc)}
#in,#task,#rpw{flex:1}
button.go{padding:10px 20px;border-radius:11px;border:0;cursor:pointer;font-size:15px;color:#fff;
background:linear-gradient(135deg,#31b45c,#23913c);font-weight:600;letter-spacing:.2px;transition:.15s}
button.go:hover{filter:brightness(1.12)}
button.go.bl{background:linear-gradient(135deg,#3f7ae4,#3263c4)}
.spin{display:none;color:var(--acc);padding:6px;font-size:13px}
.hide{display:none!important}
pre{white-space:pre-wrap;word-break:break-word}
.card{background:linear-gradient(180deg,#0b4436,var(--card));border:1px solid var(--edge);
border-radius:16px;padding:20px;margin:16px 0;box-shadow:0 8px 30px rgba(0,0,0,.28)}
.gate{max-width:400px;margin:4vh auto 0;text-align:center}
.gate .brandlogo-big{width:min(330px,86%);margin-bottom:2px;filter:drop-shadow(0 6px 22px rgba(0,0,0,.35))}
.gate svg{width:150px;height:157px;filter:drop-shadow(0 10px 30px rgba(0,0,0,.45))}
.gate h1{font-size:26px;margin:8px 0 3px;letter-spacing:.3px;color:var(--cream)}
.gate p{color:var(--mut);font-size:13px;margin:0 0 18px}
.gate input{width:100%;margin:7px 0;text-align:center;padding:12px}
.gate button.go{width:100%;margin-top:12px;padding:13px}
.foot{margin:26px auto 14px;text-align:center;color:var(--mut);font-size:12.5px}
.foot a{color:#bfe3d2;text-decoration:none;border-bottom:1px dotted #2f6b57}
.foot a:hover{color:var(--cream)}
.foot span{margin:0 7px;color:#2f6b57}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td,th{padding:8px;border-bottom:1px solid var(--edge);text-align:left}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
h3{margin:2px 0 14px;font-size:17px}
.hint{color:var(--mut);font-size:12px;line-height:1.55}
.aboutwrap{max-width:720px;margin:24px auto;text-align:center}
.aboutwrap .brandlogo-big{width:min(520px,94%);filter:drop-shadow(0 8px 26px rgba(0,0,0,.35))}
.constr{display:inline-block;margin:14px 0;padding:10px 22px;border-radius:99px;
background:repeating-linear-gradient(45deg,#0b4436,#0b4436 14px,#0d5040 14px,#0d5040 28px);
border:1px solid var(--edge);font-size:14px;color:var(--cream);font-weight:600}
@media(max-width:640px){.msg{max-width:100%}.you{margin-left:6%}.wrap{padding:12px 10px 6px}
header button{padding:9px 10px;font-size:13px}#cd{display:none}
.gate .brandlogo-big{width:96%}}
</style></head><body><div id=gate class=gate>
<img class=brandlogo-big src="data:image/png;base64,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" alt="Sevamemon">
__MASCOT__
<h1>SevaMeGPT</h1>
<p>bindaas chats • code karo • mast</p>
<input id=u placeholder="username" autocomplete=username onkeydown="if(event.key==='Enter')document.getElementById('pw').focus()">
<input id=pw type=password placeholder="password" onkeydown="if(event.key==='Enter')login()">
<button class=go onclick=login()>Enter</button><div id=lerr style=color:#f85149;font-size:13px></div>
<div class=foot><a href="mailto:admin@sevamemon.com">admin@sevamemon.com</a><span>|</span>
<a href="#" onclick="tab('about',document.getElementById('tb-about'));return false">About us</a></div>
</div>
<div id=app class=hide><header><span class=t><img class=brandlogo src="data:image/png;base64,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" alt="Sevamemon"></span>
<button class=on id=tb-chat onclick="tab('chat',this)">Chat</button>
<button id=tb-code onclick="tab('code',this)">Code Agent</button>
<button id=tb-admin onclick="tab('admin',this)">Admin</button>
<button id=tb-about onclick="tab('about',this)">About</button>
<span id=cd style="margin-left:auto"></span>
<select id=m style="margin-left:12px;max-width:34vw"></select></header>
<div class=wrap id=p-chat>
<div id=greeter style="text-align:center;padding:26px 0 4px;color:var(--mut);font-size:14px">
<svg viewBox="0 0 240 250" width="110" xmlns="http://www.w3.org/2000/svg"><g fill="#eef6f0"><path d="M48 240 C48 190 76 168 120 168 C164 168 192 190 192 240 Z"/><path d="M104 168 L120 186 L136 168" fill="none" stroke="#0b3d31" stroke-width="6" stroke-linecap="round"/><path d="M74 118 C70 112 76 106 84 110 C94 116 146 116 156 110 C164 106 170 112 166 118 C172 148 164 196 146 216 C138 226 128 232 120 232 C112 232 102 226 94 216 C76 196 68 148 74 118 Z" opacity=".92"/><circle cx="102" cy="90" r="8" fill="#0b3d31"/><circle cx="138" cy="90" r="8" fill="#0b3d31"/><path d="M52 86 C36 36 68 2 116 0 C160 -2 196 18 197 54 C198 74 186 86 166 88 L80 92 C64 93 54 92 52 86 Z"/><path d="M150 10 C164 -2 184 4 186 22 C187 38 178 46 168 42 C158 36 153 25 150 10 Z"/></g><g fill="none" stroke="#0b3d31" stroke-width="7"><rect x="70" y="112" width="38" height="36" rx="11"/><rect x="132" y="112" width="38" height="36" rx="11"/><path d="M108 124 L132 124" stroke-linecap="round"/></g></svg>
<div style="margin-top:8px">Namaste bhai! <b style="color:var(--cream)">SevaMeGPT</b> here — pucho jo poochna hai, ya Code Agent se kaam karwao.</div></div>
<div id=log></div><div id=spin class=spin>seva soch raha hai…</div>
<div id=chips-chat></div>
<div class=row><div id=inputbar>
<button class=clip id=mic-chat title="voice input" onclick=mic('chat')>🎤</button>
<button class=clip title="attach file" onclick="document.getElementById('file-chat').click()">&#128206;</button>
<input id=file-chat type=file class=hide onchange=uploadFile('chat')>
<input id=in placeholder="ask anything…" onkeydown="if(event.key==='Enter')send()">
<button class=go id=sendbtn onclick=send()>Send</button></div></div></div>
<div id=p-code class=hide><div class=row style=margin-bottom:8px>working dir:
<input id=cwd value="/app/workspace" style=flex:1></div>
<div id=clog></div><div id=cspin class=spin>agent working…</div>
<div id=chips-code></div>
<div class=row><div id=inputbar>
<button class=clip id=mic-code title="voice input" onclick=mic('code')>🎤</button>
<button class=clip title="attach file" onclick="document.getElementById('file-code').click()">&#128206;</button>
<input id=file-code type=file class=hide onchange=uploadFile('code')>
<input id=task placeholder="describe the coding task…" onkeydown="if(event.key==='Enter')code()">
<button class=go id=runbtn onclick=code()>Run</button></div></div></div>
<div id=p-admin class=hide>
<div id=alock class=card style="max-width:420px;margin:20px auto"><h3>Admin area</h3>
<div class=row style=position:static;background:none><input id=apw0 type=password placeholder="admin password" style=flex:1 onkeydown="if(event.key==='Enter')admUnlock()"><button class=go onclick=admUnlock()>Unlock</button></div>
<p id=aerr style=color:#f85149;font-size:13px></p>
<p class=hint>The session archive, user management and token budget are hidden until the admin password is validated by the server.</p></div>
<div id=apanel class=hide>
<div class=card style=margin:14px 0><h3>Session archive (today)</h3>
<div class=row style=position:static;background:none><input id=apw type=password placeholder="admin password" style=flex:1>
<button class=go onclick=decryptLog()>Decrypt</button>
<button class="go bl" onclick=downloadGz()>Download .gz</button></div>
<pre id=alog style="background:#03251e;padding:12px;max-height:46vh;overflow:auto;border-radius:10px">—</pre>
<p class=hint>Every chat and coding session is logged here, encrypted (counter-mode stream cipher, key derived from the admin password via PBKDF2-200k) and gzip-compressed. Download gives the raw encrypted file.</p></div>
<div class=card style=margin:14px 0><h3>Users</h3>
<table id=utab></table>
<div class=row style=position:static;background:none;padding:8px 0 0>
<input id=nu placeholder="new username" style=flex:1><input id=npw placeholder="password" style=flex:1 onkeydown="if(event.key==='Enter')addUser()"><button class=go onclick=addUser()>Add user</button></div>
<p id=uerr style=color:#f85149;font-size:13px></p></div>
<div class=card style=margin:14px 0><h3>Limits (non-admin users)</h3>
<div class=row style=position:static;background:none;padding:0>
<input id=lim type=number placeholder="tokens per window" title="token budget">
<input id=wh type=number placeholder="window hours" title="budget window">
<input id=maxmb type=number placeholder="max upload MB" title="file upload cap">
<button class="go bl" onclick=saveLimits()>Save</button></div>
<p class=hint>Each non-admin user can use this many upstream tokens per window and upload files up to this size. Admins are unlimited on both.</p></div>
</div></div>
<div class=foot><a href="mailto:admin@sevamemon.com">admin@sevamemon.com</a>
<span>|</span><a href="#" onclick="tab('about',document.getElementById('tb-about'));return false">About us</a></div>
<div id=p-about class=hide><div class=aboutwrap>
<img class=brandlogo-big src="data:image/png;base64,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" alt="Sevamemon">
<h2 style="margin:20px 0 8px">🚧 Under construction 🚧</h2>
<div class=constr>pehlwan-level mehnat chal rahi hai</div>
<div style="margin:16px 0 4px"><svg viewBox="0 0 240 250" width="110" xmlns="http://www.w3.org/2000/svg"><g fill="#eef6f0"><path d="M48 240 C48 190 76 168 120 168 C164 168 192 190 192 240 Z"/><path d="M104 168 L120 186 L136 168" fill="none" stroke="#0b3d31" stroke-width="6" stroke-linecap="round"/><path d="M74 118 C70 112 76 106 84 110 C94 116 146 116 156 110 C164 106 170 112 166 118 C172 148 164 196 146 216 C138 226 128 232 120 232 C112 232 102 226 94 216 C76 196 68 148 74 118 Z" opacity=".92"/><circle cx="102" cy="90" r="8" fill="#0b3d31"/><circle cx="138" cy="90" r="8" fill="#0b3d31"/><path d="M52 86 C36 36 68 2 116 0 C160 -2 196 18 197 54 C198 74 186 86 166 88 L80 92 C64 93 54 92 52 86 Z"/><path d="M150 10 C164 -2 184 4 186 22 C187 38 178 46 168 42 C158 36 153 25 150 10 Z"/></g><g fill="none" stroke="#0b3d31" stroke-width="7"><rect x="70" y="112" width="38" height="36" rx="11"/><rect x="132" y="112" width="38" height="36" rx="11"/><path d="M108 124 L132 124" stroke-linecap="round"/></g></svg></div>
<p class=hint style="font-size:14px;max-width:520px;margin:14px auto">Humari poori kahani — kaam, junoon aur thodi masti — yahan jald hi aayegi. Tab tak bhai-bhai raho: neeche chat karo, kaam karwao, mazze karo.</p>
<p class=hint>sawaal? <a href="mailto:admin@sevamemon.com" style="color:#bfe3d2">admin@sevamemon.com</a></p>
</div></div>
<div id=reauth style="display:none;position:fixed;inset:0;background:rgba(2,20,15,.8);
align-items:center;justify-content:center;z-index:9">
<div class=card style="min-width:300px;max-width:92vw;text-align:center">
<h3 style=margin-top:0>Session expired</h3>
<p class=hint>Your session timed out. Enter your password to continue — your chat history is preserved.</p>
<div class=row style=position:static;background:none><input id=rpw type=password placeholder="password" onkeydown="if(event.key==='Enter')continueSess()"><button class=go onclick=continueSess()>Continue</button></div>
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
for(const id of ["p-chat","p-code","p-admin","p-about"])document.getElementById(id).classList.add("hide");
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
document.getElementById("app").dataset.maxmb=ms.max_upload_mb||10;
const sel=document.getElementById("m");sel.innerHTML="";
for(const m of ms){const o=document.createElement("option");o.value=m.id;
o.textContent=m.label;sel.appendChild(o)}
const pref=["nemotron-3-ultra","ling-3.0-flash","qwen3","deepseek","gemma"];
const def=[...sel.options].find(o=>o.label.includes("free")&&
pref.some(p=>o.value.toLowerCase().includes(p)));
if(def)sel.value=def.value;}
let BUSY=false;
async function post(path,body){if(BUSY){throw 0}BUSY=true;
const ctrl=new AbortController();const tm=setTimeout(()=>ctrl.abort(),240000);
let r;
try{r=await fetch(path,{method:"POST",signal:ctrl.signal,
headers:{"content-type":"application/json","x-fn-token":(SESS?SESS.tok:"")},
body:JSON.stringify(body)});}
catch(e){clearTimeout(tm);BUSY=false;
if(e.name==="AbortError")alert("upstream ne 4 minute diya, phir bhi jawab nahi — dobara try karo");
else alert("network error — dobara try karo");throw 0}
clearTimeout(tm);BUSY=false;
if(r.status===401){showReauth();throw 0}
const d=await r.json();if(r.status===429){alert(d.error||"limit reached");throw 0}return d}
async function uploadFile(tab){const inp=document.getElementById("file-"+tab);
const f=inp.files[0];if(!f)return;
const maxMb=parseFloat(document.getElementById("app").dataset.maxmb||"10");
if(f.size>maxMb*1048576){alert("file "+(f.size/1048576).toFixed(1)+
" MB — limit "+maxMb+" MB (admin sets it)");inp.value="";return}
const chip=document.getElementById("chips-"+tab);
chip.insertAdjacentHTML("beforeend",`<span class="chip">uploading ${esc(f.name)}…</span>`);
const r=await fetch("/upload",{method:"POST",
headers:{"content-type":"application/octet-stream","x-fn-token":(SESS?SESS.tok:""),
"x-filename":encodeURIComponent(f.name)},body:f});
const chips=chip.querySelectorAll(".chip");chips[chips.length-1].remove();
if(r.status===413){const d=await r.json();alert(d.error);inp.value="";return}
if(!r.ok){alert("upload failed — phir se try karo");inp.value="";return}
const d=await r.json();window["att_"+tab]=d.name;
if(tab==="chat"&&f.size<16384&&/\.(txt|md|csv|json|py|js|log|ini|yaml|yml|html|css|ts|xml)$/i.test(f.name)){
window["atttext_"+tab]=(await f.text()).slice(0,12000)}
chip.insertAdjacentHTML("beforeend",`<span class="chip">&#128206; ${esc(d.name)} (${(d.size/1024).toFixed(1)} KB)</span>`);
inp.value="";}
function mic(tab){const R=window.SpeechRecognition||window.webkitSpeechRecognition;
if(!R){alert("voice input is built into Chrome/Edge — wahan try karo");return}
const key="_rec_"+tab;
if(window[key]){window[key].stop();return}
const rec=new R();rec.lang=navigator.language||"en-IN";rec.interimResults=true;
const inp=document.getElementById(tab==="chat"?"in":"task");
const base=inp.value;
rec.onresult=e=>{let t="";for(const r of e.results)t+=r[0].transcript;
inp.value=(base?base+" ":"")+t};
rec.onend=()=>{document.getElementById("mic-"+tab).classList.remove("rec");window[key]=null};
rec.onerror=e=>{window[key]=null;
document.getElementById("mic-"+tab).classList.remove("rec");
if(e.error==="not-allowed")alert("mic permission chahiye bhai — allow karo")};
window[key]=rec;document.getElementById("mic-"+tab).classList.add("rec");rec.start()}
async function send(){const i=document.getElementById("in");
let t=i.value.trim();
if(!t&&!window.att_chat)return;i.value="";
const log=document.getElementById("log");
if(window.att_chat){
const txt=window.atttext_chat?window.atttext_chat+"\\n":"";
t=`[file: ${window.att_chat}]
${txt}
${t||"is file ke baare mein batao"}`}
log.insertAdjacentHTML("beforeend",`<div class="msg you">${esc(t)}</div>`);
const gr=document.getElementById("greeter");if(gr)gr.style.display="none";
document.getElementById("chips-chat").innerHTML="";
window.att_chat=window.atttext_chat=null;
window.att_chat=window.atttext_chat=null;
const sp=document.getElementById("spin");sp.style.display="block";
const sb=document.getElementById("sendbtn");if(sb)sb.disabled=true;
try{const d=await post("/task",{tab:"chat",model:document.getElementById("m").value,message:t,attach:window.att_chat||null});
log.insertAdjacentHTML("beforeend",`<div class="msg bot">${esc(d.reply||d.error||"(empty)")}</div>`)}
catch(e){if(e!==0)log.insertAdjacentHTML("beforeend",
`<div class="msg bot">upar se hawa lag gayi — try again</div>`)}
sp.style.display="none";if(typeof sb!=='undefined')sb.disabled=false}
async function code(){const i=document.getElementById("task");
let t=i.value.trim();
if(!t&&!window.att_code)return;i.value="";const cl=document.getElementById("clog");
const sp=document.getElementById("cspin");sp.style.display="block";
const rb=document.getElementById("runbtn");if(rb)rb.disabled=true;
if(window.att_code){t=(t||"inspect the uploaded file")+
`; the file is at uploads/${window.att_code} in the working directory`}
document.getElementById("chips-code").innerHTML="";
window.att_code=null;
try{const d=await post("/task",{tab:"code",model:document.getElementById("m").value,
message:t,cwd:document.getElementById("cwd").value,attach:window.att_code||null});
for(const s of (d.steps||[]))cl.insertAdjacentHTML("beforeend",`<div class="step">${esc(s)}</div>`);
cl.insertAdjacentHTML("beforeend",`<div class="msg bot">${esc(d.reply||d.error||"")}</div>`)}
catch(e){if(e!==0)cl.insertAdjacentHTML("beforeend",
`<div class="msg bot">agent ne haath khada kar diya — try again</div>`)}
sp.style.display="none";if(typeof rb!=='undefined')rb.disabled=false}
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
const L=d.limit,W=d.window_h,M=d.max_upload_mb;
document.getElementById("lim").value=L;document.getElementById("wh").value=W;
document.getElementById("maxmb").value=M;
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
const maxmb=parseFloat(document.getElementById("maxmb").value)||10;
await post("/admin/users",{action:"limits",limit:lim,window_h:wh,max_upload_mb:maxmb});
adminLoad()}
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
                  system: str | None = None,
                  image: tuple | None = None) -> tuple[dict, int]:
    """Call the upstream chat API. Returns (normalized_response, tokens_used).

    image: (b64_str, media_type) attached to the FIRST user message."""
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
    if image:
        b64data, media = image
        for msg in messages:
            if msg.get("role") == "user":
                if p["style"] == "anthropic":
                    msg["content"] = [
                        {"type": "image", "source": {"type": "base64",
                         "media_type": media, "data": b64data}},
                        {"type": "text", "text": msg["content"]}]
                else:
                    msg["content"] = [
                        {"type": "text", "text": msg["content"]},
                        {"type": "image_url", "image_url": {"url":
                         f"data:{media};base64,{b64data}"}}]
                break
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


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    """Strip control chars providers reject (keep \n and \t)."""
    return _CTRL.sub("", text)


def run_tool(name: str, inp: dict, cwd: str) -> str:
    try:
        if name == "list_dir":
            t = resolve(cwd, inp.get("path", "."))
            return "\n".join(f"{'D' if x.is_dir() else 'F'} {x.name}"
                             for x in sorted(t.iterdir())[:200]) or "(empty)"
        if name == "read_file":
            f = resolve(cwd, inp["path"])
            head = f.open("rb").read(4096)
            if b"\x00" in head:
                return (f"binary file ({f.stat().st_size:,} bytes) — "
                        "not readable as text. If it is an image, it was "
                        "already shown to you in the conversation.")
            return _clean(f.read_text(encoding="utf-8",
                                       errors="replace"))[:MAX_OUT]
        if name == "write_file":
            f = resolve(cwd, inp["path"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(inp["content"], encoding="utf-8")
            return _clean(f"written {len(inp['content'])} chars -> {f}")
        if name == "run_cmd":
            r = subprocess.run(inp["command"], shell=True, capture_output=True,
                               text=True, timeout=MAX_CMD_SECONDS, cwd=cwd)
            out = ((r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
                   )[:MAX_OUT]
            return _clean(f"{out}\n[exit {r.returncode}]")
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

    def _do_upload(self):
        sess = self._sess({})
        if sess is None:
            return self._send(401, {"error": "session expired — re-login"})
        max_mb = float(USERS.get("max_upload_mb", 10))
        n = int(self.headers.get("content-length", 0))
        if n <= 0:
            return self._send(400, {"error": "empty upload"})
        if n > max_mb * 1024 * 1024:
            return self._send(413, {"error":
                f"file too heavy bhai — limit {max_mb:g} MB (admin sets it)"})
        raw_name = urllib.parse.unquote(
            self.headers.get("x-filename", "file.bin"))
        safe = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in raw_name)
        safe = safe.strip().strip(".")
        if not safe or safe.startswith("."):
            safe = "_" + safe.lstrip(".")
        safe = safe or "file.bin"
        updir = WORKSPACE / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        (updir / safe).write_bytes(self.rfile.read(n))
        return self._send(200, {"ok": True, "name": safe, "size": n,
                                "path": f"uploads/{safe}"})

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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, html=PAGE)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/upload":
            return self._do_upload()
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n)) if n else {}

        if self.path == "/login":
            u = str(req.get("u", "")).strip()
            pw = str(req.get("pw", ""))
            user = next((x for x in USERS["users"]
                         if x["u"].lower() == u.lower()), None)
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
            return self._send(200, {"models": models,
                                    "max_upload_mb": float(
                                        USERS.get("max_upload_mb", 10))})

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
                USERS["max_upload_mb"] = max(0.1, float(req.get("max_upload_mb", 10)))
                _save_users(USERS)
            usage = _load_usage()
            return self._send(200, {"users": [{"u": x["u"], "role": x.get("role", "user"),
                                               "used": usage.get(x["u"], {}).get("tokens", 0)}
                                              for x in USERS["users"]],
                                    "limit": USERS["limit"],
                                    "window_h": USERS["window_h"],
                                    "max_upload_mb": float(
                                        USERS.get("max_upload_mb", 10))})

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
            attach = req.get("attach", "")
            image = None
            if attach:
                f = (WORKSPACE / "uploads" / Path(attach).name)
                if f.exists():
                    ext = f.suffix.lower()
                    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif") \
                            and f.stat().st_size <= 4 * 1024 * 1024:
                        import base64 as _b64
                        image = (_b64.b64encode(f.read_bytes()).decode(),
                                 {".png": "image/png", ".jpg": "image/jpeg",
                                  ".jpeg": "image/jpeg", ".webp": "image/webp",
                                  ".gif": "image/gif"}[ext])
                    elif f.stat().st_size <= 64 * 1024:
                        body_txt = _clean(f.read_text(encoding="utf-8",
                                                       errors="replace"))
                        message = (f"[attached file: {attach}]\n" + body_txt
                                   + "\n\n" + message)
            t0 = time.time()
            free_model = (":free" in model) or model.endswith("-free")
            over = None if free_model else _usage_check(sess["user"], sess["role"])
            if over:
                return self._send(429, {"error": over})
            try:
                if tab == "chat":
                    reply, tokens = self._chat(provider, model, message,
                                               image=image)
                    steps = []
                else:
                    reply, steps, tokens = self._code(provider, model, message,
                                                      req.get("cwd", str(WORKSPACE)),
                                                      image=image)
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            if not free_model:
                _usage_record(sess["user"], sess["role"], tokens)
            self._archive({"kind": tab, "user": sess["user"], "provider": provider,
                           "model": model, "request": message, "reply": reply,
                           "steps": steps, "secs": round(time.time() - t0, 1),
                           "tokens": tokens})
            return self._send(200, {"reply": reply, "steps": steps,
                                    "tokens_used": tokens})

        return self._send(404, {"error": "not found"})

    def _chat(self, provider: str, model: str, message: str,
              image: tuple | None = None):
        try:
            data, tokens = upstream_call(provider, model,
                                         [{"role": "user", "content": message}],
                                         system=PERSONA, image=image)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise
            # degrade: some upstreams reject images or the system field
            data, tokens = upstream_call(provider, model,
                                         [{"role": "user", "content": message}])
            if image:
                message += "\n(an attached image could not be shown to this model)"
        reply = "".join(b.get("text", "") for b in data.get("content", [])
                        if b.get("type") == "text") or "(empty reply)"
        return reply, tokens

    def _code(self, provider: str, model: str, task: str,
              cwd: str = "", image: tuple | None = None):
        cwd = cwd or str(WORKSPACE)
        Path(cwd).mkdir(parents=True, exist_ok=True)
        messages = [{"role": "user", "content":
                     f"Working directory: {cwd}\n\nTask: {task}\n\n"
                     "Use the tools to complete the task. Verify your work by "
                     "running it when possible. Then summarize briefly."}]
        steps, final, used = [], None, 0
        first_image = image
        for _ in range(20):
            try:
                data, tokens = upstream_call(provider, model, messages,
                                             tools=TOOLS, image=first_image)
            except urllib.error.HTTPError as e:
                if e.code >= 500 or first_image is None:
                    raise
                first_image = None          # retry without the image
                data, tokens = upstream_call(provider, model, messages,
                                             tools=TOOLS)
            first_image = None
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
                                "content": _clean(str(out))[:MAX_OUT]})
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
    print(f"SevaMeGPT -> :{PORT}")
    srv.serve_forever()
    srv.serve_forever()
