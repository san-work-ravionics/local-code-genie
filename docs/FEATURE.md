# Feature: genie, a hybrid local-first coding agent

Status: **MVP built** (2026-11-21). It started as the "hybrid agent" idea in `Local-code-build/FEATURE-hybrid-agent.md`.

## Goal
A coding agent that runs inside a repo and uses the code that is already there (existing helpers, coding standards, CSS and design tokens) for enhancements. It calls a remote foundation model through an API only when the local model can't do the task, and records why.

## Key risk (from benchmarks)
The local models pass tests but change things outside the request: they deleted `variance()` with the `whole` edit format, and the 3B created a stray `median.py`. Genie checks for collateral changes, not only test results.

## What is built (v0.1)
- [x] `genie init/train`: index of Python, JS, CSS, HTML and Markdown docs; stdlib only
- [x] Reuse-first retrieval with a coverage score
- [x] Router: local (7B, diff format) → retry with check feedback → escalate to remote
- [x] Pre-routing to remote for new capabilities, too-large edits and too many files
- [x] Checks: changes, scope, collateral, syntax, protected paths, project rules, secrets, co-change, ruff (new issues only), tests
- [x] Secret gate before any remote call; repo map not sent to remote by default
- [x] Branch per run, accept (squash, staged) / reject
- [x] Run log and local UI: run list, why-this-route, timeline, attempts and checks, reused code, diff, remote payload, summary stats
- [x] `genie chat` for interactive work

## Interaction
- `genie init <repo>` → edit `target.toml` + `conventions.md` → `genie train`
- `genie task "…" --dry-run` to see the plan → `genie task "…"` → review in `genie ui` → accept/reject
- `--local-only`, `--remote`, `--fast`, `--test "<cmd>"`

## Roadmap
- [ ] Choose the remote provider and set the API key (default: Anthropic via litellm)
- [ ] Semantic search (MiniLM embeddings are already cached locally) alongside BM25
- [ ] Architect mode for remote (remote plans, local edits) as an option
- [x] Start tasks from the UI (new-task form, "Run this for real" on planned runs), live progress and model output, Cancel (safe: repo restored), one real run per target
- [ ] Auto-draft `conventions.md` from guardrail/spec files
- [ ] Tune `min_coverage` and retries from the run statistics
- [ ] JS syntax check (requires Node, which is not installed)
- [ ] Duplicate-helper report (e.g. `fmtPct` exists 4 times in RITA)
