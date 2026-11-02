"""Simple chatbot: web page -> this FastAPI app -> Ollama (qwen2.5:1.5b).
Replies stream back one JSON object per line:  {"token": "..."}  or  {"error": "..."}  (same format as before)."""
import json
import os

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")   # where Ollama is listening
MODEL = os.getenv("MODEL", "qwen2.5:1.5b")                    # the model to use (must be pulled in Ollama)
NUM_CTX = int(os.getenv("NUM_CTX", "4096"))                   # how much text the model can read at once
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))          # 0 = predictable, 1 = more creative
# No "think" setting: qwen2.5 has no thinking mode, so we never send that option.

# This is where you set the rules and limitations. Change it here, or via the SYSTEM_PROMPT environment variable.
SYSTEM = os.getenv("SYSTEM_PROMPT", "You are a helpful, concise assistant.")

app = FastAPI()


class ChatReq(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)


def line(obj):
    """One JSON object per line (newline-delimited JSON)."""
    return json.dumps(obj) + "\n"


def stream(req: ChatReq):
    messages = [{"role": "system", "content": SYSTEM}]
    for m in req.history[-20:]:                                   # only the last 20 turns, to keep it fast
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    try:
        with requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": messages,
                "stream": True,
                "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE},
            },
            stream=True,
            timeout=900,
        ) as r:
            if r.status_code != 200:
                yield line({"error": f"Ollama {r.status_code}: {r.text[:200]} "
                                     f"(did you run: docker compose exec ollama ollama pull {MODEL}?)"})
                return
            for raw in r.iter_lines():
                if not raw:
                    continue
                d = json.loads(raw)
                tok = d.get("message", {}).get("content", "")
                if tok:
                    yield line({"token": tok})
                if d.get("done"):
                    break
    except requests.RequestException as e:
        yield line({"error": f"Cannot reach Ollama at {OLLAMA_URL}: {e}"})


@app.post("/chat")
def chat(req: ChatReq):
    return StreamingResponse(stream(req), media_type="application/x-ndjson")


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}
