"""Checks that decide whether an edit is acceptable.

Each check returns ``{"id", "status", "summary", "details": [...]}`` where status is
``pass`` | ``warn`` | ``fail`` | ``blocked`` | ``skip``.

- ``fail`` is a quality problem. Genie retries, then escalates to the remote model.
- ``blocked`` is a policy stop (protected or read-only path). Genie stops and
  asks the user; escalating would not help.
"""

from __future__ import annotations

import ast
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


def tests_for(target: Target, files: list[str]) -> list[str]:
    """Test commands from [[test_map]] whose `when` globs match any of the files."""
    cmds: list[str] = []
    for tm in target.cfg.get("test_map", []):
        if any(match_any(f, tm["when"]) for f in files) and tm["cmd"] not in cmds:
            cmds.append(tm["cmd"])
    return cmds


def run_tests(target: Target, cmds: list[str]) -> dict[str, dict]:
    """Run test commands; used once before the edit to record the baseline."""
    return {cmd: _run_test_cmd(target, cmd) for cmd in cmds}


def run_checks(target: Target, base: str, pre_untracked: set[str], editable: list[str],
               task: str, allow_new: bool = False, test_cmds: list[str] | None = None,
               baseline: dict[str, dict] | None = None) -> list[dict]:
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
    removed_syms: list[dict] = []
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
            removed_syms.append({"file": f, "name": name})
    if removed_all:
        r = _result("collateral", "fail",
                    f"{len(removed_all)} existing symbol(s) removed that the task did not mention",
                    removed_all)
        r["symbols"] = removed_syms
        results.append(r)
    else:
        results.append(_result("collateral", "pass", "No existing code removed"))

    # --- calls to methods that do not exist on the receiver's class
    results.append(_typed_calls(target, base, changed, new_files))

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

    # --- tests (compared with the baseline taken before the edit)
    cmds = test_cmds if test_cmds is not None else [c for c in [target.section("checks").get("test_cmd", "")] if c]
    if not cmds:
        results.append(_result("tests", "skip", "No test command for these files (add [[test_map]] or pass --test)"))
    for cmd in cmds:
        results.append(_tests_vs_baseline(target, cmd, (baseline or {}).get(cmd)))
    return results


# ---------------------------------------------------------------- typed calls

GENERIC_BASES = {"Generic", "object", "ABC", "Protocol", "Enum", "IntEnum", "StrEnum"}


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _class_table(items: list[dict]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    bases: dict[str, list[str]] = {}
    methods: dict[str, set[str]] = {}
    for it in items:
        if not it["file"].endswith(".py"):
            continue
        if it["kind"] == "class":
            cname = it["name"].split(".")[-1]
            m = re.match(r"class \w+\((.*)\)$", it.get("sig", ""))
            bl = []
            for b in _split_top(m.group(1)) if m else []:
                b = re.sub(r"\[.*\]$", "", b.strip()).split(".")[-1]
                if b and "=" not in b:
                    bl.append(b)
            bases.setdefault(cname, bl)
            methods.setdefault(cname, set())
        elif it["kind"] == "method":
            parts = it["name"].split(".")
            methods.setdefault(parts[-2], set()).add(parts[-1])
    return bases, methods


def _methods_of(cls: str, bases: dict, methods: dict, seen: set | None = None) -> set[str] | None:
    """All methods of cls incl. inherited ones; None if any base is outside the repo."""
    seen = seen or set()
    if cls in seen:
        return set()
    seen.add(cls)
    if cls not in bases:
        return None
    out = set(methods.get(cls, set()))
    for b in bases[cls]:
        if b in GENERIC_BASES:
            continue
        sub = _methods_of(b, bases, methods, seen)
        if sub is None:
            return None
        out |= sub
    return out


def _ann_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _ann_name(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split(".")[-1]
    return None


def _typed_calls(target: Target, base: str, changed: list[str], new_files: list[str]) -> dict:
    py = [f for f in changed if f.endswith(".py") and (target.root / f).exists()]
    if not py or not target.index_path.exists():
        return _result("calls", "skip", "No Python changes to resolve")
    from .indexer import py_items  # local import keeps module load light
    items = [it for it in json.loads(target.index_path.read_text(encoding="utf-8"))["items"]
             if it["file"] not in py]
    srcs = {f: _read(target.root, f) or "" for f in py}
    for f, src in srcs.items():
        items.extend(py_items(f, src))
    bases, methods = _class_table(items)
    problems = []
    for f, src in srcs.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        added = {n for n, _ in _added(target, base, f, f in new_files)}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            env: dict[str, str] = {}
            for a in fn.args.args + fn.args.kwonlyargs:
                cls = _ann_name(a.annotation) if a.annotation is not None else None
                if cls in bases:
                    env[a.arg] = cls
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    cls = _ann_name(node.value.func)
                    if cls in bases:
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                env[t.id] = cls
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)):
                    continue
                recv, attr = node.func.value.id, node.func.attr
                if recv not in env or attr.startswith("__") or node.lineno not in added:
                    continue
                known = _methods_of(env[recv], bases, methods)
                if known is None or attr in known:
                    continue
                avail = ", ".join(sorted(m for m in known if not m.startswith("_"))[:14])
                problems.append(f"{f}:{node.lineno} `{recv}.{attr}()`: {env[recv]} has no method "
                                f"`{attr}` (available: {avail})")
    if problems:
        return _result("calls", "fail", f"{len(problems)} call(s) to methods that do not exist", problems)
    return _result("calls", "pass", "All calls on typed objects resolve to existing methods")


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


