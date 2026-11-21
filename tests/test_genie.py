"""Unit tests: indexing, search, checks (against a throwaway git repo). No model calls."""

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from genie import checks, gitutil
from genie.config import Target
from genie.indexer import css_items, js_items, md_items, py_items
from genie.search import Searcher, tokenize


def sh(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class TestIndexer(unittest.TestCase):
    def test_python_symbols_and_routes(self):
        src = textwrap.dedent('''
            """Alerts router."""
            @router.get("/api/v1/x")
            def list_all(repo=None):
                """List alerts."""
                return repo.read_all()

            class Repo:
                def upsert(self, row):
                    pass
        ''')
        items = py_items("src/a.py", src)
        names = {i["name"] for i in items}
        self.assertIn("list_all", names)
        self.assertIn("Repo.upsert", names)
        fn = next(i for i in items if i["name"] == "list_all")
        self.assertIn('@router.get(\'/api/v1/x\')', fn["decorators"])
        self.assertEqual(fn["doc"], "List alerts.")

    def test_js_functions_and_endpoints(self):
        src = textwrap.dedent('''
            export const fmtPct = v => v == null ? '-' : v + '%';
            export async function loadDrift() {
              const d = await apiFetch('/api/experience/ops/drift');
              return d;
            }
        ''')
        items = {i["name"]: i for i in js_items("js/x.js", src)}
        self.assertIn("fmtPct", items)
        self.assertEqual(items["loadDrift"]["apis"], ["/api/experience/ops/drift"])
        self.assertEqual(items["loadDrift"]["end"], 6)

    def test_css_tokens_and_classes(self):
        src = ":root { --surface: #fff; }\n/* badge */\n.badge.ok { color: var(--ok); }\n"
        items = css_items("a.css", src)
        self.assertIn("--surface", {i["name"] for i in items})
        cls = [i for i in items if i["kind"] == "css-class"]
        self.assertEqual({i["name"] for i in cls}, {".badge", ".ok"})
        self.assertNotIn("/*", cls[0]["sig"])

    def test_markdown_sections(self):
        src = "# Spec\n\nintro text that is long enough to keep around.\n\n## Lot sizes\n\nNIFTY lot size is read from settings, never hardcoded anywhere.\n"
        names = [i["name"] for i in md_items("spec.md", src)]
        self.assertIn("Lot sizes", names)


class TestSearch(unittest.TestCase):
    def test_tokenize_abbrev_and_camel(self):
        self.assertEqual(tokenize("formatPercentage"), ["fmt", "pct"])
        self.assertIn("drift", tokenize("get_drift_alerts"))

    def test_reuse_first_and_coverage(self):
        items = [
            {"kind": "function", "name": "fmtPct", "file": "js/shared/utils.js", "text": "fmtPct shared utils"},
            {"kind": "css-class", "name": ".pct", "file": "a.html", "text": "pct css class"},
            {"kind": "css-class", "name": ".pct", "file": "b.html", "text": "pct css class"},
        ]
        s = Searcher(items)
        res = s.search("format percent", k=5)
        self.assertEqual(res[0]["name"], "fmtPct")
        self.assertEqual(len([r for r in res if r["name"] == ".pct"]), 1)  # collapsed
        self.assertEqual(s.unknown_terms("quantum hedging"), ["quantum", "hedging"])


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "user.email", "t@t")
        sh(self.root, "config", "user.name", "t")
        (self.root / "src").mkdir()
        (self.root / "src" / "stats.py").write_text(
            "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef variance(xs):\n    return 0\n",
            encoding="utf-8")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "base")
        self.base = gitutil.head(self.root)
        self.target = Target("t", self.root / ".ws", {
            "target": {"path": str(self.root), "workdir": ""},
            "checks": {"lint": ""},
            "rules": [{"id": "no-print", "pattern": r"(^|[^\w.])print\(", "include": ["src/*.py"],
                       "description": "no print"}],
            "protected": [{"path": "src/core/*", "reason": "protected"}],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def run_checks(self, task="add mode", editable=("src/stats.py",)):
        res = checks.run_checks(self.target, self.base, set(), list(editable), task)
        return {r["id"]: r for r in res}, checks.overall(res)

    def test_collateral_deletion_is_caught(self):
        # the exact failure seen in the aider-bench runs: variance() silently dropped
        (self.root / "src" / "stats.py").write_text(
            "def mean(xs):\n    return sum(xs) / len(xs) if xs else 0.0\n", encoding="utf-8")
        r, verdict = self.run_checks("fix mean for empty lists")
        self.assertEqual(r["collateral"]["status"], "fail")
        self.assertIn("variance", r["collateral"]["details"][0])
        self.assertEqual(verdict, "fail")

    def test_named_removal_allowed(self):
        (self.root / "src" / "stats.py").write_text("def mean(xs):\n    return 1\n", encoding="utf-8")
        r, _ = self.run_checks("remove variance")
        self.assertEqual(r["collateral"]["status"], "pass")

    def test_stray_file_and_rule(self):
        (self.root / "src" / "stats.py").write_text(
            "def mean(xs):\n    print(xs)\n    return 1\n\n\ndef variance(xs):\n    return 0\n", encoding="utf-8")
        (self.root / "median.py").write_text("x = 1\n", encoding="utf-8")
        r, verdict = self.run_checks()
        self.assertEqual(r["scope"]["status"], "fail")
        self.assertIn("median.py", r["scope"]["details"])
        self.assertEqual(r["rules"]["status"], "fail")
        self.assertEqual(verdict, "fail")

    def test_protected_path_blocks(self):
        (self.root / "src" / "core").mkdir()
        (self.root / "src" / "core" / "greeks.py").write_text("x = 1\n", encoding="utf-8")
        r, verdict = self.run_checks(editable=("src/core/greeks.py",))
        self.assertEqual(r["protected"]["status"], "blocked")
        self.assertEqual(verdict, "blocked")

    def _with_repo_classes(self):
        """Index with a repository class hierarchy, like RITA's SqlRepository."""
        import json
        self.target.ws.mkdir(parents=True, exist_ok=True)
        items = [
            {"kind": "class", "name": "Base", "file": "src/repo.py", "sig": "class Base(Generic[T])"},
            {"kind": "method", "name": "Base.read_all", "file": "src/repo.py", "sig": ""},
            {"kind": "class", "name": "AlertsRepository", "file": "src/repo.py", "sig": "class AlertsRepository(Base[A, B])"},
        ]
        (self.target.ws / "index.json").write_text(json.dumps({"items": items}), encoding="utf-8")

    def test_call_to_missing_method_is_caught(self):
        # the exact bug from the RITA sample: repo.count() does not exist
        self._with_repo_classes()
        (self.root / "src" / "stats.py").write_text(
            "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef variance(xs):\n    return 0\n\n\n"
            "def get_count(repo: AlertsRepository):\n    return {'count': repo.count()}\n\n\n"
            "def get_all(repo: AlertsRepository):\n    return repo.read_all()\n", encoding="utf-8")
        r, verdict = self.run_checks()
        self.assertEqual(r["calls"]["status"], "fail")
        self.assertIn("repo.count()", r["calls"]["details"][0])
        self.assertIn("read_all", r["calls"]["details"][0])   # lists what does exist
        self.assertEqual(len(r["calls"]["details"]), 1)        # read_all() is fine
        self.assertEqual(verdict, "fail")

    def test_retry_feedback_contains_deleted_code(self):
        (self.root / "src" / "stats.py").write_text("def mean(xs):\n    return 1\n", encoding="utf-8")
        res = checks.run_checks(self.target, self.base, set(), ["src/stats.py"], "fix mean")
        fb = checks.failure_feedback(res, self.target, self.base)
        self.assertIn("def variance(xs):", fb)
        self.assertIn("exactly as below", fb)

    def test_baseline_failing_tests_only_warn(self):
        (self.root / "src" / "stats.py").write_text(
            "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef variance(xs):\n    return 0\n\n\n"
            "def mode(xs):\n    return None\n", encoding="utf-8")
        cmd = 'python -c "raise SystemExit(1)"'
        res = checks.run_checks(self.target, self.base, set(), ["src/stats.py"], "add mode",
                                test_cmds=[cmd], baseline={cmd: {"ok": False, "error": "ImportError"}})
        t = next(r for r in res if r["id"] == "tests")
        self.assertEqual(t["status"], "warn")
        self.assertEqual(checks.overall(res), "pass")
        res = checks.run_checks(self.target, self.base, set(), ["src/stats.py"], "add mode",
                                test_cmds=[cmd], baseline={cmd: {"ok": True, "error": ""}})
        self.assertEqual(checks.overall(res), "fail")   # newly broken by the change

    def test_clean_change_passes(self):
        (self.root / "src" / "stats.py").write_text(
            "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef variance(xs):\n    return 0\n\n\n"
            "def mode(xs):\n    return max(set(xs), key=xs.count) if xs else None\n", encoding="utf-8")
        _, verdict = self.run_checks()
        self.assertEqual(verdict, "pass")


if __name__ == "__main__":
    unittest.main()
