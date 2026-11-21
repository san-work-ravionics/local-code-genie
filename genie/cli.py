"""genie command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, gitutil
from .config import WORKSPACES, load_settings, load_target


def _print_plan(d: dict) -> None:
    print(f"\nTask:      {d['task']}")
    print(f"Coverage:  {d['coverage']:.0%} of the task's vocabulary found in existing code")
    if d.get("unknown_terms"):
        print(f"New terms: {', '.join(d['unknown_terms'])}")
    print(f"Edit:      {', '.join(d['editable']) or '(none chosen)'}")
    e = d["estimate"]
    print(f"Size:      ~{e['needed_tokens']} tokens needed / {e['local_budget']} local context")
    print(f"Route:     {d['planned_route']}")
    for r in d["escalation_reasons"]:
        print(f"  - escalate because {r}")
    for x in d["decisions"]:
        print(f"  - {x}")
    print("\nReusable code found:")
    for m in d["matches"][:8]:
        print(f"  {m['coverage']:>4.0%}  {m['kind']:<9} {m['file']}:{m['line']}  {m['sig'][:70]}")
    if d.get("docs"):
        print("Relevant docs:")
        for m in d["docs"]:
            print(f"        {m['sig'][:90]}")


def cmd_init(a) -> None:
    path = Path(a.path).resolve()
    root = gitutil.toplevel(path)
    name = a.name or root.name
    ws = WORKSPACES / name
    ws.mkdir(parents=True, exist_ok=True)
    tt = ws / "target.toml"
    if tt.exists() and not a.force:
        print(f"Workspace exists: {ws} (use --force to overwrite target.toml)")
    else:
        tt.write_text(config.TARGET_TEMPLATE.format(path=str(root)), encoding="utf-8")
    conv = ws / "conventions.md"
    if not conv.exists():
        conv.write_text(config.CONVENTIONS_TEMPLATE, encoding="utf-8")
    config.set_current(name)
    print(f"Target '{name}' -> {root}\n  edit {tt}\n  edit {conv}\n  then run: genie train")


def cmd_train(a) -> None:
    from .indexer import build_index
    t = load_target(a.target)
    print(f"Indexing {t.root} ...")
    data = build_index(t)
    gitutil.add_local_exclude(t.root, ".aider*")
    print(f"Indexed {sum(data['files'].values())} files in {data['seconds']} s: "
          + ", ".join(f"{v} {k}" for k, v in data["files"].items()))
    print("Items: " + ", ".join(f"{v} {k}" for k, v in sorted(data["kinds"].items(), key=lambda kv: -kv[1])))
    conv = t.conventions_path
    words = len(conv.read_text(encoding="utf-8").split()) if conv.exists() else 0
    print(f"Conventions: {conv} ({words} words)")
    print(f"Rules: {len(t.cfg.get('rules', []))} project rules, "
          f"{len(t.cfg.get('protected', []))} protected paths")


def cmd_search(a) -> None:
    from .indexer import load_index
    from .search import CODE_KINDS, DOC_KINDS, Searcher
    t = load_target(a.target)
    s = Searcher(load_index(t)["items"])
    kinds = DOC_KINDS if a.docs else CODE_KINDS
    for m in s.search(a.query, k=a.k, kinds=kinds):
        print(f"{m['coverage']:>4.0%} {m['score']:>7.2f}  {m['kind']:<9} {m['file']}:{m['line']}  {m['sig'][:80]}")
    unk = s.unknown_terms(a.query)
    if unk:
        print(f"(not found anywhere in the repo: {', '.join(unk)})")


def cmd_task(a) -> None:
    from .router import run_task
    t = load_target(a.target)
    s = load_settings()
    run = run_task(t, s, a.task, files=a.file, local_only=a.local_only, force_remote=a.remote,
                   dry_run=a.dry_run, fast=a.fast, test_cmd=a.test)
    d = run.data
    _print_plan(d)
    if a.dry_run:
        print(f"\nDry run only. Context packet: {run.dir / 'context.md'}")
        return
    print()
    for att in d["attempts"]:
        print(f"Attempt {att['n']} ({att['kind']}, {att['model']}): {att['verdict'].upper()} "
              f"in {att['aider'].get('seconds')} s")
        for c in att["checks"]:
            if c["status"] in {"fail", "blocked", "warn"}:
                print(f"   {c['status']:<7} {c['id']}: {c['summary']}")
                for x in c["details"][:5]:
                    print(f"            {x}")
    print(f"\nStatus: {d['status']}   Route: {d.get('route')}   Run: {run.id}")
    if d.get("result", {}).get("error"):
        print("Error:", d["result"]["error"])
    if d["status"] in {"needs_review", "failed", "blocked"} and d.get("branch"):
        print(f"Review: genie show {run.id}   (or genie ui)")
        print(f"Then:   genie accept {run.id}   |   genie reject {run.id}")


def cmd_runs(a) -> None:
    from .runlog import list_runs
    t = load_target(a.target)
    for d in list_runs(t)[: a.n]:
        print(f"{d['id']:<52} {d.get('status', ''):<13} {str(d.get('route')):<10} {d['task'][:50]}")


def cmd_show(a) -> None:
    from .runlog import Run
    t = load_target(a.target)
    run = Run.load(t, a.run)
    _print_plan(run.data)
    p = run.dir / "diff.patch"
    if p.exists():
        print("\n" + p.read_text(encoding="utf-8"))
    if a.json:
        print(json.dumps(run.data, indent=2))


def cmd_accept(a) -> None:
    from .router import accept
    from .runlog import Run
    t = load_target(a.target)
    print(accept(t, Run.load(t, a.run)))


def cmd_reject(a) -> None:
    from .router import reject
    from .runlog import Run
    t = load_target(a.target)
    print(reject(t, Run.load(t, a.run)))


def cmd_ui(a) -> None:
    from .ui.server import serve
    serve(load_settings(), port=a.port)


def cmd_chat(a) -> None:
    from .aider import MODEL_SETTINGS, ensure_ollama, find_aider
    t = load_target(a.target)
    s = load_settings()
    model = s["remote"]["model"] if a.remote else s["local"]["model"]
    if not a.remote:
        ensure_ollama(s)
    exe = find_aider(s)
    if not exe:
        raise SystemExit("aider not found")
    cmd = [exe, "--model", model, "--model-settings-file", str(MODEL_SETTINGS), "--subtree-only",
           "--no-auto-commits", "--no-gitignore", "--no-check-update", "--analytics-disable",
           "--no-show-model-warnings"]
    if t.conventions_path.exists():
        cmd += ["--read", str(t.conventions_path)]
    cmd += a.files
    env = os.environ.copy()
    env.setdefault("OLLAMA_API_BASE", s["local"]["ollama_url"])
    sys.exit(subprocess.call(cmd, cwd=t.workdir, env=env))


def cmd_doctor(a) -> None:
    from .aider import find_aider, ollama_models, ollama_up
    from .router import remote_available
    s = load_settings()
    ok = lambda b: "ok " if b else "!! "  # noqa: E731
    print(ok(shutil.which("git")) + "git")
    print(ok(find_aider(s)) + f"aider: {find_aider(s)}")
    up = ollama_up(s["local"]["ollama_url"])
    print(ok(up) + f"ollama at {s['local']['ollama_url']}" + ("" if up else " (genie starts it when needed)"))
    if up:
        have = ollama_models(s)
        for m in (s["local"]["model"], s["local"]["fast_model"]):
            print(ok(m.split("/", 1)[1] in have) + f"model {m}")
    r, why = remote_available(s)
    print(ok(r) + f"remote {s['remote']['model']}: {why}")
    try:
        import importlib.util
        print(ok(importlib.util.find_spec("ruff")) + "ruff (python -m ruff)")
    except Exception:  # noqa: BLE001
        pass
    try:
        t = load_target(a.target)
        print(ok(t.root.exists()) + f"target '{t.name}': {t.root}")
        print(ok(t.index_path.exists()) + "index" + ("" if t.index_path.exists() else " (run genie train)"))
        dirty = gitutil.tracked_changes(t.root)
        print(ok(not dirty) + ("target clean" if not dirty else f"target has {len(dirty)} uncommitted change(s)"))
    except SystemExit as e:
        print("!! " + str(e))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="genie", description="Local-first coding agent with remote escalation")
    p.add_argument("--target", help="target workspace name (default: current)")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("init", help="register a target repo")
    x.add_argument("path")
    x.add_argument("--name")
    x.add_argument("--force", action="store_true")
    x.set_defaults(fn=cmd_init)

    x = sub.add_parser("train", help="index the target repo (code, CSS tokens, docs)")
    x.set_defaults(fn=cmd_train)

    x = sub.add_parser("search", help="search the index (debug retrieval)")
    x.add_argument("query")
    x.add_argument("-k", type=int, default=10)
    x.add_argument("--docs", action="store_true")
    x.set_defaults(fn=cmd_search)

    x = sub.add_parser("task", help="run a coding task")
    x.add_argument("task")
    x.add_argument("--file", "-f", action="append", help="file to edit (repeatable)")
    x.add_argument("--local-only", action="store_true", help="never call the remote model")
    x.add_argument("--remote", action="store_true", help="go straight to the remote model")
    x.add_argument("--fast", action="store_true", help="use the small fast local model")
    x.add_argument("--dry-run", action="store_true", help="plan only: show context and route")
    x.add_argument("--test", help="test command to run as a check, e.g. 'python -m pytest -q tests/unit/x.py'")
    x.set_defaults(fn=cmd_task)

    x = sub.add_parser("runs", help="list runs")
    x.add_argument("-n", type=int, default=20)
    x.set_defaults(fn=cmd_runs)

    for name, fn, hlp in (("show", cmd_show, "show a run and its diff"),
                          ("accept", cmd_accept, "squash-merge a run into your branch (staged)"),
                          ("reject", cmd_reject, "discard a run")):
        x = sub.add_parser(name, help=hlp)
        x.add_argument("run")
        if name == "show":
            x.add_argument("--json", action="store_true")
        x.set_defaults(fn=fn)

    x = sub.add_parser("ui", help="open the dashboard")
    x.add_argument("--port", type=int)
    x.set_defaults(fn=cmd_ui)

    x = sub.add_parser("chat", help="interactive Aider session with the target's conventions")
    x.add_argument("files", nargs="*")
    x.add_argument("--remote", action="store_true")
    x.set_defaults(fn=cmd_chat)

    x = sub.add_parser("doctor", help="check the setup")
    x.set_defaults(fn=cmd_doctor)

    a = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    a.fn(a)
