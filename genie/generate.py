"""Ask the local model (Ollama chat API) for code only, streaming the reply to a log
file so the UI can show it live. Supports cancel and timeout by closing the socket,
which also makes Ollama stop generating."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

INSERT_SYSTEM = """You add new code to an existing file in a software project.
Reply with ONLY the new code, in a single ```python block:
- first any NEW import lines the code needs (only if they are not already in the file)
- then the new function(s) or class(es)
Do not repeat, rewrite or modify any existing code. A tool inserts your code into the
file at the right place, so do not include the rest of the file.
Follow the project conventions exactly and reuse existing helpers and methods.
Call only methods that already exist."""


def generate(*, settings: dict, model: str, system: str, user: str, log_path: Path,
             timeout_s: int, cancel_path: Path | None = None) -> dict:
    name = model.split("/", 1)[1] if model.startswith("ollama") and "/" in model else model
    u = urlparse(settings["local"]["ollama_url"])
    body = json.dumps({
        "model": name,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": True,
        "options": {"num_ctx": settings["local"]["context_tokens"], "temperature": 0},
    })
    state = {"text": "", "done": False, "error": None, "prompt_tokens": None, "out_tokens": None}
    t0 = time.time()
    conn = http.client.HTTPConnection(u.hostname, u.port or 11434, timeout=timeout_s)
    log = open(log_path, "w", encoding="utf-8", buffering=1)
    log.write(f"genie insert mode: asking {name} for the new code only "
              f"(prompt ~{len(user) // 4} tokens; the model reads it before the first output appears)\n\n")

    def reader() -> None:
        try:
            conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                state["error"] = f"ollama HTTP {resp.status}: {resp.read()[:300]!r}"
                return
            for raw in resp:
                if not raw.strip():
                    continue
                msg = json.loads(raw)
                piece = msg.get("message", {}).get("content", "")
                if piece:
                    state["text"] += piece
                    log.write(piece)
                if msg.get("done"):
                    state["prompt_tokens"] = msg.get("prompt_eval_count")
                    state["out_tokens"] = msg.get("eval_count")
                    break
        except (OSError, ValueError, http.client.HTTPException) as e:
            if not state.get("stopped"):
                state["error"] = f"{type(e).__name__}: {e}"
        finally:
            state["done"] = True

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    cancelled = timed_out = False
    while not state["done"]:
        time.sleep(0.5)
        if cancel_path is not None and cancel_path.exists():
            cancelled = True
        elif time.time() - t0 > timeout_s:
            timed_out = True
        if cancelled or timed_out:
            state["stopped"] = True
            try:
                if conn.sock:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            th.join(timeout=5)
            break
    log.write("\n\n" + (f"[stopped: {'cancelled' if cancelled else 'timed out'}]" if cancelled or timed_out
                        else f"Tokens: {state['prompt_tokens']} sent, {state['out_tokens']} received.") + "\n")
    log.close()
    return {
        "ok": not (cancelled or timed_out or state["error"]),
        "text": state["text"],
        "seconds": round(time.time() - t0, 1),
        "tokens_sent": state["prompt_tokens"],
        "tokens_received": state["out_tokens"],
        "cost_usd": 0.0,
        "cancelled": cancelled,
        "timed_out": timed_out,
        "errors": [state["error"]] if state["error"] else [],
        "log": log_path.name,
    }
