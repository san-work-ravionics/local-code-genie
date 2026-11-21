"""Secret detection. It runs on added lines (as a check) and on everything
genie would send to a remote model (as a gate before the call)."""

from __future__ import annotations

import re

PATTERNS = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("assigned-secret", re.compile(
        r"""(?i)\b(?:password|passwd|secret|api_?key|token|jwt_secret)\b\s*[:=]\s*['"][^'"\s]{8,}['"]""")),
]

SENSITIVE_FILES = re.compile(r"(?i)(^|/)(\.env(\..*)?|.*secret.*|.*credential.*|.*\.pem|.*\.key|id_rsa.*)$")
SAFE_FILES = re.compile(r"(?i)\.env\.example$")


def scan_text(text: str) -> list[dict]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS:
            if pat.search(line):
                found.append({"type": name, "line": lineno})
    return found


def sensitive_path(rel: str) -> bool:
    return bool(SENSITIVE_FILES.search(rel)) and not SAFE_FILES.search(rel)
