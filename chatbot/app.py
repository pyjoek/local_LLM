import json   # to turn Python dicts into JSON text (for the browser) and JSON text into dicts (from Ollama)
import os     # to read settings from environment variables (set in docker-compose.yml)

import requests                                           # to make the HTTP call to Ollama
from fastapi import FastAPI                               # the web framework that gives us routes like /chat
from fastapi.responses import FileResponse, StreamingResponse
#   FileResponse      = send a file (your index.html) to the browser
#   StreamingResponse = send a response piece by piece instead of all at once
from pydantic import BaseModel, Field                     # describes and VALIDATES the data the browser sends us

# ----------------------------------------------------------------------------------------------- settings
# os.getenv("NAME", "default") means: use the environment variable NAME if it is set, else the default.
# That is why you can change behaviour in docker-compose.yml without touching this code.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")   # where Ollama listens; "ollama" is its compose service name
MODEL = os.getenv("MODEL", "qwen2.5:1.5b")                    # which model to use (must already be pulled in Ollama)
NUM_CTX = int(os.getenv("NUM_CTX", "4096"))                   # context window: how many tokens the model can read at once
#   (history + your question + its answer must fit in this. Bigger = can remember more, but slower and uses more RAM.)
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))          # 0 = predictable and repeatable, 1 = more varied/creative
# There is no "think" setting on purpose: qwen2.5 has no thinking mode, so we never send that option.

# The system prompt is the instruction the model reads BEFORE the conversation. This is where your rules and limits go.
# Change it here, or set SYSTEM_PROMPT in docker-compose.yml.
SYSTEM = os.getenv("SYSTEM_PROMPT", "You are a helpful, concise assistant.")

app = FastAPI()   # creates the web app; the decorators below (@app.post, @app.get) register its URLs


# ----------------------------------------------------------------------------------------------- input format
class ChatReq(BaseModel):
    """The JSON the browser must send to /chat. FastAPI checks it and rejects bad input with an error automatically."""
    message: str                                   # the new question (required)
    history: list[dict] = Field(default_factory=list)
    #   earlier turns, like [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}].
    #   default_factory=list gives every request its OWN fresh empty list (a plain "= []" would be shared between requests).


def line(obj):
    """Turn a dict into one line of JSON. The trailing newline is what lets the browser split the stream into messages."""
    return json.dumps(obj) + "\n"


# ----------------------------------------------------------------------------------------------- the core logic
def stream(req: ChatReq):
    """A generator: each `yield` sends one piece to the browser immediately, without waiting for the full answer."""

    # Build the conversation in the format Ollama expects: a list of {"role", "content"} messages.
    messages = [{"role": "system", "content": SYSTEM}]          # 1st: the rules
    for m in req.history[-20:]:                                 # then the last 20 turns only, so the prompt stays small and fast
        # Accept only well-formed turns. Ignoring other roles stops a browser from sneaking in its own "system" message.
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})   # last: the new question

    try:
        # `with` closes the connection to Ollama when we are done, even if something fails.
        with requests.post(
            f"{OLLAMA_URL}/api/chat",                           # Ollama's chat endpoint
            json={
                "model": MODEL,
                "messages": messages,
                "stream": True,                                 # ask Ollama to send tokens as they are produced
                "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE},   # per-request model settings
            },
            stream=True,                                        # tell `requests` NOT to wait for the whole response body
            timeout=900,                                        # give up after 15 min (a small model on CPU can be slow)
        ) as r:
            if r.status_code != 200:                            # e.g. 404 = the model has not been pulled yet
                yield line({"error": f"Ollama {r.status_code}: {r.text[:200]} "
                                     f"(did you run: docker compose exec ollama ollama pull {MODEL}?)"})
                return                                          # stop here; the error has been sent to the browser

            for raw in r.iter_lines():                          # Ollama sends one JSON object per line; read them as they arrive
                if not raw:                                     # skip blank keep-alive lines
                    continue
                d = json.loads(raw)                             # bytes of JSON text -> Python dict
                tok = d.get("message", {}).get("content", "")   # the new piece of text; .get() avoids crashing if a key is missing
                if tok:
                    yield line({"token": tok})                  # forward it to the browser right away
                if d.get("done"):                               # Ollama marks its last message with done = true
                    break                                       # stop reading

    except requests.RequestException as e:
        # Ollama is down, the name is wrong, the network failed, or the request timed out. Tell the user instead of crashing.
        yield line({"error": f"Cannot reach Ollama at {OLLAMA_URL}: {e}"})


# ----------------------------------------------------------------------------------------------- the URLs
@app.post("/chat")                                  # the browser calls this when you press Send
def chat(req: ChatReq):                             # FastAPI turns the incoming JSON into a ChatReq for us
    return StreamingResponse(stream(req), media_type="application/x-ndjson")
    #   StreamingResponse pulls from the generator above and sends each yielded line as soon as it exists.


@app.get("/")                                       # opening http://localhost:PORT/ in the browser
def index():
    # Build the path to static/index.html relative to THIS file, so it works wherever the app is run from.
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"))


@app.get("/health")                                 # a quick "is it alive, and which model?" check: curl localhost:PORT/health
def health():
    return {"status": "ok", "model": MODEL}
