#!/usr/bin/env python3
"""Jiatrix agent with web UI. One file + static/index.html.

  python agent.py [secretary|dev|sysadmin]     run from the folder the agent should work in

Env: AGENT_MODE, AGENT_MODEL, AGENT_CTX, AGENT_OWNER, AGENT_TIMEOUT, AGENT_NOTHINK,
     AGENT_HOST (127.0.0.1), AGENT_PORT (8765), AGENT_TOKEN, OLLAMA_HOST
Needs: pip install ollama fastapi uvicorn
"""
import os, re, sys, json, uuid, html, queue, difflib, pathlib, secrets, threading, subprocess, datetime
import urllib.request

import ollama, uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

MODE = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("AGENT_MODE", "secretary")).lower()
if MODE not in ("secretary", "dev", "sysadmin"):
    sys.exit("mode must be: secretary | dev | sysadmin")

DEFAULT_MODELS = {"secretary": "qwen2.5:7b", "dev": "qwen3:4b", "sysadmin": "qwen2.5:7b"}
MODEL = os.getenv("AGENT_MODEL", DEFAULT_MODELS[MODE])
OWNER = os.getenv("AGENT_OWNER", "Joel (pyjoek)")
ROOT = pathlib.Path.cwd().resolve()
HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static" / "index.html"
HOME = pathlib.Path.home() / ".jiatrix"            # memory lives outside any project
NOTES, TASKS = HOME / "notes.md", HOME / "tasks.md"
OPTS = {"num_ctx": int(os.getenv("AGENT_CTX", "8192")), "temperature": 0}
THINK = {"think": False} if os.getenv("AGENT_NOTHINK") else {}
TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "300"))
SKIP = {".git", "node_modules", "vendor", "storage", "__pycache__", ".venv", "dist", "build", ".next"}
MAX_STEPS = 10
ALLOW_OUTSIDE = MODE == "sysadmin"                 # sysadmin may touch system paths (still asks first)
MEMORY_TOOLS = {"remember", "add_task", "done_task"}

HOST = os.getenv("AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("AGENT_PORT", "8765"))
TOKEN = os.getenv("AGENT_TOKEN") or secrets.token_urlsafe(16)
APPROVAL_TIMEOUT = 300

# ---------- approvals (browser replaces the terminal y/N) ----------
Q = queue.Queue()                                  # events for the turn currently running
pending = {}                                       # approval id -> {"ev": Event, "ok": bool}

def emit(obj):
    Q.put(obj)

def confirm(msg):
    aid = uuid.uuid4().hex[:8]
    slot = {"ev": threading.Event(), "ok": False}
    pending[aid] = slot
    emit({"type": "approval", "id": aid, "text": msg})
    slot["ev"].wait(APPROVAL_TIMEOUT)              # no answer = deny
    pending.pop(aid, None)
    return slot["ok"]

# ---------- safety helpers ----------
def safe(p):
    p = (ROOT / p).resolve()
    if not ALLOW_OUTSIDE and not p.is_relative_to(ROOT):
        raise ValueError("outside working folder")
    return p

def guard_git(p):
    if ".git" in p.parts:
        raise ValueError("refusing to touch .git")

def preview(old, new, name, limit=40):
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  f"a/{name}", f"b/{name}", lineterm="", n=2))
    if len(d) > limit:
        d = d[:limit] + [f"... ({len(d) - limit} more diff lines)"]
    return "\n".join(d)

# ---------- file tools ----------
def list_dir(path=".", depth="1"):
    out = []
    def walk(d, lvl):
        for f in sorted(d.iterdir()):
            if f.name in SKIP:
                continue
            out.append(f"{'  ' * lvl}{'d' if f.is_dir() else 'f'} {f.name}")
            if f.is_dir() and lvl + 1 < int(depth):
                walk(f, lvl + 1)
    walk(safe(path), 0)
    return "\n".join(out)[:3000] or "(empty)"

def read_file(path, start="1", end="200"):
    lines = safe(path).read_text(errors="replace").splitlines()
    s, e = max(1, int(start)), min(int(end), len(lines))
    out, used = [], 0
    for i in range(s, e + 1):
        row = f"{i}: {lines[i - 1]}"
        if used + len(row) > 6000:
            e = i - 1
            break
        out.append(row)
        used += len(row) + 1
    return "\n".join(out) + f"\n[lines {s}-{e} of {len(lines)}]"

def search(pattern, path="."):
    ex = [f"--exclude-dir={d}" for d in SKIP]
    r = subprocess.run(["grep", "-rIn", *ex, "--", pattern, str(safe(path))],
                       capture_output=True, text=True)
    return r.stdout.replace(str(ROOT) + "/", "")[:3000] or "no matches"

