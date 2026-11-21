"""Local dashboard: a stdlib HTTP server and one static page. It binds to 127.0.0.1 only."""

from __future__ import annotations

import json
import re
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import load_target, target_names, current_target_name
from ..runlog import Run, list_runs

STATIC = Path(__file__).resolve().parent / "index.html"
RUN_ID = re.compile(r"^[A-Za-z0-9-]{1,120}$")


def _summary(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "task", "created", "status", "route", "planned_route",
                                  "coverage", "editable", "escalated")} | {
        "seconds": d.get("result", {}).get("seconds"),
        "cost": d.get("result", {}).get("remote_cost_usd"),
        "attempts": len(d.get("attempts", [])),
        "diff_stat": d.get("result", {}).get("diff_stat"),
    }


def stats(runs: list[dict]) -> dict:
    executed = [r for r in runs if r.get("status") not in {"planned", "running"}]
    status = Counter(r.get("status") for r in runs)
    routes = Counter(r.get("route") for r in executed if r.get("attempts"))
    reasons: Counter = Counter()
    for r in runs:
        for x in r.get("escalation_reasons", []):
            reasons[x.split(":")[0].split(" (")[0]] += 1
        if r.get("escalated"):
            reasons["failed local checks"] += 1
    checks_failed: Counter = Counter()
    by_kind: dict[str, list[float]] = {"local": [], "remote": []}
    passes: Counter = Counter()
    tries: Counter = Counter()
    for r in runs:
        for a in r.get("attempts", []):
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

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                body = STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
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
                                "current": n == current_target_name()})
                self._json(out)
            elif u.path == "/api/runs":
                t = self._target(q)
                runs = list_runs(t)
                self._json({"runs": [_summary(r) for r in runs], "stats": stats(runs),
                            "costs": self.costs})
            elif u.path == "/api/run":
                t = self._target(q)
                rid = (q.get("id") or [""])[0]
                if not RUN_ID.match(rid):
                    return self._json({"error": "bad id"}, 400)
                run = Run.load(t, rid)
                extra = {}
                for name in ("diff.patch", "context.md"):
                    p = run.dir / name
                    extra[name] = p.read_text(encoding="utf-8") if p.exists() else None
                self._json({"run": run.data, "files": extra, "costs": self.costs})
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
            if not RUN_ID.match(body.get("id", "")):
                return self._json({"error": "bad id"}, 400)
            run = Run.load(t, body["id"])
            if u.path == "/api/accept":
                self._json({"message": accept(t, run)})
            elif u.path == "/api/reject":
                self._json({"message": reject(t, run)})
            else:
                self._json({"error": "not found"}, 404)
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
    print(f"genie UI on {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
