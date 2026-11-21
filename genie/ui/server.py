"""Local dashboard: a stdlib HTTP server and one static page. It binds to 127.0.0.1 only.

Tasks started from the UI run as separate `genie task` processes, so a run
survives the UI being closed, and the router's cleanup (switching the target
back to your branch) always runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import GENIE_HOME, current_target_name, load_target, target_names
from ..runlog import Run, RunLock, effective_status, list_runs, new_run_id

STATIC = Path(__file__).resolve().parent / "index.html"
RUN_ID = re.compile(r"^[A-Za-z0-9-]{1,120}$")
MAX_TASK_CHARS = 4000


def _summary(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "task", "created", "route", "planned_route",
                                  "coverage", "editable", "escalated")} | {
        "status": effective_status(d),
        "seconds": d.get("result", {}).get("seconds"),
        "cost": d.get("result", {}).get("remote_cost_usd"),
        "attempts": len(d.get("attempts", [])),
        "diff_stat": d.get("result", {}).get("diff_stat"),
    }


def stats(runs: list[dict]) -> dict:
    executed = [r for r in runs if r.get("status") not in {"planned", "running"}]
    status = Counter(effective_status(r) for r in runs)
    routes = Counter(r.get("route") for r in executed if r.get("attempts"))
    reasons: Counter = Counter()
    for r in runs:
        if r.get("status") == "planned":
            continue
        for x in r.get("escalation_reasons", []):
            reasons[x.split(":")[0].split(" (")[0]] += 1
        if r.get("escalated"):
            reasons["failed local checks"] += 1
    checks_failed: Counter = Counter()
    by_kind: dict[str, list[float]] = {}
    passes: Counter = Counter()
    tries: Counter = Counter()
    for r in runs:
        for a in r.get("attempts", []):
            if a.get("verdict") == "cancelled":
                continue
            tries[a["kind"]] += 1
            if a.get("verdict") == "pass":
                passes[a["kind"]] += 1
            by_kind.setdefault(a["kind"], []).append(a.get("aider", {}).get("seconds") or 0)
            for c in a.get("checks", []):
                if c["status"] in {"fail", "blocked"}:
                    checks_failed[c["id"]] += 1
    solved = [r for r in runs if r.get("status") in {"needs_review", "accepted"}]
    return {
        "total": len(runs),
        "status": dict(status),
        "routes": dict(routes),
        "solved_local": sum(1 for r in solved if r.get("route") == "local"),
        "solved_remote": sum(1 for r in solved if r.get("route") in {"remote", "escalated"}),
        "escalation_reasons": dict(reasons.most_common(8)),
        "checks_failed": dict(checks_failed.most_common()),
        "attempt_pass_rate": {k: f"{passes[k]}/{tries[k]}" for k in tries},
        "avg_seconds": {k: round(sum(v) / len(v), 1) for k, v in by_kind.items() if v},
        "remote_cost_usd": round(sum(r.get("result", {}).get("remote_cost_usd") or 0 for r in runs), 4),
    }


def _tail(p: Path, n: int = 8000) -> str | None:
    if not p.exists():
        return None
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read().decode("utf-8", errors="replace")


def spawn_task(target_name: str, body: dict) -> str:
    """Start `genie task` as its own process and return the run id it will use."""
    t = load_target(target_name)
    task = str(body.get("task", "")).strip()
    if not task or len(task) > MAX_TASK_CHARS:
        raise ValueError("task is empty or too long")
    rid = new_run_id(task)
    args = [sys.executable, "-m", "genie", "--target", target_name, "task", task, "--run-id", rid]
    for f in body.get("files") or []:
        f = str(f).strip()
        if f:
            args += ["--file", f]
    for flag in ("dry_run", "local_only", "remote", "fast"):
        if body.get(flag):
            args.append("--" + flag.replace("_", "-"))
    if str(body.get("test") or "").strip():
        args += ["--test", str(body["test"]).strip()]
    run_dir = t.runs_dir / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(GENIE_HOME) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    out = open(run_dir / "genie.out.log", "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=GENIE_HOME, stdin=subprocess.DEVNULL, stdout=out,
                            stderr=subprocess.STDOUT, env=env, creationflags=flags)
    out.close()
    if not body.get("dry_run"):
        RunLock(t).claim(proc.pid)   # close the gap before the child takes the lock itself
    return rid


_SPAWN_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "genie-ui"
    costs = {"currency": "EUR", "usd_to_eur": 0.92}

    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _target(self, q: dict):
        name = (q.get("target") or [None])[0] or current_target_name()
        if not name or name not in target_names():
            raise ValueError("unknown target")
        return load_target(name)

    def _run(self, t, rid: str) -> Run:
        if not RUN_ID.match(rid or ""):
            raise ValueError("bad id")
        return Run.load(t, rid)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                body = STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/targets":
                out = []
                for n in target_names():
                    t = load_target(n)
                    idx = None
                    if t.index_path.exists():
                        head = t.index_path.read_text(encoding="utf-8")[:400]
                        m = re.search(r'"built_at": "([^"]+)"', head)
                        idx = m.group(1) if m else "yes"
                    out.append({"name": n, "root": str(t.root), "indexed": idx,
                                "current": n == current_target_name(),
                                "busy": bool(RunLock(t).holder())})
                self._json(out)
            elif u.path == "/api/runs":
                t = self._target(q)
                runs = list_runs(t)
                self._json({"runs": [_summary(r) for r in runs], "stats": stats(runs),
                            "costs": self.costs, "busy": bool(RunLock(t).holder())})
            elif u.path == "/api/run":
                t = self._target(q)
                rid = (q.get("id") or [""])[0]
                if not RUN_ID.match(rid):
                    return self._json({"error": "bad id"}, 400)
                if not (t.runs_dir / rid / "run.json").exists():
                    return self._json({"starting": True, "id": rid,
                                       "spawn_log": _tail(t.runs_dir / rid / "genie.out.log")})
                run = Run.load(t, rid)
                data = dict(run.data)
                data["status"] = effective_status(run.data)
                extra = {}
                for name in ("diff.patch", "context.md"):
                    p = run.dir / name
                    extra[name] = p.read_text(encoding="utf-8") if p.exists() else None
                self._json({"run": data, "files": extra, "costs": self.costs})
            elif u.path == "/api/log":
                t = self._target(q)
                rid = (q.get("id") or [""])[0]
                if not RUN_ID.match(rid):
                    return self._json({"error": "bad id"}, 400)
                d = t.runs_dir / rid
                logs = sorted(d.glob("attempt*.out.log"), key=lambda p: p.stat().st_mtime) if d.exists() else []
                self._json({"attempt_log": logs[-1].name if logs else None,
                            "text": _tail(logs[-1]) if logs else None,
                            "spawn_log": _tail(d / "genie.out.log", 3000)})
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, SystemExit) as e:
            self._json({"error": str(e)}, 400)

    def do_POST(self):  # noqa: N802
        from ..router import accept, reject
        u = urlparse(self.path)
        if self.headers.get("X-Genie") != "1":
            return self._json({"error": "missing X-Genie header"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            t = self._target({"target": [body.get("target")]})
            if u.path == "/api/task":
                with _SPAWN_LOCK:
                    if not body.get("dry_run") and RunLock(t).holder():
                        return self._json({"error": "A run is already in progress for this target"}, 409)
                    return self._json({"id": spawn_task(t.name, body)})
            run = self._run(t, body.get("id", ""))
            if u.path == "/api/accept":
                self._json({"message": accept(t, run)})
            elif u.path == "/api/reject":
                self._json({"message": reject(t, run)})
            elif u.path == "/api/cancel":
                (run.dir / "cancel").write_text("cancel", encoding="utf-8")
                self._json({"message": "Cancelling: the current attempt stops within a few seconds, "
                                       "then genie switches the repo back to your branch."})
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except SystemExit as e:
            self._json({"error": str(e)}, 409)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def serve(settings: dict, port: int | None = None, open_browser: bool = True) -> None:
    host = settings["ui"]["host"]
    port = port or settings["ui"]["port"]
    Handler.costs = dict(settings["costs"])
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"genie UI on {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
