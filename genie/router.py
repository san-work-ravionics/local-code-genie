"""The genie loop: retrieve → route → attempt (local, retry, escalate) → check → record.

Safety model for the target repo:
- Genie refuses to run if the target has uncommitted changes to tracked files.
- Each run works on its own branch (``<branch_prefix><run-id>``). The result is
  committed there, and the target is switched back to your original branch.
- ``genie accept`` squash-merges the branch into your working tree (staged, not
  committed). ``genie reject`` deletes the branch.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import gitutil
from .aider import ensure_ollama, run_aider
from .checks import changed_files, failure_feedback, overall, run_checks
from .config import Target
from .context import build_context, choose_files, estimate_tokens, task_message
from .indexer import load_index, match_any
from .runlog import Run, RunLock
from .search import CODE_KINDS, DOC_KINDS, Searcher
from .secretscan import scan_text, sensitive_path

AIDER_OVERHEAD_TOKENS = 1500   # Aider system prompt + edit-format instructions
OUTPUT_RESERVE_TOKENS = 1200


def remote_available(settings: dict) -> tuple[bool, str]:
    r = settings["remote"]
    if not r.get("enabled", True):
        return False, "remote disabled in genie.toml"
    if not os.environ.get(r["api_key_env"]):
        return False, f"{r['api_key_env']} is not set"
    return True, "ok"


def _norm_file(target: Target, f: str) -> str:
    p = Path(f)
    if not p.is_absolute():
        cand_root, cand_work = target.root / p, target.workdir / p
        p = cand_work if cand_work.exists() or not cand_root.exists() else cand_root
    return p.resolve().relative_to(target.root.resolve()).as_posix()


def plan_task(target: Target, settings: dict, task: str, files: list[str] | None = None,
              force_remote: bool = False, local_only: bool = False, fast: bool = False) -> dict:
    index = load_index(target)
    s = Searcher(index["items"])
    code = s.search(task, k=12, kinds=CODE_KINDS)
    docs = s.search(task, k=settings["context"]["doc_sections"], kinds=DOC_KINDS)
    coverage = code[0]["coverage"] if code else 0.0
    unknown = s.unknown_terms(task)

    user_files = [_norm_file(target, f) for f in (files or [])]
    protected = [p["path"] for p in target.cfg.get("protected", [])]
    editable = user_files or choose_files([m for m in code if not match_any(m["file"], protected)], 1, task)
    new_files = [f for f in editable if not (target.root / f).exists()]

    ctx = build_context(target, settings, task, code, docs, editable)
    loc = settings["local"]
    fmt = loc["fast_edit_format"] if fast else loc["edit_format"]
    file_tokens = sum(estimate_tokens((target.root / f).read_text(encoding="utf-8", errors="replace"))
                      for f in editable if (target.root / f).exists())
    factor = 2.0 if fmt == "whole" else 1.3   # "whole" must echo the full file back
    need = int(estimate_tokens(ctx) + file_tokens * factor + loc["map_tokens"]
               + AIDER_OVERHEAD_TOKENS + OUTPUT_RESERVE_TOKENS)
    budget = loc["context_tokens"]

    # Does the task name existing functions/classes in the files being edited?
    # Then it changes existing code (e.g. a bug fix), not a new capability.
    qtok = set(s.query_tokens(task))
    named = sorted({it["name"] for it in index["items"]
                    if it["file"] in editable and it["kind"] in {"function", "method", "class"}
                    and it["name"].split(".")[-1].lower() in qtok})

    esc = settings["escalation"]
    reasons = []
    if force_remote:
        reasons.append("forced with --remote")
    if coverage < esc["min_coverage"] and named:
        pass  # low vocabulary overlap, but it edits named existing code
    elif coverage < esc["min_coverage"]:
        reasons.append(f"new capability: the best existing match covers {coverage:.0%} of the task's "
                       f"vocabulary (threshold {esc['min_coverage']:.0%})"
                       + (f"; words not found anywhere in the repo: {', '.join(unknown[:6])}" if unknown else ""))
    if len(editable) > esc["max_local_files"]:
        reasons.append(f"{len(editable)} files to edit (local limit {esc['max_local_files']})")
    if need > budget:
        reasons.append(f"too large for the local model: about {need} tokens needed, "
                       f"context window is {budget}")

    ok, why = remote_available(settings)
    decisions = []
    if reasons and local_only:
        route = "local"
        decisions.append("Remote would be preferred, but --local-only was given")
    elif reasons and ok:
        route = "remote"
    elif reasons:
        route = "local"
        decisions.append(f"Remote would be preferred, but it is unavailable ({why}); trying local")
    elif named:
        route = "local"
        decisions.append(f"Task changes existing code ({', '.join(named[:5])}); local model first")
    else:
        route = "local"
        decisions.append("Existing code covers the task; local model first")

    return {
        "code_matches": code, "doc_matches": docs, "coverage": coverage, "unknown_terms": unknown,
        "named_symbols": named,
        "editable": editable, "new_files": new_files, "context": ctx,
        "estimate": {"needed_tokens": need, "local_budget": budget, "context_tokens": estimate_tokens(ctx),
                     "file_tokens": file_tokens},
        "escalation_reasons": reasons, "route": route, "decisions": decisions,
        "remote_available": ok, "remote_status": why,
    }


def _reset_to_base(root: Path, base: str, pre_untracked: set[str]) -> None:
    gitutil.git(root, "reset", "--hard", base)
    for f in gitutil.untracked(root) - pre_untracked:
        p = root / f
        if p.is_file() and not f.startswith(".aider"):
            p.unlink()


def run_task(target: Target, settings: dict, task: str, files: list[str] | None = None,
             local_only: bool = False, force_remote: bool = False, dry_run: bool = False,
             fast: bool = False, test_cmd: str | None = None, run_id: str | None = None) -> Run:
    options = {"files": files or [], "local_only": local_only, "remote": force_remote,
               "dry_run": dry_run, "fast": fast, "test_cmd": test_cmd}
    run = Run.create(target, task, options, rid=run_id)
    d = run.data

    st = run.step("retrieve", detail="Search the index for reusable code and docs")
    plan = plan_task(target, settings, task, files, force_remote, local_only, fast)
    run.write("context.md", plan.pop("context"))
    trim = lambda ms: [{k: m.get(k) for k in ("kind", "name", "file", "line", "sig", "doc", "score",  # noqa: E731
                                              "coverage", "matched")} for m in ms]
    d["matches"] = trim(plan.pop("code_matches"))
    d["docs"] = trim(plan.pop("doc_matches"))
    d.update(plan)
    run.end_step(st, detail=f"{len(d['matches'])} code matches, best coverage {d['coverage']:.0%}")

    st = run.step("route")
    d["planned_route"] = d["route"]
    run.end_step(st, detail=f"{d['route']}: " + "; ".join(d["escalation_reasons"] or d["decisions"]))

    if not d["editable"]:
        d["decisions"].append("No suitable file to edit was found (protected or test files only); pass --file")
    if dry_run:
        d["status"] = "planned"
        run.save()
        return run
    if not d["editable"]:
        d["status"] = "failed"
        d["result"] = {"error": "No file to edit. Re-run with --file <path> (it may be a new file)."}
        run.save()
        return run

    lock = RunLock(target)
    if not lock.acquire():
        d["status"] = "failed"
        d["result"] = {"error": f"Another genie run is in progress for this target (pid {lock.holder()})"}
        run.save()
        return run
    try:
        return _execute(run, target, settings, task, local_only, fast, test_cmd)
    finally:
        lock.release()


def _execute(run: Run, target: Target, settings: dict, task: str, local_only: bool, fast: bool,
             test_cmd: str | None) -> Run:
    d = run.data
    ctx_path = run.dir / "context.md"
    cancel_path = run.dir / "cancel"
    root = target.root
    dirty = gitutil.tracked_changes(root)
    if dirty:
        d["status"] = "failed"
        d["result"] = {"error": f"Target repo has uncommitted changes: {dirty[:5]}. Commit or stash first."}
        run.save()
        return run
    orig = gitutil.current_branch(root)
    if orig == "HEAD":
        d["status"] = "failed"
        d["result"] = {"error": "Target repo is in detached HEAD state"}
        run.save()
        return run
    gitutil.add_local_exclude(root, ".aider*")
    base = gitutil.head(root)
    pre_untracked = gitutil.untracked(root)
    branch = f"{target.branch_prefix}{run.id}"
    gitutil.git(root, "checkout", "-q", "-b", branch)
    d.update(branch=branch, base=base, orig_branch=orig)
    run.save()

    loc, rem, esc = settings["local"], settings["remote"], settings["escalation"]
    lm, lf = (loc["fast_model"], loc["fast_edit_format"]) if fast else (loc["model"], loc["edit_format"])
    attempts: list[tuple[str, str, str]] = []
    if d["route"] == "remote":
        attempts = [("remote", rem["model"], rem["edit_format"])]
    else:
        attempts = [("local", lm, lf)] * (1 + esc["local_retries"])
        if d["remote_available"] and not local_only:
            attempts.append(("remote", rem["model"], rem["edit_format"]))

    editable_abs = [root / f for f in d["editable"]]
    final = "fail"
    feedback = None
    ollama_ok = None
    try:
        for n, (kind, model, fmt) in enumerate(attempts, 1):
            if cancel_path.exists():
                d["cancelled"] = True
                break
            if n > 1:
                _reset_to_base(root, base, pre_untracked)
            if kind == "local" and ollama_ok is None:
                st = run.step("ollama", detail="Make sure the local model server is running")
                ollama_ok = ensure_ollama(settings)
                run.end_step(st, "done" if ollama_ok else "fail")
            if kind == "local" and not ollama_ok:
                continue
            if kind == "remote" and n > 1 and d["attempts"]:
                d["escalated"] = True
                d["decisions"].append(f"Escalated to {model} after {n - 1} failed local attempt(s)")
            msg = task_message(task, d["editable"], feedback,
                               escalated_from_local=(kind == "remote" and n > 1))
            if kind == "remote":
                gate = _remote_gate(target, ctx_path, editable_abs, msg)
                d["remote_payload"] = gate
                if gate["blocked"]:
                    d["decisions"].append("Remote call blocked: " + "; ".join(gate["reasons"]))
                    run.save()
                    break
            st = run.step(f"attempt {n}: {kind}", detail=model)
            res = run_aider(
                settings=settings, cwd=target.workdir, model=model, edit_format=fmt,
                files=editable_abs, read_files=[ctx_path], message=msg, run_dir=run.dir,
                tag=f"attempt{n}", timeout_s=(rem if kind == "remote" else loc)["timeout_s"],
                map_tokens=loc["map_tokens"] if (kind == "local" or rem.get("send_repo_map")) else 0,
                cancel_path=cancel_path,
            )
            if res.get("cancelled"):
                d["cancelled"] = True
                d["attempts"].append({"n": n, "kind": kind, "model": model, "edit_format": fmt,
                                      "message": msg, "aider": res, "checks": [], "verdict": "cancelled"})
                run.end_step(st, "cancelled", "Cancelled by user")
                break
            checks = run_checks(target, base, pre_untracked, d["editable"], task,
                                allow_new=bool(d["new_files"]), test_cmd=test_cmd)
            verdict = overall(checks)
            d["attempts"].append({"n": n, "kind": kind, "model": model, "edit_format": fmt,
                                  "message": msg, "aider": res, "checks": checks, "verdict": verdict})
            run.end_step(st, verdict, f"{model}: {verdict} in {res.get('seconds')} s")
            if verdict in {"pass", "blocked"}:
                final = verdict
                break
            feedback = failure_feedback(checks)
    finally:
        _finalize(run, target, task, base, pre_untracked, branch, orig)

    used = [a["kind"] for a in d["attempts"]]
    if final == "pass":
        d["route"] = "escalated" if (used[-1] == "remote" and "local" in used) else used[-1]
    elif used:
        d["route"] = "escalated" if ("remote" in used and "local" in used) else used[-1]
    if d.get("cancelled"):
        d["status"] = "cancelled"
    elif not d["attempts"]:
        d["status"] = "failed"
        d["result"].setdefault("error", "No attempt could run (see decisions)")
    elif d["result"].get("no_changes"):
        d["status"] = "no_change"
    else:
        d["status"] = {"pass": "needs_review", "blocked": "blocked"}.get(final, "failed")
    d["result"]["remote_cost_usd"] = round(sum(a["aider"].get("cost_usd", 0) for a in d["attempts"]), 4)
    d["result"]["seconds"] = round(sum(a["aider"].get("seconds", 0) for a in d["attempts"]), 1)
    run.save()
    return run


def _remote_gate(target: Target, ctx_path: Path, files: list[Path], msg: str) -> dict:
    reasons, hits = [], []
    for p in files:
        rel = p.relative_to(target.root).as_posix()
        if sensitive_path(rel):
            reasons.append(f"sensitive file name: {rel}")
        if p.exists():
            for h in scan_text(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(f"{rel}:{h['line']} {h['type']}")
    for h in scan_text(ctx_path.read_text(encoding="utf-8")):
        hits.append(f"context.md:{h['line']} {h['type']}")
    for h in scan_text(msg):
        hits.append(f"message:{h['line']} {h['type']}")
    if hits:
        reasons.append(f"possible secrets: {', '.join(hits[:5])}")
    return {"blocked": bool(reasons), "reasons": reasons,
            "files": [p.relative_to(target.root).as_posix() for p in files],
            "context_file": ctx_path.name, "context_chars": len(ctx_path.read_text(encoding="utf-8")),
            "message": msg}


def _finalize(run: Run, target: Target, task: str, base: str, pre_untracked: set[str],
              branch: str, orig: str) -> None:
    root, d = target.root, run.data
    st = run.step("finalize", detail="Commit the result on the genie branch and switch back")
    try:
        changed = changed_files(target, base, pre_untracked)
        if changed:
            gitutil.git(root, "add", "-A", "--", *changed)
            gitutil.git(root, "-c", "user.name=genie", "-c", "user.email=genie@localhost",
                        "commit", "-q", "-m", f"genie: {task[:60]}\n\nRun {run.id}")
            patch = gitutil.diff(root, base, branch)
            run.write("diff.patch", patch)
            d["result"]["diff_stat"] = gitutil.diff_stat(root, base, branch)
            d["result"]["changed_files"] = changed
        else:
            d["result"]["no_changes"] = True
        gitutil.git(root, "checkout", "-q", orig)
        if not changed:
            gitutil.git(root, "branch", "-q", "-D", branch)
            d["branch"] = None
        run.end_step(st, "done", f"back on {orig}" + (f"; result on {branch}" if changed else ""))
    except gitutil.GitError as e:
        d["result"]["finalize_error"] = str(e)
        run.end_step(st, "fail", str(e))


def accept(target: Target, run: Run) -> str:
    d, root = run.data, target.root
    if d.get("status") not in {"needs_review", "failed"} or not d.get("branch"):
        raise SystemExit(f"Run {run.id} is '{d.get('status')}', nothing to accept")
    if gitutil.current_branch(root) != d["orig_branch"]:
        raise SystemExit(f"Switch the target repo to '{d['orig_branch']}' first")
    if gitutil.tracked_changes(root):
        raise SystemExit("Target repo has uncommitted changes; commit or stash them first")
    gitutil.git(root, "merge", "--squash", d["branch"])
    gitutil.git(root, "branch", "-q", "-D", d["branch"])
    d["status"] = "accepted"
    d["result"]["accepted_into"] = d["orig_branch"]
    run.save()
    return (f"Changes from {run.id} are staged on '{d['orig_branch']}'. "
            "Review with `git diff --staged`, then commit.")


def reject(target: Target, run: Run) -> str:
    d, root = run.data, target.root
    if d.get("branch"):
        if gitutil.current_branch(root) == d["branch"]:
            raise SystemExit("Target repo is on the genie branch; switch away first")
        gitutil.git(root, "branch", "-q", "-D", d["branch"], check=False)
    d["status"] = "rejected"
    d["branch"] = None
    run.save()
    return f"Run {run.id} rejected; branch deleted."
