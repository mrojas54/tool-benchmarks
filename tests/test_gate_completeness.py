"""Regression test for TB-19.

`unittest.TestLoader.discover` only finds `unittest.TestCase` methods; it is
blind to module-level `test_*` functions, which `pytest` collects just as
readily. This is the exact defect TB-19 exists to fix (the documented /
gate command must be `pytest`, not `unittest discover`) — pinned here against
a synthetic fixture package so the guarantee holds regardless of what the
real `tests/` directory looks like later.

This test is itself a module-level function: if the gate ever regresses back
to `unittest discover`, this file's own single class-based sibling test would
still run, but this test would silently vanish — a live instance of the bug
it documents.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_FIXTURE_MODULE = textwrap.dedent(
    """
    import unittest

    def test_module_level_marker():
        assert True

    class ClassBasedMarker(unittest.TestCase):
        def test_class_based_marker(self):
            assert True
    """
)


def _write_fixture_package(root: Path) -> Path:
    pkg = root / "sample_tests"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "test_fixture_module.py").write_text(_FIXTURE_MODULE)
    return pkg


def test_unittest_discover_misses_module_level_functions_pytest_does_not(
    tmp_path: Path,
) -> None:
    pkg = _write_fixture_package(tmp_path)

    unittest_result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(pkg)],
        capture_output=True,
        text=True,
        check=False,
    )
    pytest_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", str(pkg)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert "Ran 1 test" in unittest_result.stderr, unittest_result.stderr
    assert "2 tests collected" in pytest_result.stdout, pytest_result.stdout


class GateCompletenessSanityCheck(unittest.TestCase):
    """A `TestCase` sibling: proves the fixture assertions above aren't a fluke of pytest-only collection."""

    def test_fixture_module_has_exactly_one_module_level_and_one_class_based_test(
        self,
    ) -> None:
        import ast

        tree = ast.parse(_FIXTURE_MODULE)
        module_level = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test")
        ]
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertEqual(len(module_level), 1)
        self.assertEqual(len(classes), 1)
