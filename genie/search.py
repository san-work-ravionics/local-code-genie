"""Lexical (BM25) search over the index, with no dependencies.

``coverage`` is the share of the task's vocabulary (idf-weighted) that a
matched item contains. A low coverage on the best code match is genie's signal
that the task needs something the repo does not already have.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

STOP = set("""
a an and or of to in for on with is are be been this that it its as at by from into onto
the then than also so such via per each when where which who what how should must can could
will would shall may might please make add adds adding new create creates update updates
change changes use uses using fix fixes implement implements write support supports allow
existing current so do does done get set file files code function functions method methods
class classes value values return returns based like just only all any some more less one two
bug bugs instead crash crashes crashing broken wrong correct correctly properly issue issues
need needs want wants currently case cases sure ensure them they their there here
""".split())

DOC_KINDS = {"doc"}
CODE_KINDS = {"module", "function", "method", "class", "css-class", "css-var", "html-id"}
K1, B = 1.4, 0.75


# Common code abbreviations, mapped to one canonical token on both sides.
ABBREV = {
    "percent": "pct", "percentage": "pct", "format": "fmt", "formatting": "fmt", "formatted": "fmt",
    "button": "btn", "message": "msg", "configuration": "config", "cfg": "config",
    "database": "db", "authentication": "auth", "authorization": "auth", "number": "num",
    "parameter": "param", "parameters": "param", "params": "param", "temporary": "tmp",
    "temp": "tmp", "element": "el", "document": "doc", "repository": "repo",
    "repositories": "repo", "statistic": "stat", "statistics": "stat", "initialize": "init",
    "milliseconds": "ms", "millisecond": "ms",
}
REUSE_PATH = re.compile(r"(^|/)(shared|common|utils?|helpers?|lib|core)(/|\.|$)")
REUSE_KINDS = {"function": 1.35, "method": 1.25, "class": 1.25, "module": 1.0,
               "css-var": 1.0, "css-class": 0.9, "html-id": 0.8, "doc": 1.0}
COLLAPSE_KINDS = {"css-class", "css-var", "html-id"}


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    out = []
    for t in re.findall(r"[A-Za-z][A-Za-z0-9]+", text):
        t = t.lower()
        if t in STOP:
            continue
        t = ABBREV.get(t, t)
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        t = ABBREV.get(t, t)
        out.append(t)
    return out


class Searcher:
    def __init__(self, items: list[dict]):
        self.items = items
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths: list[int] = []
        self.name_tokens: list[set[str]] = []
        for i, it in enumerate(items):
            c = Counter(tokenize(it.get("text", "")))
            self.lengths.append(sum(c.values()) or 1)
            self.name_tokens.append(set(tokenize(it.get("name", ""))))
            for t, f in c.items():
                self.postings[t].append((i, f))
        n = len(items) or 1
        self.avgdl = sum(self.lengths) / n
        self.n = n
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}
        self.max_idf = math.log(1 + (n + 0.5) / 0.5)

    def query_tokens(self, query: str) -> list[str]:
        return list(dict.fromkeys(tokenize(query)))

    def search(self, query: str, k: int = 10, kinds: set[str] | None = None) -> list[dict]:
        q = self.query_tokens(query)
        if not q:
            return []
        scores: dict[int, float] = defaultdict(float)
        hits: dict[int, set[str]] = defaultdict(set)
        for t in q:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, f in self.postings[t]:
                if kinds and self.items[i]["kind"] not in kinds:
                    continue
                dl = self.lengths[i]
                scores[i] += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / self.avgdl))
                hits[i].add(t)
        qset = set(q)
        for i in list(scores):
            it = self.items[i]
            overlap = len(self.name_tokens[i] & qset)
            if overlap:
                scores[i] *= 1 + 0.5 * overlap
            scores[i] *= REUSE_KINDS.get(it["kind"], 1.0)
            if REUSE_PATH.search(it["file"]) and it["kind"] in {"function", "method", "class"}:
                scores[i] *= 1.25   # reuse-first: prefer shared helpers
            if "/tests/" in it["file"] or it["file"].startswith("tests/"):
                scores[i] *= 0.6
        total_w = sum(self.idf.get(t, self.max_idf) for t in q)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: list[dict] = []
        seen: dict[tuple[str, str], dict] = {}
        for i, s in ranked:
            it = self.items[i]
            key = (it["kind"], it["name"])
            if it["kind"] in COLLAPSE_KINDS and key in seen:
                seen[key]["also_in"] = seen[key].get("also_in", 0) + 1
                continue
            cov = sum(self.idf[t] for t in hits[i]) / total_w if total_w else 0.0
            item = {kk: vv for kk, vv in it.items() if kk != "text"}
            item.update(score=round(s, 3), coverage=round(cov, 3), matched=sorted(hits[i]))
            seen[key] = item
            out.append(item)
            if len(out) >= k:
                break
        return out

    def unknown_terms(self, query: str) -> list[str]:
        """Task words that appear nowhere in the indexed repo."""
        return [t for t in self.query_tokens(query) if t not in self.idf]
