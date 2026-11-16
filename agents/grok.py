#!/usr/bin/env python3
"""Jiatrix Secretary: Joel's personal terminal assistant.
Remembers notes and tasks between sessions, works inside the folder you launch it from."""
import os, re, difflib, pathlib, subprocess, datetime, urllib.request, ollama

MODEL = os.getenv("AGENT_MODEL", "qwen2.5:1.5b")
ROOT = pathlib.Path.cwd().resolve()
HOME = pathlib.Path.home() / ".grok_memory"            # memory lives here, outside any project
NOTES, TASKS = HOME / "notes.md", HOME / "tasks.md"
OPTS = {"num_ctx": int(os.getenv("AGENT_CTX", "8192")), "temperature": 0}
THINK = {"think": False} if os.getenv("AGENT_NOTHINK") else {}   # set for qwen3 to skip slow "thinking"
SKIP = {".git", "node_modules", "vendor", "storage", "__pycache__", ".venv"}
MAX_STEPS = 8
SEEN = {}                                          # path -> how many lines were read from the top
MEMORY_TOOLS = {"remember", "add_task", "done_task"}

# ---------- safety helpers ----------
def safe(p):
    p = (ROOT / p).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError("outside working folder")
    return p

def guard_git(p):
    if ".git" in p.relative_to(ROOT).parts:
        raise ValueError("refusing to touch .git")

def confirm(msg):
    return input(f"\n⚠ {msg} [y/N] ").strip().lower() == "y"

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
    p = safe(path)
    lines = p.read_text(errors="replace").splitlines()
    s, e = max(1, int(start)), min(int(end), len(lines))
    out, used = [], 0
    for i in range(s, e + 1):
        row = f"{i}: {lines[i - 1]}"
        if used + len(row) > 6000:
            e = i - 1
            break
        out.append(row)
        used += len(row) + 1
    if s <= SEEN.get(p, 0) + 1:                    # only count contiguous reading from the top
        SEEN[p] = max(SEEN.get(p, 0), e)
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
    SEEN[p] = 0                                    # line numbers changed, force a re-read before overwrite
    return "edited"

def write_file(path, content):
    p = safe(path); guard_git(p)
    old = p.read_text(errors="replace") if p.exists() else None
    if old is not None and SEEN.get(p, -1) < len(old.splitlines()):
        return "refused: file exists and was not fully read. Use edit_file for changes."
    verb = "Overwrite" if old is not None else "Create"
    if not confirm(f"{verb} {path}\n{preview(old or '', content, path)}"):
        return "user denied"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    SEEN[p] = len(content.splitlines())
    return "written"