def edit_file(path, old, new):
    p = safe(path); guard_git(p)
    text = p.read_text(errors="replace")
    n = text.count(old) if old else 0
    if n != 1:
        return f"error: old text found {n} times, need exactly 1. Copy more surrounding lines."
    updated = text.replace(old, new, 1)
    if not confirm(f"Edit {path}\n{preview(text, updated, path)}"):
        return "user denied"
    p.write_text(updated)
    return "edited"

def write_file(path, content):
    p = safe(path); guard_git(p)
    old = p.read_text(errors="replace") if p.exists() else None
    verb = "Overwrite" if old is not None else "Create"
    if not confirm(f"{verb} {p}\n{preview(old or '', content, path)}"):
        return "user denied"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"written {len(content)} chars to {path}"

def run(command):
    if not confirm(f"Run: {command}"):
        return "user denied"
    try:
        r = subprocess.run(command, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"error: timed out after {TIMEOUT}s"
    return f"exit {r.returncode}\n" + ((r.stdout + r.stderr)[-3000:] or "(no output)")

# ---------- git tools (only when asked) ----------
def git(*a):
    r = subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()

def git_status():
    return git("status", "--short", "--branch")[:3000] or "clean"

def git_diff(path=""):
    return (git("diff", "--", str(safe(path))) if path else git("diff"))[:4000] or "no unstaged changes"

# ---------- secretary tools: memory, tasks, web ----------
def load(p):
    return p.read_text() if p.exists() else ""

def append(p, line):
    HOME.mkdir(exist_ok=True)
    with p.open("a") as f:
        f.write(line.strip() + "\n")

def remember(note):
    append(NOTES, f"- {datetime.date.today()}: {note}")
    return "saved"

def _tasks():
    return [l for l in load(TASKS).splitlines() if l.strip()]

def add_task(task):
    append(TASKS, f"[ ] {task}")
    return "task added"

def list_tasks():
    return "\n".join(f"{i}. {l}" for i, l in enumerate(_tasks(), 1)) or "no tasks"

def done_task(number):
    t, i = _tasks(), int(number) - 1
    if not 0 <= i < len(t):
        return "error: no such task number"
    t[i] = t[i].replace("[ ]", "[x]", 1)
    TASKS.write_text("\n".join(t) + "\n")
    return "marked done"

def open_tasks():
    return "\n".join(l for l in _tasks() if l.startswith("[ ]")) or "none"

def fetch_url(url):
    if not url.startswith(("http://", "https://")):
        return "error: only http(s) urls"
    if not confirm(f"Allow internet access: fetch {url}?"):
        return "user denied"
    req = urllib.request.Request(url, headers={"User-Agent": "jiatrix"})
    page = urllib.request.urlopen(req, timeout=15).read(200000).decode(errors="replace")
    page = re.sub(r"(?s)<(script|style).*?</\1>", " ", page)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)).strip()[:4000]

# ---------- tool specs ----------
def spec(fn, desc, props, required):
    return {"type": "function", "function": {"name": fn.__name__, "description": desc,
            "parameters": {"type": "object",
                           "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
                           "required": required}}}

ALL = {
    list_dir: spec(list_dir, "List files in a folder", {"path": "folder, default .", "depth": "levels deep, default 1"}, []),
    read_file: spec(read_file, "Read a text file with line numbers, 200 lines by default",
                    {"path": "file path", "start": "first line, default 1", "end": "last line, default 200"}, ["path"]),
    search: spec(search, "Grep for text in files", {"pattern": "text to find", "path": "folder, default ."}, ["pattern"]),
    edit_file: spec(edit_file, "Replace one exact snippet in an existing file. Best for small changes. "
                    "old must match exactly once, copied WITHOUT line-number prefixes",
                    {"path": "file path", "old": "exact text to replace", "new": "replacement text"}, ["path", "old", "new"]),
    write_file: spec(write_file, "Create a file or fully overwrite one. USE THIS whenever the owner asks to "
                     "create, write, save or replace a file. Never paste file contents in chat instead",
                     {"path": "file path", "content": "full file content"}, ["path", "content"]),
    run: spec(run, "Run a shell command in the working folder", {"command": "shell command"}, ["command"]),
    git_status: spec(git_status, "Show git branch and changed files (only when asked or relevant)", {}, []),
    git_diff: spec(git_diff, "Show unstaged git changes (only when asked or relevant)", {"path": "file path, optional"}, []),
    remember: spec(remember, "Save a lasting fact about the owner, his work, goals or preferences",
                   {"note": "one short factual sentence"}, ["note"]),
    add_task: spec(add_task, "Add a to-do item", {"task": "what to do"}, ["task"]),
    list_tasks: spec(list_tasks, "Show all to-dos with numbers", {}, []),
    done_task: spec(done_task, "Mark a to-do done", {"number": "task number from list_tasks"}, ["number"]),
    fetch_url: spec(fetch_url, "Fetch a web page as text. Needs owner approval each time", {"url": "http(s) url"}, ["url"]),
}
CORE = [list_dir, read_file, search, edit_file, write_file, run]
GIT = [git_status, git_diff]
SECRETARY = [remember, add_task, list_tasks, done_task, fetch_url]
TOOLSET = {"dev": CORE + GIT, "secretary": CORE + SECRETARY, "sysadmin": CORE + [fetch_url]}[MODE]
FUNCS = {f.__name__: f for f in TOOLSET}
TOOLS = [ALL[f] for f in TOOLSET]

