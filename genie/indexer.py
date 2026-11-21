"""`genie train`: build a searchable index of a target repo.

"Training" here means indexing, not fine-tuning. Genie extracts:

- Python functions, classes and methods (signature, decorators such as routes,
  docstring, names they call)
- JS functions (including inline <script> blocks in HTML) and the /api/ endpoints they call
- CSS classes and CSS custom properties (design tokens), including inline <style> blocks
- HTML element ids
- Markdown doc sections (specs, guardrails, ADRs), split by heading

The index is plain JSON in the target's workspace.
"""

from __future__ import annotations

import ast
import bisect
import fnmatch
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from .config import Target

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache",
             ".pytest_cache", ".mypy_cache", ".idea", ".vscode", ".aider.tags.cache.v4"}
CODE_EXT = {".py": "python", ".js": "js", ".jsx": "js", ".mjs": "js", ".ts": "js",
            ".tsx": "js", ".css": "css", ".html": "html", ".htm": "html"}
DOC_EXT = {".md"}
MAX_BYTES = 600_000

JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else",
               "typeof", "new", "await", "with", "constructor"}


def match_any(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def iter_files(root: Path, include: list[str], exclude: list[str], exts: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if d in SKIP_DIRS or d.startswith(".aider") or match_any(rel, exclude):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in exts:
                continue
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if not match_any(rel, include) or match_any(rel, exclude):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            yield rel, p


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def _path_words(rel: str) -> str:
    return re.sub(r"[/\\._-]", " ", rel)


# ---------------------------------------------------------------- Python

def py_items(rel: str, src: str) -> list[dict]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    items: list[dict] = []
    mod_doc = (ast.get_docstring(tree) or "").strip().split("\n")[0]
    items.append({"kind": "module", "name": rel.rsplit("/", 1)[-1], "file": rel, "line": 1,
                  "end": min(len(src.splitlines()), 40), "sig": rel, "doc": mod_doc,
                  "text": f"{_path_words(rel)} {mod_doc}"})

    def visit(node: ast.AST, prefix: str) -> None:
        for n in ast.iter_child_nodes(node):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            q = f"{prefix}.{n.name}" if prefix else n.name
            decos = []
            for d in n.decorator_list:
                try:
                    decos.append("@" + ast.unparse(d))
                except Exception:  # noqa: BLE001 - best effort
                    pass
            if isinstance(n, ast.ClassDef):
                kind = "class"
                bases = ", ".join(ast.unparse(b) for b in n.bases)
                sig = f"class {n.name}({bases})" if bases else f"class {n.name}"
            else:
                kind = "method" if prefix else "function"
                asyn = "async " if isinstance(n, ast.AsyncFunctionDef) else ""
                ret = f" -> {ast.unparse(n.returns)}" if n.returns else ""
                sig = f"{asyn}def {n.name}({ast.unparse(n.args)}){ret}"
            doc = (ast.get_docstring(n) or "").strip().split("\n")[0]
            calls: list[str] = []
            strings: list[str] = []
            for sub in ast.walk(n):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                    if nm and nm not in calls:
                        calls.append(nm)
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if sub.value.startswith("/") and len(sub.value) < 80:
                        strings.append(sub.value)
            calls = calls[:25]
            text = " ".join([n.name, q, _path_words(rel), doc, " ".join(decos),
                             " ".join(calls), " ".join(strings)])
            items.append({"kind": kind, "name": q, "file": rel, "line": n.lineno,
                          "end": getattr(n, "end_lineno", n.lineno), "sig": sig,
                          "decorators": decos, "doc": doc, "calls": calls[:12],
                          "text": text})
            if isinstance(n, ast.ClassDef):
                visit(n, q)

    visit(tree, "")
    return items


def py_symbol_names(src: str) -> set[str]:
    return {it["name"] for it in py_items("x.py", src) if it["kind"] != "module"}


# ---------------------------------------------------------------- JS

_JS_PATTERNS = [
    re.compile(r"^[ \t]*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", re.M),
    re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b[^(]*\(([^)]*)\)|\(([^)]*)\)\s*=>|([A-Za-z_$][\w$]*)\s*=>)", re.M),
    re.compile(r"^[ \t]*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)", re.M),
    re.compile(r"^[ \t]*((?:window|module\.exports|[A-Za-z_$][\w$]*)\.[A-Za-z_$][\w$.]*)\s*=\s*(?:async\s*)?(?:function\b[^(]*\(([^)]*)\)|\(([^)]*)\)\s*=>)", re.M),
    re.compile(r"^[ \t]+(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{", re.M),
]
_API = re.compile(r"""['"`](/api/[^'"`?\s$]*|/health|/progress)""")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


