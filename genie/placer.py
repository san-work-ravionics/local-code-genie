"""Insert mode: place model-generated code into a file with fixed rules. No AI here.

The model writes only the new code. Genie decides where it goes, based on the
file's syntax tree, and only ever adds lines. Existing code cannot be deleted
this way.

Python placement rules, in order:
1. New import lines go after the file's last top-level import (duplicates skipped).
2. A FastAPI route on a router that already has routes:
   - a fixed path (no `{param}`) goes directly above the first parameterised route
     (`/{id}`), so FastAPI does not match the fixed path as an id;
   - otherwise it goes after that router's last route.
3. A function whose first parameter is `self`/`cls` becomes a method of the
   file's only class, or of the class named in the task.
4. Anything else goes after the last top-level function or class.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field

ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route", "head", "options"}
_FENCE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.S | re.I)


@dataclass
class Placement:
    ok: bool
    new_src: str = ""
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    added: list[str] = field(default_factory=list)


def extract_code(text: str) -> str | None:
    """The code from a model reply: all fenced blocks, else the raw text if it parses."""
    blocks = [b.strip("\n") for b in _FENCE.findall(text) if b.strip()]
    if blocks:
        return "\n\n".join(blocks)
    raw = text.strip()
    try:
        ast.parse(raw)
        return raw or None
    except SyntaxError:
        return None


def _start(node: ast.AST) -> int:
    decos = getattr(node, "decorator_list", [])
    return min([node.lineno] + [d.lineno for d in decos])


def _route(node: ast.AST) -> tuple[str, str, str] | None:
    for d in getattr(node, "decorator_list", []):
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name) and d.func.attr in ROUTE_METHODS):
            path = ""
            if d.args and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str):
                path = d.args[0].value
            return d.func.value.id, d.func.attr, path
    return None


def _segment(lines: list[str], node: ast.AST) -> list[str]:
    return lines[_start(node) - 1:node.end_lineno]


def _is_subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


def place_python(src: str, generated: str, task: str = "") -> Placement:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return Placement(False, error=f"the existing file does not parse: {e}")
    gen = textwrap.dedent(generated).strip("\n")
    try:
        gtree = ast.parse(gen)
    except SyntaxError as e:
        return Placement(False, error=f"the generated code does not parse (line {e.lineno}: {e.msg})")

    lines = src.splitlines()
    glines = gen.splitlines()
    top_defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    by_name = {n.name: n for n in top_defs}
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    have_imports = {ast.unparse(n) for n in imports}
    classes = [n for n in top_defs if isinstance(n, ast.ClassDef)]

    notes: list[str] = []
    added: list[str] = []
    inserts: list[tuple[int, int, list[str]]] = []   # (line index, sequence, lines)
    seq = 0

    new_imports = []
    for n in gtree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            if ast.unparse(n) not in have_imports:
                new_imports.extend(_segment(glines, n))
                notes.append(f"added import `{ast.unparse(n)}`")
    if new_imports:
        after = max((n.end_lineno for n in imports), default=0)
        if not after and tree.body and isinstance(tree.body[0], ast.Expr) \
                and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
            after = tree.body[0].end_lineno   # after the module docstring
        if not imports:   # first import block in the file: PEP 8 spacing around it
            new_imports = ([""] if after else []) + new_imports + ["", ""]
        inserts.append((after, seq, new_imports))
        seq += 1

    for n in gtree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant):
                continue   # stray docstring
            return Placement(False, error=f"insert mode only adds imports, functions and classes; "
                                          f"got `{ast.unparse(n)[:60]}`")
        seg = _segment(glines, n)
        if n.name in by_name:
            if ast.dump(n) == ast.dump(by_name[n.name]):
                notes.append(f"skipped an unchanged copy of existing `{n.name}`")
                continue
            return Placement(False, error=f"`{n.name}` already exists in the file. Insert mode only adds "
                                          f"new code: use a new name, or ask for a modification instead")

        first_arg = n.args.args[0].arg if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.args.args else ""
        route = _route(n)
        if first_arg in {"self", "cls"} and classes:
            named = [c for c in classes if c.name.lower() in task.lower()]
            cls = named[0] if len(named) == 1 else (classes[0] if len(classes) == 1 else None)
            if cls is None:
                return Placement(False, error="the new method's class is ambiguous; name the class in the task")
            if n.name in {m.name for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}:
                return Placement(False, error=f"`{cls.name}.{n.name}` already exists")
            indent = re.match(r"\s*", lines[cls.body[0].lineno - 1]).group(0) if cls.body else "    "
            inserts.append((cls.end_lineno, seq, [""] + [indent + ln if ln else "" for ln in seg]))
            notes.append(f"`{n.name}` added as the last method of `{cls.name}`")
        elif route:
            rvar, _meth, path = route
            existing = [(t, _route(t)) for t in top_defs if _route(t) and _route(t)[0] == rvar]
            anchor_before = None
            if existing and "{" not in path:
                param = [(t, r) for t, r in existing if "{" in r[2]]
                if param:
                    anchor_before = param[0]
            if anchor_before:
                t, r = anchor_before
                inserts.append((_start(t) - 1, seq, seg + ["", ""]))
                notes.append(f"route `{path}` placed above `{t.name}` (`{r[2]}`): fixed paths must come "
                             f"before parameterised ones")
            elif existing:
                t = existing[-1][0]
                inserts.append((t.end_lineno, seq, ["", ""] + seg))
                notes.append(f"route `{path}` placed after the last `{rvar}` route `{t.name}`")
            else:
                last = top_defs[-1].end_lineno if top_defs else len(lines)
                inserts.append((last, seq, ["", ""] + seg))
                notes.append(f"route `{path}` placed at the end of the file")
        else:
            last = top_defs[-1] if top_defs else None
            at = last.end_lineno if last else len(lines)
            inserts.append((at, seq, ["", ""] + seg))
            notes.append(f"`{n.name}` placed after `{last.name}`" if last else f"`{n.name}` placed at the end")
        added.append(n.name)
        seq += 1

    if not added and not new_imports:
        return Placement(False, error="the answer contained no new code")

    out = list(lines)
    for idx, _s, block in sorted(inserts, key=lambda x: (x[0], x[1]), reverse=True):
        out[idx:idx] = block
    new_src = "\n".join(out) + ("\n" if src.endswith("\n") or not src else "")
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        return Placement(False, error=f"placing the code broke the file (line {e.lineno}: {e.msg})")
    if not _is_subsequence(lines, new_src.splitlines()):
        return Placement(False, error="internal: placement changed existing lines")   # should never happen
    return Placement(True, new_src=new_src, notes=notes, added=added)