def run(command):
    if not confirm(f"Run: {command}"):
        return "user denied"
    try:
        r = subprocess.run(command, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "error: timed out after 120s"
    return f"exit {r.returncode}\n" + ((r.stdout + r.stderr)[-3000:] or "(no output)")

# ---------- secretary tools: memory, tasks, internet ----------
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
    html = urllib.request.urlopen(req, timeout=15).read(200000).decode(errors="replace")
    html = re.sub(r"(?s)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:4000]

# ---------- tool specs ----------
def spec(fn, desc, props, required):
    return {"type": "function", "function": {"name": fn.__name__, "description": desc,
            "parameters": {"type": "object",
                           "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
                           "required": required}}}

FUNCS = {f.__name__: f for f in (list_dir, read_file, search, edit_file, write_file, run,
                                 remember, add_task, list_tasks, done_task, fetch_url)}
TOOLS = [
    spec(list_dir, "List files in a folder", {"path": "folder, default .", "depth": "levels deep, default 1"}, []),
    spec(read_file, "Read a text file with line numbers, 200 lines by default",
         {"path": "file path", "start": "first line, default 1", "end": "last line, default 200"}, ["path"]),
    spec(search, "Grep for text in files", {"pattern": "text to find", "path": "folder, default ."}, ["pattern"]),
    spec(edit_file, "Replace one exact snippet in an existing file. Preferred way to change files. "
                    "old must match exactly once, copied WITHOUT line-number prefixes",
         {"path": "file path", "old": "exact text to replace", "new": "replacement text"}, ["path", "old", "new"]),
    spec(write_file, "Create a NEW file (or overwrite one that was fully read)",
         {"path": "file path", "content": "full file content"}, ["path", "content"]),
    spec(run, "Run a shell command in the working folder", {"command": "shell command"}, ["command"]),
    spec(remember, "Save a lasting fact about Joel, his work, goals or preferences",
         {"note": "one short factual sentence"}, ["note"]),
    spec(add_task, "Add a to-do item", {"task": "what to do"}, ["task"]),
    spec(list_tasks, "Show all to-dos with numbers", {}, []),
    spec(done_task, "Mark a to-do done", {"number": "task number from list_tasks"}, ["number"]),
    spec(fetch_url, "Fetch a web page as text. Needs Joel's approval each time", {"url": "http(s) url"}, ["url"]),
]

def system_prompt():
    return (
        "You are Jiatrix, the personal computer secretary of Joel (pyjoek), who created you. "
        "You serve only him. Your job is to help him grow, build better systems and make real money, honestly. "
        "His role models are Jesus (character, service, humility) and Elon Musk (ambition, building, first principles). "
        f"Today is {datetime.date.today():%A %d %B %Y}. Working folder: {ROOT}.\n\n"
        "How you work:\n"
        "- Use tools instead of guessing. List or search before reading; read big files in line ranges.\n"
        "- To change an existing file use edit_file. write_file is for new files.\n"
        "- When Joel tells you something lasting about himself, his work, goals or preferences, call remember. "
        "Never invent facts about him.\n"
        "- Track his to-dos with add_task, list_tasks, done_task.\n"
        "- When you need to know him better, ask ONE short question at a time, only when it matters.\n"
        "- You cannot ask anyone else for help, ask Joel directly. fetch_url is your only internet access; "
        "he approves every use, so ask for it only when you are sure you need it.\n"
        "- Be brief and practical.\n\n"
        f"What you know about Joel:\n{load(NOTES)[-4000:] or '(nothing yet)'}\n\n"
        f"Open tasks:\n{open_tasks()}"
    )

# ---------- conversation loop ----------
def _role(m): return m["role"] if isinstance(m, dict) else m.role
def _len(m): return len((m["content"] if isinstance(m, dict) else m.content) or "")

def trim(msgs, budget=int(OPTS["num_ctx"] * 1.5)):
    """Drop the oldest whole turns when history gets too big for the context window."""
    while sum(map(_len, msgs)) > budget and len(msgs) > 1:
        del msgs[1]
        while len(msgs) > 1 and _role(msgs[1]) != "user":
            del msgs[1]

def main():
    fresh = lambda: [{"role": "system", "content": system_prompt()}]
    msgs = fresh()
    print(f"jiatrix secretary | {ROOT} | {MODEL} | /new /notes /tasks, ctrl-c quits")
    while True:
        try:
            q = input("\n› ").strip()
        except (KeyboardInterrupt, EOFError):
            print(); break
        if q == "/new":
            msgs = fresh(); continue
        if q == "/notes":
            print(load(NOTES) or "no notes yet"); continue
        if q == "/tasks":
            print(list_tasks()); continue
        if not q:
            continue
        trim(msgs)
        start = len(msgs)                          # rollback point if the turn fails
        msgs.append({"role": "user", "content": q})
        try:
            for _ in range(MAX_STEPS):
                try:
                    m = ollama.chat(model=MODEL, messages=msgs, tools=TOOLS,
                                    options=OPTS, keep_alive="10m", **THINK).message
                except Exception as e:
                    print(f"ollama error: {e}"); del msgs[start:]; break
                msgs.append(m)
                if not m.tool_calls:
                    print("\n" + (m.content or "")); break
                for c in m.tool_calls:
                    name, args = c.function.name, dict(c.function.arguments)
                    print("  →", name, {k: str(v)[:50] for k, v in args.items()})
                    fn = FUNCS.get(name)
                    try:
                        out = str(fn(**args)) if fn else f"error: unknown tool {name}"
                    except Exception as e:
                        out = f"error: {e}"
                    msgs.append({"role": "tool", "tool_name": name, "content": out})
                    if name in MEMORY_TOOLS:       # refresh what the model knows right away
                        msgs[0]["content"] = system_prompt()
            else:
                print("stopped: too many steps")
        except KeyboardInterrupt:
            print("\ninterrupted"); del msgs[start:]

if __name__ == "__main__":
    main()