class _Lines:
    """Fast offset -> line number lookup for one source string."""

    def __init__(self, src: str):
        self.nl = [m.start() for m in re.finditer("\n", src)]
        self.lines = src.split("\n")

    def line(self, pos: int) -> int:
        return bisect.bisect_left(self.nl, pos) + 1


def _block_end(src: str, start: int, li: _Lines, max_lines: int = 300) -> int:
    start_line = li.line(start)
    i = src.find("{", start)
    if i < 0 or li.line(i) > start_line + 2:
        return start_line  # no braced body (e.g. arrow expression)
    depth = 0
    stop = li.nl[min(start_line + max_lines, len(li.nl) - 1)] if li.nl else len(src)
    for j in range(i, min(len(src), stop + 1)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return li.line(j)
    return min(start_line + max_lines, len(li.lines))


def js_items(rel: str, src: str, line_offset: int = 0) -> list[dict]:
    items: list[dict] = []
    seen: set[tuple[str, int]] = set()
    li = _Lines(src)
    for idx, pat in enumerate(_JS_PATTERNS):
        for m in pat.finditer(src):
            name = m.group(1)
            if name in JS_KEYWORDS:
                continue
            local_line = li.line(m.start())
            line = local_line + line_offset
            if (name, line) in seen:
                continue
            seen.add((name, line))
            kind = "class" if idx == 2 else ("method" if idx == 4 else "function")
            args = next((g for g in m.groups()[1:] if g is not None), "")
            local_end = _block_end(src, m.start(), li)
            end = local_end + line_offset
            body = "\n".join(li.lines[local_line - 1:local_end])
            apis = sorted(set(_API.findall(body)))
            calls = [c for c in dict.fromkeys(_CALL.findall(body)) if c not in JS_KEYWORDS and c != name][:25]
            sig = f"class {name}" if kind == "class" else f"function {name}({args.strip()})"
            items.append({"kind": kind, "name": name, "file": rel, "line": line, "end": end,
                          "sig": sig, "doc": "", "apis": apis, "calls": calls[:12],
                          "text": " ".join([name, _path_words(rel), " ".join(apis), " ".join(calls)])})
    return items


def js_symbol_names(src: str) -> set[str]:
    return {it["name"] for it in js_items("x.js", src)}


# ---------------------------------------------------------------- CSS / HTML

_CSS_VAR = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+)[;}]")
_CSS_RULE = re.compile(r"([^{}@;]+)\{")
_CSS_CLASS = re.compile(r"\.([A-Za-z_][\w-]*)")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def css_items(rel: str, src: str, line_offset: int = 0) -> list[dict]:
    items: list[dict] = []
    li = _Lines(src)
    for m in _CSS_VAR.finditer(src):
        line = li.line(m.start()) + line_offset
        items.append({"kind": "css-var", "name": m.group(1), "file": rel,
                      "line": line, "end": line,
                      "sig": f"{m.group(1)}: {m.group(2).strip()[:60]}", "doc": "",
                      "text": f"{m.group(1)} {m.group(2)} css variable token style {_path_words(rel)}"})
    seen: dict[str, dict] = {}
    for m in _CSS_RULE.finditer(src):
        sel = _CSS_COMMENT.sub(" ", m.group(1)).strip()
        if not sel or sel.startswith("--") or "." not in sel or len(sel) > 300:
            continue
        for cls in _CSS_CLASS.findall(sel):
            if cls in seen or cls[0].isdigit():
                continue
            line = li.line(m.start(1)) + line_offset
            seen[cls] = {"kind": "css-class", "name": "." + cls, "file": rel, "line": line,
                         "end": line + 6, "sig": sel[:100], "doc": "",
                         "text": f"{cls} {sel} css class style {_path_words(rel)}"}
    items.extend(seen.values())
    return items


