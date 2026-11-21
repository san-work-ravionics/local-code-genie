"""Checks that decide whether an edit is acceptable.

Each check returns ``{"id", "status", "summary", "details": [...]}`` where status is
``pass`` | ``warn`` | ``fail`` | ``blocked`` | ``skip``.

- ``fail`` is a quality problem. Genie retries, then escalates to the remote model.
- ``blocked`` is a policy stop (protected or read-only path). Genie stops and
  asks the user; escalating would not help.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import gitutil
from .config import Target
from .indexer import css_class_names, js_symbol_names, match_any, py_symbol_names
from .secretscan import scan_text

IGNORE_CHANGES = [".aider*", "*/.aider*", "*__pycache__*", "*.pyc", ".ruff_cache*",
                  "*/.ruff_cache*", ".pytest_cache*", "*/.pytest_cache*"]
CONFLICT = re.compile(r"^(<<<<<<< |>>>>>>> |=======$)")


def _result(cid: str, status: str, summary: str, details: list | None = None) -> dict:
    return {"id": cid, "status": status, "summary": summary, "details": details or []}


def changed_files(target: Target, base: str, pre_untracked: set[str]) -> list[str]:
    root = target.root
    files = set(gitutil.changed_since(root, base))
    files |= {f for f in gitutil.untracked(root) if f not in pre_untracked}
    return sorted(f for f in files if not match_any(f, IGNORE_CHANGES))


def _symbols(rel: str, src: str) -> set[str]:
    ext = os.path.splitext(rel)[1].lower()
    if ext == ".py":
        return py_symbol_names(src)
    if ext in {".js", ".jsx", ".mjs", ".ts", ".tsx"}:
        return js_symbol_names(src)
    if ext == ".css":
        return css_class_names(src)
    if ext in {".html", ".htm"}:
        return js_symbol_names(src) | css_class_names(src)
    return set()


def _read(root: Path, rel: str) -> str | None:
    p = root / rel
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8-sig", errors="replace")


def _added(target: Target, base: str, rel: str, is_new: bool) -> list[tuple[int, str]]:
    if is_new:
        src = _read(target.root, rel) or ""
        return list(enumerate(src.splitlines(), 1))
    return gitutil.added_lines(target.root, base, rel)


def run_checks(target: Target, base: str, pre_untracked: set[str], editable: list[str],
               task: str, allow_new: bool = False, test_cmd: str | None = None) -> list[dict]:
    root = target.root
    results: list[dict] = []
    changed = changed_files(target, base, pre_untracked)
    new_files = [f for f in changed if gitutil.show(root, base, f) is None]

    if not changed:
        return [_result("changes", "fail", "The model made no changes.")]
    results.append(_result("changes", "pass", f"{len(changed)} file(s) changed", changed))

    # --- scope: only the files genie asked the model to edit
    outside = [f for f in changed if f not in editable and not (allow_new and f in new_files)]
    if outside:
        results.append(_result("scope", "fail",
                               f"{len(outside)} file(s) changed or created outside the requested files",
                               outside))
    else:
        results.append(_result("scope", "pass", "Only requested files changed"))

    # --- collateral: existing functions/classes/CSS rules silently removed
    task_l = task.lower()
    removed_all = []
    for f in changed:
        before = gitutil.show(root, base, f)
        if before is None:
            continue
        after = _read(root, f)
        if after is None:
            removed_all.append(f"{f}: whole file deleted")
            continue
        gone = sorted(_symbols(f, before) - _symbols(f, after))
        for name in gone:
            short = name.split(".")[-1].lstrip(".#").lower()
            if short and short in task_l:
                continue  # the task names it, so removal may be intended
            removed_all.append(f"{f}: removed `{name}`")
    if removed_all:
        results.append(_result("collateral", "fail",
                               f"{len(removed_all)} existing symbol(s) removed that the task did not mention",
                               removed_all))
    else:
        results.append(_result("collateral", "pass", "No existing code removed"))

    # --- syntax (Python)
    bad = []
    for f in changed:
        if f.endswith(".py"):
            src = _read(root, f)
            if src is None:
                continue
            try:
                compile(src, f, "exec")
            except SyntaxError as e:
                bad.append(f"{f}:{e.lineno}: {e.msg}")
    results.append(_result("syntax", "fail" if bad else "pass",
                           "Syntax errors" if bad else "Python files compile", bad))

    # --- protected / read-only paths
    stops = []
    for p in target.cfg.get("protected", []):
        for f in changed:
            if match_any(f, [p["path"]]):
                stops.append(f"{f}: {p.get('reason', 'protected path')}")
    results.append(_result("protected", "blocked" if stops else "pass",
                           "Change touches a protected path" if stops else "No protected paths touched",
                           stops))

    # --- project rules + built-ins on added lines
    rules = [r for r in target.cfg.get("rules", [])]
    compiled = [(r, re.compile(r["pattern"])) for r in rules]
    violations: list[dict] = []
    for f in changed:
        if _read(root, f) is None:
            continue
        for lineno, text in _added(target, base, f, f in new_files):
            if CONFLICT.match(text):
                violations.append({"rule": "conflict-marker", "severity": "error", "file": f,
                                   "line": lineno, "text": text.strip()[:140],
                                   "why": "Merge-conflict marker left in file"})
            for hit in scan_text(text):
                violations.append({"rule": f"secret:{hit['type']}", "severity": "error", "file": f,
                                   "line": lineno, "text": "[redacted]",
                                   "why": "Possible secret in source (org.md §1)"})
            for r, rx in compiled:
                if r.get("include") and not match_any(f, r["include"]):
                    continue
                if r.get("exclude") and match_any(f, r["exclude"]):
                    continue
                if rx.search(text):
                    violations.append({"rule": r["id"], "severity": r.get("severity", "error"),
                                       "file": f, "line": lineno, "text": text.strip()[:140],
                                       "why": r.get("description", "")})
    errors = [v for v in violations if v["severity"] == "error"]
    warns = [v for v in violations if v["severity"] != "error"]
    status = "fail" if errors else ("warn" if warns else "pass")
    results.append(_result("rules", status,
                           f"{len(errors)} rule error(s), {len(warns)} warning(s)" if violations
                           else f"{len(rules)} project rules + built-ins: no violations",
                           [f"[{v['rule']}] {v['file']}:{v['line']} {v['text']}  ({v['why']})"
                            for v in violations]))

    # --- co-change rules (e.g. API change must update a spec)
    notes = []
    for cc in target.cfg.get("co_change", []):
        trig = [f for f in changed if match_any(f, cc["when"])]
        if trig and not any(match_any(f, cc["require"]) for f in changed):
            notes.append(f"{cc.get('description', 'co-change rule')} (triggered by {', '.join(trig)})")
    if notes:
        results.append(_result("co-change", "warn", "Related files usually change together", notes))

    # --- lint (new violations only, compared with the base version)
    results.append(_lint(target, base, changed))

    # --- tests
    cmd = test_cmd if test_cmd is not None else target.section("checks").get("test_cmd", "")
    if cmd:
        results.append(_tests(target, cmd))
    else:
        results.append(_result("tests", "skip", "No test command configured (pass --test)"))
    return results


def _ruff(target: Target, rel: str, content: str) -> list[dict]:
    p = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--no-cache",
         "--stdin-filename", str(target.root / rel), "-"],
        input=content, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=target.workdir,
    )
    try:
        return json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ruff failed") from None


def _lint(target: Target, base: str, changed: list[str]) -> dict:
    if target.section("checks").get("lint", "ruff") != "ruff":
        return _result("lint", "skip", "Lint disabled")
    py = [f for f in changed if f.endswith(".py") and (target.root / f).exists()]
    if not py:
        return _result("lint", "skip", "No Python files changed")
    new_issues = []
    try:
        for f in py:
            after = _ruff(target, f, _read(target.root, f) or "")
            before_src = gitutil.show(target.root, base, f)
            before = _ruff(target, f, before_src) if before_src is not None else []
            budget = Counter(i["code"] for i in before)
            for issue in after:
                if budget[issue["code"]] > 0:
                    budget[issue["code"]] -= 1
                    continue
                loc = issue.get("location", {}).get("row", "?")
                new_issues.append(f"{f}:{loc} {issue['code']} {issue['message']}")
    except (RuntimeError, OSError) as e:
        return _result("lint", "skip", f"ruff unavailable: {e}")
    return _result("lint", "fail" if new_issues else "pass",
                   f"{len(new_issues)} new ruff issue(s)" if new_issues else "ruff: no new issues",
                   new_issues)


def _tests(target: Target, cmd: str) -> dict:
    env = os.environ.copy()
    for k, v in target.section("checks").get("env", {}).items():
        env[k] = v.replace("{root}", str(target.root)).replace("{workdir}", str(target.workdir))
    try:
        p = subprocess.run(cmd, shell=True, cwd=target.workdir, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return _result("tests", "fail", f"Tests timed out: {cmd}")
    tail = (p.stdout + p.stderr)[-2500:]
    return _result("tests", "pass" if p.returncode == 0 else "fail",
                   f"`{cmd}` exited {p.returncode}", tail.splitlines()[-30:])


def overall(results: list[dict]) -> str:
    statuses = {r["status"] for r in results}
    if "blocked" in statuses:
        return "blocked"
    if "fail" in statuses:
        return "fail"
    return "pass"


def failure_feedback(results: list[dict]) -> str:
    lines = []
    for r in results:
        if r["status"] in {"fail", "blocked"}:
            lines.append(f"- {r['id']}: {r['summary']}")
            lines.extend(f"    - {d}" for d in r["details"][:8])
    return "\n".join(lines)
