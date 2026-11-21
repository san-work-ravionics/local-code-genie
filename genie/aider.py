"""Run Aider as the edit engine, non-interactively.

Genie handles context, routing and checks. Aider handles applying model edits
to files. Aider's own history files go into the run folder, not the target repo.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import ASSETS, expand

MODEL_SETTINGS = ASSETS / "model-settings.yml"


def find_aider(settings: dict) -> str | None:
    p = expand(settings["aider"]["exe"])
    if Path(p).exists():
        return p
    return shutil.which("aider")


def ollama_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def ensure_ollama(settings: dict, wait_s: int = 25) -> bool:
    url = settings["local"]["ollama_url"]
    if ollama_up(url):
        return True
    exe = expand(settings["local"]["ollama_exe"])
    if not Path(exe).exists():
        exe = shutil.which("ollama") or ""
    if not exe:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=flags)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if ollama_up(url):
            return True
        time.sleep(1)
    return False


def ollama_models(settings: dict) -> list[str]:
    import json
    try:
        with urllib.request.urlopen(settings["local"]["ollama_url"].rstrip("/") + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.loads(r.read())["models"]]
    except OSError:
        return []


_APPLIED = re.compile(r"Applied edit to (.+)")
_TOKENS = re.compile(r"Tokens: ([^\n]+?) sent, ([^\n]+?) received")
_COST = re.compile(r"Cost: \$([0-9.]+) message")


def run_aider(*, settings: dict, cwd: Path, model: str, edit_format: str, files: list[Path],
              read_files: list[Path], message: str, run_dir: Path, tag: str, map_tokens: int,
              timeout_s: int, extra_env: dict | None = None,
              cancel_path: Path | None = None) -> dict:
    exe = find_aider(settings)
    if not exe:
        return {"ok": False, "error": "aider not found", "seconds": 0}
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe, "--model", model, "--edit-format", edit_format,
        "--model-settings-file", str(MODEL_SETTINGS),
        "--map-tokens", str(map_tokens), "--subtree-only",
        "--no-auto-commits", "--no-dirty-commits", "--no-auto-lint", "--no-auto-test",
        "--no-check-update", "--no-show-model-warnings", "--no-show-release-notes",
        # --stream writes the model's output to the log as it is generated (live view in the UI)
        "--analytics-disable", "--no-pretty", "--stream", "--no-fancy-input",
        "--yes-always", "--no-gitignore",
        "--chat-history-file", str(run_dir / f"{tag}.chat.md"),
        "--input-history-file", str(run_dir / f"{tag}.input"),
        "--llm-history-file", str(run_dir / f"{tag}.llm.log"),
    ]
    for r in read_files:
        cmd += ["--read", str(r)]
    cmd += ["--message", message]
    cmd += [str(f) for f in files]

    env = os.environ.copy()
    env.setdefault("OLLAMA_API_BASE", settings["local"]["ollama_url"])
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})

    log_path = run_dir / f"{tag}.out.log"
    t0 = time.time()
    killed_after_finish = timed_out = cancelled = False
    with open(log_path, "w", encoding="utf-8", errors="replace", buffering=1) as log:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, env=env)
        finished_at = None
        while proc.poll() is None:
            time.sleep(1)
            if cancel_path is not None and cancel_path.exists():
                proc.kill()
                cancelled = True
                break
            elapsed = time.time() - t0
            if elapsed > timeout_s:
                proc.kill()
                timed_out = True
                break
            # Aider can hang after finishing in --message mode. Once it has
            # reported tokens, give it 30 s to exit, then stop it.
            if finished_at is None:
                try:
                    if "Tokens:" in log_path.read_text(encoding="utf-8", errors="replace"):
                        finished_at = time.time()
                except OSError:
                    pass
            elif time.time() - finished_at > 30:
                proc.kill()
                killed_after_finish = True
                break
        proc.wait()
    out = log_path.read_text(encoding="utf-8", errors="replace")
    tok = _TOKENS.search(out)
    cost = _COST.search(out)
    errors = [ln for ln in out.splitlines() if "Error" in ln or "Exception" in ln][:5]
    return {
        "ok": not (timed_out or cancelled),
        "cancelled": cancelled,
        "seconds": round(time.time() - t0, 1),
        "applied": sorted(set(m.strip() for m in _APPLIED.findall(out))),
        "tokens_sent": tok.group(1) if tok else None,
        "tokens_received": tok.group(2) if tok else None,
        "cost_usd": float(cost.group(1)) if cost else 0.0,
        "timed_out": timed_out,
        "killed_after_finish": killed_after_finish,
        "returncode": proc.returncode,
        "errors": errors,
        "log": log_path.name,
        "cmd": [c if c != message else "<message>" for c in cmd],
    }
