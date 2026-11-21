"""Insert mode placement rules (no model calls)."""

import textwrap
import unittest

from genie.placer import extract_code, place_python
from genie.router import choose_edit_mode

ALERTS = textwrap.dedent('''\
    """System CRUD router for the alerts table."""
    from fastapi import APIRouter, Depends

    router = APIRouter(prefix="/api/v1/system/alerts")


    def get_repo():
        return None


    @router.get("/")
    def list_all(repo=Depends(get_repo)):
        return repo.read_all()


    @router.get("/{id}")
    def get_one(id: str, repo=Depends(get_repo)):
        return repo.find_by_id(id)


    @router.delete("/{id}", status_code=204)
    def delete(id: str, repo=Depends(get_repo)):
        return repo.delete(id)
''')


class TestPlacer(unittest.TestCase):
    def test_fixed_route_goes_above_param_route_and_keeps_everything(self):
        gen = '@router.get("/count")\ndef get_count(repo=Depends(get_repo)):\n    return {"count": len(repo.read_all())}\n'
        p = place_python(ALERTS, gen)
        self.assertTrue(p.ok, p.error)
        out = p.new_src
        self.assertLess(out.index("def get_count"), out.index("def get_one"))
        self.assertGreater(out.index("def get_count"), out.index("def list_all"))
        for name in ("list_all", "get_one", "delete", "get_repo"):   # nothing removed
            self.assertIn(f"def {name}", out)
        self.assertIn("route `/count` placed above `get_one`", p.notes[0])

    def test_param_route_goes_after_last_route(self):
        gen = '@router.patch("/{id}/ack")\ndef ack(id: str):\n    return id\n'
        p = place_python(ALERTS, gen)
        self.assertTrue(p.ok, p.error)
        self.assertGreater(p.new_src.index("def ack"), p.new_src.index("def delete"))

    def test_new_import_added_once_after_imports(self):
        gen = "from fastapi import APIRouter, Depends\nimport structlog\n\n\ndef helper():\n    return 1\n"
        p = place_python(ALERTS, gen)
        self.assertTrue(p.ok, p.error)
        self.assertEqual(p.new_src.count("import structlog"), 1)
        self.assertEqual(p.new_src.count("from fastapi import APIRouter, Depends"), 1)
        self.assertLess(p.new_src.index("import structlog"), p.new_src.index("router = APIRouter"))

    def test_unchanged_copy_of_existing_code_is_skipped(self):
        gen = ('def get_repo():\n    return None\n\n\n'
               '@router.get("/count")\ndef get_count():\n    return {"count": 0}\n')
        p = place_python(ALERTS, gen)
        self.assertTrue(p.ok, p.error)
        self.assertEqual(p.new_src.count("def get_repo"), 1)
        self.assertTrue(any("skipped an unchanged copy" in n for n in p.notes))

    def test_redefining_existing_function_is_rejected(self):
        p = place_python(ALERTS, "def get_one(id):\n    return 42\n")
        self.assertFalse(p.ok)
        self.assertIn("already exists", p.error)

    def test_method_goes_into_the_only_class(self):
        src = "class Repo:\n    def read_all(self):\n        return []\n\n\ndef f():\n    pass\n"
        p = place_python(src, "def count(self):\n    return len(self.read_all())\n")
        self.assertTrue(p.ok, p.error)
        self.assertIn("    def count(self):", p.new_src)
        self.assertLess(p.new_src.index("def count"), p.new_src.index("def f"))

    def test_extract_code_from_reply(self):
        reply = "Sure:\n```python\ndef a():\n    return 1\n```\nDone."
        self.assertEqual(extract_code(reply), "def a():\n    return 1")
        self.assertIsNone(extract_code("I cannot do that."))

    def test_bad_generated_code_is_rejected(self):
        p = place_python(ALERTS, "def broken(:\n")
        self.assertFalse(p.ok)
        self.assertIn("does not parse", p.error)


class TestEditMode(unittest.TestCase):
    def test_choice(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(choose_edit_mode("Add a GET /count endpoint", ["a.py"], root)[0], "insert")
            self.assertEqual(choose_edit_mode("Fix mean for empty lists", ["a.py"], root)[0], "diff")
            self.assertEqual(choose_edit_mode("Add a function", ["a.py", "b.py"], root)[0], "diff")
            self.assertEqual(choose_edit_mode("Add a function", ["a.js"], root)[0], "diff")
            self.assertEqual(choose_edit_mode("Fix it", ["a.py"], root, "insert")[0], "insert")


if __name__ == "__main__":
    unittest.main()
