"""Run records: one folder per task run, holding run.json and its artifacts.

The UI reads these folders, so writes are atomic (temp file + replace).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .config import Target


def new_run_id(task: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:32].strip("-") or "task"
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex() + "-" + slug


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Run:
    def __init__(self, target: Target, data: dict):
        self.target = target
        self.data = data

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def dir(self) -> Path:
        return self.target.runs_dir / self.id

    @classmethod
    def create(cls, target: Target, task: str, options: dict, rid: str | None = None) -> "Run":
        rid = rid or new_run_id(task)
        run = cls(target, {"id": rid, "target": target.name, "task": task, "created": now(),
                           "status": "running", "route": None, "options": options,
                           "pid": os.getpid(),
                           "steps": [], "attempts": [], "decisions": [], "result": {}})
        run.dir.mkdir(parents=True, exist_ok=True)
        run.save()
        return run

    @classmethod
    def load(cls, target: Target, rid: str) -> "Run":
        p = target.runs_dir / rid / "run.json"
        if not p.exists():
            matches = [d.name for d in target.runs_dir.glob(f"*{rid}*")] if target.runs_dir.exists() else []
            if len(matches) == 1:
                p = target.runs_dir / matches[0] / "run.json"
            else:
                raise SystemExit(f"Run '{rid}' not found" + (f" (ambiguous: {matches})" if matches else ""))
        return cls(target, json.loads(p.read_text(encoding="utf-8")))

    def save(self) -> None:
        p = self.dir / "run.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def step(self, name: str, status: str = "running", detail: str = "") -> dict:
        s = {"name": name, "status": status, "detail": detail, "started": now(), "ended": None,
             "t0": time.time()}
        self.data["steps"].append(s)
        self.save()
        return s

    def end_step(self, s: dict, status: str = "done", detail: str | None = None) -> None:
        s["status"] = status
        s["ended"] = now()
        s["seconds"] = round(time.time() - s.pop("t0", time.time()), 1)
        if detail is not None:
            s["detail"] = detail
        self.save()

    def write(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p


def pid_alive(pid: int | None) -> bool:
    """True if a process with this pid is running. Never signals the process."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class RunLock:
    """One real run per target at a time (each run switches branches in the target repo)."""

    def __init__(self, target: Target):
        self.path = target.ws / ".lock"

    def holder(self) -> int | None:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return pid if pid_alive(pid) else None

    def acquire(self) -> bool:
        holder = self.holder()
        if holder and holder != os.getpid():   # the UI may have claimed it for us already
            return False
        self.claim(os.getpid())
        return True

    def claim(self, pid: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(pid), encoding="utf-8")

    def release(self) -> None:
        try:
            if self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def effective_status(d: dict) -> str:
    """'running' runs whose process has died show as 'interrupted'."""
    if d.get("status") == "running" and not pid_alive(d.get("pid")):
        return "interrupted"
    return d.get("status", "")


def list_runs(target: Target) -> list[dict]:
    if not target.runs_dir.exists():
        return []
    out = []
    for d in sorted(target.runs_dir.iterdir(), reverse=True):
        p = d / "run.json"
        if p.exists():
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return out
