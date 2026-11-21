# local-code-genie

A coding agent that runs inside your repo. It **reuses the code, standards and CSS that already exist** and runs on a local model. It **escalates to a remote foundation model** only when the local model can't do the job, and every escalation records why.

```
task ─► retrieve ─► route ─► attempt ─► checks ─► pass ─► branch ready for review
          │           │        local 7B    │
          │           │        retry       └─ fail ─► retry ─► escalate to remote ─► checks
          │           └─ new capability / too big / too many files ─► remote first
          └─ index: functions, routes, JS + API calls, CSS tokens, HTML ids, spec sections
```

## Why

Small local models are cheap and private, but they make **changes nobody asked for**. In our benchmarks, both qwen2.5-coder 3B and 7B passed the tests while silently deleting `variance()`, and the 3B also created a stray `median.py`. Genie treats "what else changed?" as a first-class check, not just "do the tests pass?".

## Commands

```
genie init <repo> [--name N]       register a target repo (creates workspaces/<N>/)
genie train                        index the target (≈1–15 s)
genie search "<query>" [--docs]    see what genie would reuse
genie task "<task>" [-f FILE]...   run a task
      --dry-run                    plan only: retrieval, route, context packet
      --local-only | --remote      force the route
      --fast                       use the 3B model
      --test "<cmd>"               run tests as a check
genie runs | show <id> | accept <id> | reject <id>
genie ui                           dashboard on http://127.0.0.1:8765
genie chat [files]                 interactive Aider session with the conventions loaded
genie doctor                       check the setup
```

On Windows, `genie.bat` is the launcher. Add this folder to PATH to run `genie` from anywhere.

## How it works

| Stage | What happens |
|---|---|
| **Train** | Parses Python (AST: functions, classes, route decorators, calls), JS (functions, `/api/...` endpoints called), CSS (classes and `--custom-properties`), HTML (ids, inline `<script>`/`<style>`) and Markdown docs (sections by heading). Stored as JSON in the target's workspace. |
| **Retrieve** | BM25 search with a reuse-first bias (functions and shared/utils code rank higher) and abbreviation matching (pct ↔ percentage). **Coverage** is the share of the task's vocabulary that existing code already contains. |
| **Route** | Remote first if coverage is below `min_coverage` (a new capability), the edit won't fit the local context window, or too many files are involved. Otherwise local first. |
| **Context packet** | `context.md` holds the conventions, signatures and snippets to reuse, design tokens, relevant spec sections and the rules that will be checked. The same packet goes to the remote model, so the UI shows exactly what left the machine. |
| **Checks** | Changes made? Only the requested files touched? No existing symbols removed? Python compiles? Protected paths untouched? Project rules (regex on added lines) and secret scan? Spec co-change? New ruff issues only (compared with base)? Optional tests. |
| **Escalate** | After `local_retries` failed local attempts, the same task, the packet and the check failures go to the remote model. A secret scan runs before every remote call; any hit blocks it. The repo map is not sent by default. |
| **Review** | Each run is committed on its own branch (`<branch_prefix><run-id>`), and your repo is switched back to your branch. `accept` squash-merges the change into your working tree (staged, not committed). `reject` deletes the branch. |

## Configuration

- `genie.toml`: models, escalation thresholds, context budget, UI port. Overrides go in `genie.local.toml`.
- `workspaces/<target>/target.toml`: repo path, index globs, rules, protected paths, co-change rules.
- `workspaces/<target>/conventions.md`: short standards doc sent with every task.
- Remote: set the env var named in `remote.api_key_env` (default `ANTHROPIC_API_KEY`). The `remote.model` can be any litellm id Aider supports.

`workspaces/` is git-ignored: project knowledge, run logs and diffs stay on this machine.

## Requirements

Python 3.11+ (stdlib only), git, [Aider](https://aider.chat) as the edit engine, and [Ollama](https://ollama.com) with `qwen2.5-coder:7b` (plus `:3b` for `--fast`). `ruff` is optional, for lint checks.

## Tests

```
python -m unittest discover -s tests -v
```

See [docs/FEATURE.md](docs/FEATURE.md) for the design and roadmap.