# ---------- prompts ----------
RULES = (
    "Rules:\n"
    "- Act, don't narrate. Call tools yourself. Never ask the owner to run a command, and never print a tool call "
    "as text or JSON: call the tool. Approval prompts are handled by the tools.\n"
    "- When the owner says create, write, save, or replace a file, call write_file immediately with the full "
    "content. Do not paste the content in chat instead. For small changes to an existing file use edit_file.\n"
    "- Never invent paths, file contents, or facts. If a tool errors, read the error and fix your call.\n"
    "- Search before reading; read big files in line ranges.\n"
    "- If you are blocked, ask ONE short question."
)

def stack_info():
    marks = {"artisan": "Laravel", "composer.json": "PHP", "package.json": "Node", "vite.config.js": "Vite",
             "requirements.txt": "Python", "pyproject.toml": "Python", "Cargo.toml": "Rust", "go.mod": "Go"}
    stack = sorted({v for k, v in marks.items() if (ROOT / k).exists()})
    tests = {"Laravel": "php artisan test", "Node": "npm test", "Python": "pytest"}
    hints = [v for k, v in tests.items() if k in stack]
    return (f"Detected stack: {', '.join(stack) or 'unknown'}. "
            f"Likely test command: {', '.join(hints) or 'look at the project files'}.")

def system_prompt():
    if MODE == "dev":
        return (
            f"You are Jiatrix-Dev, a senior coding agent for {OWNER}, working in {ROOT}. "
            "Style: direct, concise, dry wit, no filler, no flattery. If you don't know, say so. "
            "If the code or plan is bad, say why and propose the fix.\n"
            f"{stack_info()}\n\n"
            "Workflow:\n"
            "1. Search to find the code, then read only the lines you need.\n"
            "2. For non-trivial work, state the plan in one or two lines, then do it.\n"
            "3. Small change: edit_file with the smallest exact snippet. New file or full rewrite: write_file.\n"
            "4. After changing code, run the tests or linter if the project has them and read the output. "
            "Never claim something works without running it.\n"
            "5. Report: what changed, what you verified, what is left. "
            "Use git tools only when asked or when they help the task.\n\n" + RULES)
    if MODE == "sysadmin":
        return (
            f"You are Jiatrix-Sys, system administrator of this Arch/Manjaro machine for {OWNER}. "
            f"Today is {datetime.date.today():%A %d %B %Y}. Working folder: {ROOT}.\n"
            "Goal: a fast, stable, hardened system, and every task done by you, not by the owner.\n\n"
            "Method:\n"
            "1. Inspect first (systemctl, journalctl, pacman -Q, ss -tulpn, df, free), then change.\n"
            "2. Run the commands yourself with the run tool. If one fails, read the error, try plan B, "
            "and keep going until the task is done or you are truly blocked.\n"
            "3. Before anything destructive or lockout-prone (rm -rf, pacman -R, partitioning, firewall, ssh, "
            "bootloader, overwriting configs) back up the target, and say what you are about to do and why.\n"
            "4. Prefer pacman and official repos; check the Arch wiki via fetch_url when unsure.\n"
            "5. Report: what you changed, how to undo it, what you verified.\n\n" + RULES)
    return (
        f"You are Jiatrix, the personal computer secretary of {OWNER}, who created you. You serve only him. "
        "Your job is to help him grow, build better systems and earn real money honestly. "
        "His role models are Jesus (character, service, humility) and Elon Musk (ambition, building, first "
        f"principles). Today is {datetime.date.today():%A %d %B %Y}. Working folder: {ROOT}.\n\n"
        "How you work:\n"
        "- Be his second brain: when he tells you something lasting about himself, his work, goals or "
        "preferences, call remember (one short factual sentence). Never invent facts about him.\n"
        "- Track his to-dos with add_task, list_tasks, done_task. Offer next actions, ranked by impact.\n"
        "- Be honest over agreeable: if an idea or plan is weak, say why and give a better one.\n"
        "- To know him better, ask ONE short question at a time, only when it matters.\n"
        "- You cannot ask anyone else for help. fetch_url is your only internet access and he approves every "
        "use, so use it only when you are sure you need it.\n"
        "- Be brief and practical.\n\n" + RULES + "\n\n"
        f"What you know about him:\n{load(NOTES)[-4000:] or '(nothing yet)'}\n\n"
        f"Open tasks:\n{open_tasks()}")

