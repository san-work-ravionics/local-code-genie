"""Configuration.

- ``genie.toml`` (committed) holds global settings: models, escalation thresholds, UI port.
- ``genie.local.toml`` (git-ignored) overrides it per machine.
- ``workspaces/<target>/target.toml`` describes one target repo: where it is, what to
  index, and which project rules to check. Workspaces are git-ignored, so project
  knowledge never leaves this machine unless you choose to commit it.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

GENIE_HOME = Path(__file__).resolve().parent.parent
WORKSPACES = GENIE_HOME / "workspaces"
CURRENT_FILE = WORKSPACES / ".current"
ASSETS = Path(__file__).resolve().parent / "assets"

DEFAULTS: dict = {
    "local": {
        "model": "ollama_chat/qwen2.5-coder:7b",
        "edit_format": "diff",
        "fast_model": "ollama_chat/qwen2.5-coder:3b",
        "fast_edit_format": "whole",
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_exe": r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",
        "context_tokens": 8192,
        "map_tokens": 1024,
        "timeout_s": 1500,
    },
    "remote": {
        "enabled": True,
        "model": "anthropic/claude-sonnet-5-5",
        "api_key_env": "ANTHROPIC_API_KEY",
        "edit_format": "diff",
        "send_repo_map": False,
        "timeout_s": 600,
    },
    "escalation": {
        "min_coverage": 0.35,
        "local_retries": 1,
        "max_local_files": 2,
    },
    "context": {"max_chars": 9000, "snippets": 3, "doc_sections": 2},
    "aider": {"exe": r"%USERPROFILE%\.local\bin\aider.exe"},
    "ui": {"host": "127.0.0.1", "port": 8765},
    "costs": {"currency": "EUR", "usd_to_eur": 0.92},
}


def to_eur(usd: float | None, settings: dict) -> float:
    return round((usd or 0.0) * float(settings["costs"]["usd_to_eur"]), 4)


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def load_settings() -> dict:
    s = copy.deepcopy(DEFAULTS)
    for name in ("genie.toml", "genie.local.toml"):
        p = GENIE_HOME / name
        if p.exists():
            s = _merge(s, _load(p))
    return s


@dataclass
class Target:
    name: str
    ws: Path
    cfg: dict

    @property
    def root(self) -> Path:
        return Path(self.cfg["target"]["path"])

    @property
    def workdir(self) -> Path:
        return self.root / self.cfg["target"].get("workdir", "")

    @property
    def branch_prefix(self) -> str:
        return self.cfg["target"].get("branch_prefix", "genie/")

    @property
    def conventions_path(self) -> Path:
        return self.ws / "conventions.md"

    @property
    def index_path(self) -> Path:
        return self.ws / "index.json"

    @property
    def runs_dir(self) -> Path:
        return self.ws / "runs"

    def section(self, name: str) -> dict:
        return self.cfg.get(name, {})


def target_names() -> list[str]:
    if not WORKSPACES.exists():
        return []
    return sorted(p.name for p in WORKSPACES.iterdir() if (p / "target.toml").exists())


def current_target_name() -> str | None:
    if CURRENT_FILE.exists():
        name = CURRENT_FILE.read_text(encoding="utf-8").strip()
        if name:
            return name
    names = target_names()
    return names[0] if len(names) == 1 else None


def set_current(name: str) -> None:
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(name, encoding="utf-8")


def load_target(name: str | None = None) -> Target:
    name = name or current_target_name()
    if not name:
        raise SystemExit("No target selected. Run `genie init <repo-path>` or pass --target.")
    ws = WORKSPACES / name
    cfg_path = ws / "target.toml"
    if not cfg_path.exists():
        raise SystemExit(f"Unknown target '{name}'. Known: {', '.join(target_names()) or 'none'}")
    return Target(name=name, ws=ws, cfg=_load(cfg_path))


TARGET_TEMPLATE = """\
# Genie target profile. Edit freely; re-run `genie train` after changing [index].

[target]
path = '{path}'
workdir = ''            # sub-folder where the app lives (lint/tests run here)
branch_prefix = 'genie/'

[index]
code_include = ['*']
doc_include = ['*.md']
exclude = ['*/node_modules', '*/archive', '*/dist', '*/build', '*.min.js']

[checks]
lint = 'ruff'           # 'ruff' or '' to disable
test_cmd = ''           # e.g. 'python -m pytest -q tests/unit'

# Project rules, checked on added lines only:
# [[rules]]
# id = 'no-print'
# description = 'Use logging, not print()'
# pattern = '\\bprint\\('
# include = ['src/*.py']
# severity = 'error'
"""

CONVENTIONS_TEMPLATE = """\
# Project conventions

Keep this short (under ~1,000 words). It is sent to the model with every task.

## Code style
- ...

## Architecture rules
- ...

## UI / CSS
- ...
"""
