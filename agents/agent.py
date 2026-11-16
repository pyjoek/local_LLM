#!/usr/bin/env python3
"""Jiatrix web UI. Run from the folder you want the agent to work in:

  python jiatrix_web.py [secretary|dev|sysadmin]

Env: AGENT_HOST (default 127.0.0.1), AGENT_PORT (default 8765), AGENT_TOKEN, OLLAMA_HOST
Needs: pip install fastapi uvicorn   (ollama is already installed)
"""
import os, sys, json, uuid, html, queue, secrets, threading

if len(sys.argv) > 1:                              # let jiatrix.py read the mode from env
    os.environ["AGENT_MODE"] = sys.argv[1]
sys.argv = sys.argv[:1]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ollama, uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import jiatrix as J

HOST = os.getenv("AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("AGENT_PORT", "8765"))
TOKEN = os.getenv("AGENT_TOKEN") or secrets.token_urlsafe(16)
APPROVAL_TIMEOUT = 300

app = FastAPI(title="Jiatrix")
msgs = [{"role": "system", "content": J.system_prompt()}]
busy = threading.Lock()
pending = {}                                       # approval id -> {"ev": Event, "ok": bool}
Q = queue.Queue()                                  # events for the turn currently running


def emit(obj):
    Q.put(obj)


def web_confirm(msg):
    """Replaces the terminal y/N prompt: ask the browser and wait."""
    aid = uuid.uuid4().hex[:8]
    slot = {"ev": threading.Event(), "ok": False}
    pending[aid] = slot
    emit({"type": "approval", "id": aid, "text": msg})
    slot["ev"].wait(APPROVAL_TIMEOUT)              # no answer = deny
    pending.pop(aid, None)
    return slot["ok"]


J.confirm = web_confirm                            # tools call confirm() through the module global


def worker(text):
    start = len(msgs)
    try:
        J.trim(msgs)
        start = len(msgs)
        msgs.append({"role": "user", "content": text})
        for _ in range(J.MAX_STEPS):
            try:
                m = ollama.chat(model=J.MODEL, messages=msgs, tools=J.TOOLS,
                                options=J.OPTS, keep_alive="10m", **J.THINK).message
            except Exception as e:
                emit({"type": "error", "text": f"ollama error: {e}"})
                del msgs[start:]
                return
            msgs.append(m)
            calls = [(c.function.name, dict(c.function.arguments)) for c in (m.tool_calls or [])]
            recovered = False
            if not calls:
                calls = J.parse_text_calls(m.content or "")
                recovered = bool(calls)
            if not calls:
                emit({"type": "message", "text": m.content or ""})
                return
            for name, args in calls:
                emit({"type": "tool", "name": name, "args": {k: str(v)[:200] for k, v in args.items()}})
                fn = J.FUNCS.get(name)
                try:
                    out = str(fn(**args)) if fn else (f"error: unknown tool '{name}'. "
                                                      f"Valid tools: {', '.join(J.FUNCS)}.")
                except Exception as e:
                    out = f"error: {e}"
                emit({"type": "result", "text": out[:600]})
                if recovered:
                    msgs.append({"role": "user", "content": f"[result of {name}]\n{out}"})
                else:
                    msgs.append({"role": "tool", "tool_name": name, "content": out})
                if name in J.MEMORY_TOOLS:
                    msgs[0]["content"] = J.system_prompt()
        emit({"type": "error", "text": "stopped: too many steps"})
    except Exception as e:
        emit({"type": "error", "text": f"agent error: {e}"})
        del msgs[start:]
    finally:
        emit({"type": "done"})
        busy.release()


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
    msgs[:] = [{"role": "system", "content": J.system_prompt()}]
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok", "mode": J.MODE, "model": J.MODEL}


@app.get("/", response_class=HTMLResponse)
def index():
    page = PAGE
    for k, v in {"__TOKEN__": TOKEN, "__MODE__": J.MODE, "__MODEL__": J.MODEL,
                 "__ROOT__": html.escape(str(J.ROOT))}.items():
        page = page.replace(k, v)
    return page


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jiatrix</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e8ee;--dim:#8b93a5;--acc:#6ea8fe;--ok:#3fb950;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center}
header b{color:var(--acc)} header span{color:var(--dim);font-size:13px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
button{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--acc)} button:disabled{opacity:.5;cursor:default}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.m{max-width:820px;padding:10px 14px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.me{align-self:flex-end;background:#243049}
.bot{background:var(--panel);border:1px solid var(--line)}
.tool{color:var(--acc);font:13px ui-monospace,monospace;padding:2px 14px}
.res{color:var(--dim);font:12px ui-monospace,monospace;padding:2px 14px;max-height:120px;overflow:auto}
.err{background:#3a1a1a;border:1px solid var(--bad)}
.ask{background:var(--panel);border:1px solid #d29922}
.ask pre{margin:0 0 10px;white-space:pre-wrap;font:13px ui-monospace,monospace;max-height:260px;overflow:auto}
.ask button{margin-right:8px}.ask.yes{border-color:var(--ok)}.ask.no{border-color:var(--bad)}
form{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line)}
textarea{flex:1;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px;font:inherit;resize:none;height:48px}
</style></head><body>
<header><b>Jiatrix</b><span>__MODE__ · __MODEL__ · __ROOT__</span><button id="new">New chat</button></header>
<div id="log"></div>
<form id="f"><textarea id="box" placeholder="Message (Enter sends, Shift+Enter newline)" autofocus></textarea>
<button id="send">Send</button></form>
<script>
const T="__TOKEN__", log=document.getElementById('log'), box=document.getElementById('box'),
      send=document.getElementById('send');
const post=(p,b)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/json','X-Token':T},body:JSON.stringify(b||{})});
function add(cls,text){const d=document.createElement('div');d.className='m '+cls;d.textContent=text;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
function approval(ev){
  const d=document.createElement('div');d.className='m ask';
  const p=document.createElement('pre');p.textContent=ev.text.trim();
  const a=document.createElement('button'),n=document.createElement('button');
  a.textContent='Approve';n.textContent='Deny';
  const done=ok=>{a.disabled=n.disabled=true;d.classList.add(ok?'yes':'no');post('/approve',{id:ev.id,ok})};
  a.onclick=()=>done(true);n.onclick=()=>done(false);
  d.append(p,a,n);log.appendChild(d);log.scrollTop=log.scrollHeight}
function handle(ev){
  if(ev.type==='tool')add('tool','→ '+ev.name+' '+JSON.stringify(ev.args));
  else if(ev.type==='result')add('res',ev.text);
  else if(ev.type==='approval')approval(ev);
  else if(ev.type==='message')add('bot',ev.text);
  else if(ev.type==='error')add('err',ev.text)}
async function ask(){
  const text=box.value.trim();if(!text||send.disabled)return;
  box.value='';add('me',text);send.disabled=true;
  try{
    const r=await post('/chat',{message:text});
    if(!r.ok){add('err','Error '+r.status+(r.status===409?' (agent busy)':''));return}
    const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
    while(true){const {value,done}=await rd.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+1);
        if(line.trim())handle(JSON.parse(line))}}
  }catch(e){add('err',String(e))}
  finally{send.disabled=false;box.focus()}}
document.getElementById('f').onsubmit=e=>{e.preventDefault();ask()};
box.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask()}};
document.getElementById('new').onclick=async()=>{const r=await post('/new');
  if(r.ok)log.innerHTML='';else add('err','Cannot reset while busy')};
</script></body></html>
"""


if __name__ == "__main__":
    print(f"jiatrix web | {J.MODE} | {J.MODEL} | {J.ROOT}\nopen http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