# ---------- recover tool calls the model wrote as plain text ----------
def parse_text_calls(text):
    dec, calls, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and "name" in obj and isinstance(obj.get("arguments"), dict):
            calls.append((str(obj["name"]), obj["arguments"]))
        i = end
    return calls

# ---------- conversation ----------
def _role(m): return m["role"] if isinstance(m, dict) else m.role
def _len(m): return len((m["content"] if isinstance(m, dict) else m.content) or "")

def trim(msgs, budget=int(OPTS["num_ctx"] * 1.5)):
    while sum(map(_len, msgs)) > budget and len(msgs) > 1:
        del msgs[1]
        while len(msgs) > 1 and _role(msgs[1]) != "user":
            del msgs[1]

msgs = [{"role": "system", "content": system_prompt()}]
busy = threading.Lock()

def worker(text):
    start = len(msgs)
    try:
        trim(msgs)
        start = len(msgs)
        msgs.append({"role": "user", "content": text})
        for _ in range(MAX_STEPS):
            try:
                m = ollama.chat(model=MODEL, messages=msgs, tools=TOOLS,
                                options=OPTS, keep_alive="10m", **THINK).message
            except Exception as e:
                emit({"type": "error", "text": f"ollama error: {e}"})
                del msgs[start:]
                return
            msgs.append(m)
            calls = [(c.function.name, dict(c.function.arguments)) for c in (m.tool_calls or [])]
            recovered = False
            if not calls:
                calls = parse_text_calls(m.content or "")
                recovered = bool(calls)
            if not calls:
                emit({"type": "message", "text": m.content or ""})
                return
            for name, args in calls:
                emit({"type": "tool", "name": name, "args": {k: str(v)[:200] for k, v in args.items()}})
                fn = FUNCS.get(name)
                try:
                    out = str(fn(**args)) if fn else (f"error: unknown tool '{name}'. "
                                                      f"Valid tools: {', '.join(FUNCS)}.")
                except Exception as e:
                    out = f"error: {e}"
                emit({"type": "result", "text": out[:600]})
                if recovered:
                    msgs.append({"role": "user", "content": f"[result of {name}]\n{out}"})
                else:
                    msgs.append({"role": "tool", "tool_name": name, "content": out})
                if name in MEMORY_TOOLS:
                    msgs[0]["content"] = system_prompt()
        emit({"type": "error", "text": "stopped: too many steps"})
    except Exception as e:
        emit({"type": "error", "text": f"agent error: {e}"})
        del msgs[start:]
    finally:
        emit({"type": "done"})
        busy.release()

# ---------- web server ----------
app = FastAPI(title="Jiatrix")

def check(token):
    if not secrets.compare_digest(token or "", TOKEN):
        raise HTTPException(401, "bad token")

class ChatReq(BaseModel):
    message: str

class ApproveReq(BaseModel):
    id: str
    ok: bool

@app.post("/chat")
def chat(req: ChatReq, x_token: str = Header("")):
    global Q
    check(x_token)
    if not busy.acquire(blocking=False):
        raise HTTPException(409, "agent is busy")
    Q = q = queue.Queue()
    threading.Thread(target=worker, args=(req.message,), daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            yield json.dumps(ev) + "\n"
            if ev["type"] == "done":
                break

    return StreamingResponse(gen(), media_type="application/x-ndjson")

@app.post("/approve")
def approve(req: ApproveReq, x_token: str = Header("")):
    check(x_token)
    slot = pending.get(req.id)
    if not slot:
        raise HTTPException(404, "no such approval")
    slot["ok"] = req.ok
    slot["ev"].set()
    return {"ok": True}

@app.post("/new")
def new(x_token: str = Header("")):
    check(x_token)
    if busy.locked():
        raise HTTPException(409, "agent is busy")
    msgs[:] = [{"role": "system", "content": system_prompt()}]
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "ok", "mode": MODE, "model": MODEL}

@app.get("/", response_class=HTMLResponse)
def index():
    if not STATIC.exists():
        raise HTTPException(500, f"missing {STATIC}")
    page = STATIC.read_text()
    for k, v in {"__TOKEN__": TOKEN, "__MODE__": MODE, "__MODEL__": MODEL,
                 "__ROOT__": html.escape(str(ROOT))}.items():
        page = page.replace(k, v)
    return page

if __name__ == "__main__":
    print(f"jiatrix {MODE} | {MODEL} | {ROOT}\nopen http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
