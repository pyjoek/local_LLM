import json
import os

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434") # local ollama serving url
MODEL = os.getenv("MODEL", "qwen2.5:1.5b") # model to be used
NUM_CTX = int(os.getenv("NUM_CTX", "4096"))
THINK = os.getenv("THINK", "0") == "0"  # "0" skips slow hidden reasoning
SYSTEM = os.getenv("SYSTEM_PROMPT", "You are a helpful, concise assistant.") # This is where you set the restriction and limitations

app = FastAPI() # initiating fast api


class ChatReq(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)


def sse(obj):
    return json.dumps(obj) + "\n"


def stream(req: ChatReq):
    messages = [{"role": "system", "content": SYSTEM}]
    for m in req.history[-20:]:
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
                "think": THINK,
                "options": {"num_ctx": NUM_CTX},
            },
            stream=,
            timeout=900,
        ) as r:
            if r.status_code != 200:
                yield sse({"error": f"Ollama {r.status_code}: {r.text[:200]}"})
                return
            for raw in r.iter_lines():
                if not raw:
                    continue
                d = json.loads(raw)
                tok = d.get("message", {}).get("content", "")
                if tok:
                    yield sse({"token": tok})
                if d.get("done"):
                    break
    except requests.RequestException as e:
        yield sse({"error": f"Cannot reach Ollama at {OLLAMA_URL}: {e}"})


@app.post("/chat")
def chat(req: ChatReq):
    return StreamingResponse(stream(req), media_type="application/x-ndjson")


@app.get("/")
def index():
    return FileResponse(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
    )


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}
