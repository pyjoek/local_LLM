const $ = id => document.getElementById(id);
const log = $("log"), q = $("q"), b = $("b");
let history = [];

function add(cls, text) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.textContent = text;
  log.append(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

async function send() {
  const message = q.value.trim();
  if (!message || b.disabled) return;
  q.value = "";
  b.disabled = true;

  add("user", message);
  const reply = add("bot muted", "Thinking…");
  let answer = "";

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history })
    });
    if (!res.ok) throw new Error(await res.text() || "Request failed");

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    const handle = line => {
      if (!line.trim()) return;
      const d = JSON.parse(line);
      if (d.error) throw new Error(d.error);
      if (d.token) {
        answer += d.token;
        reply.className = "msg bot";
        reply.textContent = answer;
        log.scrollTop = log.scrollHeight;
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      lines.forEach(handle);
    }
    handle(buf);

    if (!answer) reply.textContent = "(No response)";
    history.push({ role: "user", content: message }, { role: "assistant", content: answer });
  } catch (e) {
    reply.className = "msg bot err";
    reply.textContent = (answer ? answer + "\n\n" : "") + "Error: " + e.message;
  } finally {
    b.disabled = false;
    q.focus();
  }
}

$("f").addEventListener("submit", e => { e.preventDefault(); send(); });
q.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("clear").addEventListener("click", () => { history = []; log.innerHTML = ""; });