def css_class_names(src: str) -> set[str]:
    return {it["name"] for it in css_items("x.css", src) if it["kind"] == "css-class"}


_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_ID = re.compile(r"<([a-zA-Z][\w-]*)[^>]*?\bid=\"([^\"]+)\"")


def html_items(rel: str, src: str) -> list[dict]:
    items: list[dict] = []
    li = _Lines(src)
    for m in _STYLE.finditer(src):
        items.extend(css_items(rel, m.group(1), li.line(m.start(1)) - 1))
    for m in _SCRIPT.finditer(src):
        items.extend(js_items(rel, m.group(1), li.line(m.start(1)) - 1))
    for m in _ID.finditer(src):
        line = li.line(m.start())
        tag, el_id = m.group(1), m.group(2)
        if "{" in el_id or "$" in el_id:
            continue
        items.append({"kind": "html-id", "name": "#" + el_id, "file": rel, "line": line,
                      "end": line + 8, "sig": f"<{tag} id=\"{el_id}\">", "doc": "",
                      "text": f"{el_id} {tag} html element {_path_words(rel)}"})
    return items


# ---------------------------------------------------------------- Markdown

_HEAD = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)


def md_items(rel: str, src: str) -> list[dict]:
    items: list[dict] = []
    heads = list(_HEAD.finditer(src))
    if not heads:
        heads_spans = [(0, len(src), rel)]
    else:
        heads_spans = []
        if heads[0].start() > 0 and src[: heads[0].start()].strip():
            heads_spans.append((0, heads[0].start(), rel))
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(src)
            heads_spans.append((h.start(), end, h.group(2)))
    title = heads[0].group(2) if heads else rel
    for start, end, heading in heads_spans:
        body = src[start:end].strip()
        if len(body) < 40:
            continue
        line = _line_of(src, start)
        items.append({"kind": "doc", "name": heading[:120], "file": rel, "line": line,
                      "end": _line_of(src, end), "sig": f"{rel} › {heading[:80]}", "doc": title,
                      "text": f"{heading} {heading} {title} {_path_words(rel)} {body[:2500]}"})
    return items


# ---------------------------------------------------------------- build

def build_index(target: Target) -> dict:
    t0 = time.time()
    root = target.root
    idx_cfg = target.section("index")
    exclude = idx_cfg.get("exclude", [])
    items: list[dict] = []
    files: Counter = Counter()
    for rel, p in iter_files(root, idx_cfg.get("code_include", ["*"]), exclude, set(CODE_EXT)):
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        lang = CODE_EXT[p.suffix.lower()]
        fn = {"python": py_items, "js": js_items, "css": css_items, "html": html_items}[lang]
        found = fn(rel, src)
        items.extend(found)
        files[lang] += 1
    for rel, p in iter_files(root, idx_cfg.get("doc_include", ["*.md"]), exclude, DOC_EXT):
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        items.extend(md_items(rel, src))
        files["markdown"] += 1
    kinds = Counter(it["kind"] for it in items)
    data = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(root),
        "seconds": round(time.time() - t0, 1),
        "files": dict(files),
        "kinds": dict(kinds),
        "items": items,
    }
    target.ws.mkdir(parents=True, exist_ok=True)
    target.index_path.write_text(json.dumps(data), encoding="utf-8")
    return data


def load_index(target: Target) -> dict:
    if not target.index_path.exists():
        raise SystemExit(f"No index for '{target.name}'. Run `genie train` first.")
    return json.loads(target.index_path.read_text(encoding="utf-8"))