def _run_test_cmd(target: Target, cmd: str) -> dict:
    env = os.environ.copy()
    for k, v in target.section("checks").get("env", {}).items():
        env[k] = v.replace("{root}", str(target.root)).replace("{workdir}", str(target.workdir))
    try:
        p = subprocess.run(cmd, shell=True, cwd=target.workdir, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": None, "tail": [f"timed out: {cmd}"], "error": "timeout"}
    lines = (p.stdout + p.stderr)[-2500:].splitlines()[-30:]
    err = next((ln.strip() for ln in reversed(lines) if re.search(r"Error|FAILED|failed", ln)), "")
    return {"ok": p.returncode == 0, "rc": p.returncode, "tail": lines, "error": err}


def _tests_vs_baseline(target: Target, cmd: str, before: dict | None) -> dict:
    now = _run_test_cmd(target, cmd)
    if now["ok"]:
        return _result("tests", "pass", f"`{cmd}` passed", now["tail"][-5:])
    if before is not None and not before["ok"]:
        return _result("tests", "warn",
                       f"`{cmd}` was already failing before the change, so it cannot judge this edit"
                       + (f" ({before['error'][:120]})" if before.get("error") else ""), now["tail"])
    return _result("tests", "fail", f"`{cmd}` exited {now['rc']}", now["tail"])


def overall(results: list[dict]) -> str:
    statuses = {r["status"] for r in results}
    if "blocked" in statuses:
        return "blocked"
    if "fail" in statuses:
        return "fail"
    return "pass"


def failure_feedback(results: list[dict], target: Target | None = None, base: str | None = None) -> str:
    lines = []
    for r in results:
        if r["status"] in {"fail", "blocked"}:
            lines.append(f"- {r['id']}: {r['summary']}")
            lines.extend(f"    - {d}" for d in r["details"][:8])
    if target is not None and base:
        lines.append(_preserve_block(results, target, base))
    return "\n".join(x for x in lines if x)


def _preserve_block(results: list[dict], target: Target, base: str) -> str:
    """Show the exact code the model deleted, so the retry can keep it."""
    from .indexer import py_items
    syms = next((r.get("symbols", []) for r in results if r["id"] == "collateral"), [])
    blocks = []
    for s in syms[:4]:
        src = gitutil.show(target.root, base, s["file"])
        if src is None or not s["file"].endswith(".py"):
            continue
        it = next((i for i in py_items(s["file"], src) if i["name"] == s["name"]), None)
        if not it:
            continue
        lines = src.splitlines()
        start = it["line"] - len(it.get("decorators", []))
        blocks.append("\n".join(lines[start - 1:it["end"]]))
    if not blocks:
        return ""
    return ("\nThese existing definitions were deleted. They must stay in the file exactly as below:\n"
            "```python\n" + "\n\n\n".join(blocks) + "\n```\n"
            "Add the new code next to them instead of replacing them.")
