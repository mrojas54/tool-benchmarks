"""C7 -- the acceptance test for the whole fixture set.

For each of the 8 defects this provisions a real trial tree and asserts both
halves of what makes a fixture valid:

  1. clean tree (apply_defect=False) -> oracle GREEN (exit 0)
  2. defect tree (apply_defect=True)  -> oracle RED  (non-zero exit)

A patch that applies cleanly but breaks nothing (or breaks everything, or breaks
a test unrelated to the defect) is otherwise indistinguishable from a good one
and would sail into the pilot. This is the first time any defect patch is run
against its own oracle; C1 and C6 would both have been caught by it on first run.

It needs the vendored corpus, `npm ci`, `cargo`, and a python venv, so it is slow
and must NOT run in the fast suite: it is gated behind TOOLBENCH_CORPUS_TESTS=1
and skips otherwise, following the suite's existing skipTest idiom
(tests/test_hermes.py::LiveArchive).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from toolbench.complex import DEFECTS, build_arms
from toolbench.complex_runner import ensure_deps, provision_worktree, shell_oracle

_CORPUS_ROOT = Path(__file__).resolve().parent.parent / "corpus"


class FixtureRedGreen(unittest.TestCase):
    """Every defect must turn its own oracle from GREEN to RED, and nothing else."""

    def _require_corpus(self) -> None:
        if not os.environ.get("TOOLBENCH_CORPUS_TESTS"):
            self.skipTest(
                "slow corpus test; set TOOLBENCH_CORPUS_TESTS=1 (needs the vendored "
                "corpus, npm ci, cargo, and a python venv)"
            )
        if not (_CORPUS_ROOT / "manifest.json").exists():
            self.skipTest("no vendored corpus; run corpus/vendor.sh first")

    def _oracle_passes(self, defect_repo: str, defect_id: str, apply_defect: bool) -> bool:
        defect = next(d for d in DEFECTS if d.repo == defect_repo and d.id == defect_id)
        arm = next(a for a in build_arms(defect.test_gate) if a.name == "bash")
        oracle = shell_oracle(list(defect.oracle_cmd), defect.oracle_cwd)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "tree"
            provision_worktree(
                defect,
                arm,
                1,
                _CORPUS_ROOT,
                dest,
                apply_defect=apply_defect,
            )
            return oracle(dest)

    def test_every_defect_is_green_when_clean_and_red_when_applied(self) -> None:
        self._require_corpus()
        # Provision dep caches once up front (idempotent) rather than per subtest.
        for repo in {d.repo for d in DEFECTS}:
            ensure_deps(_CORPUS_ROOT, repo)

        for defect in DEFECTS:
            with self.subTest(defect=f"{defect.repo}/{defect.id}"):
                clean_green = self._oracle_passes(defect.repo, defect.id, apply_defect=False)
                self.assertTrue(
                    clean_green,
                    f"{defect.repo}/{defect.id}: clean tree is not GREEN -- the oracle "
                    f"fails before the defect is applied (broken fixture or missing deps)",
                )
                defect_red = not self._oracle_passes(
                    defect.repo, defect.id, apply_defect=True
                )
                self.assertTrue(
                    defect_red,
                    f"{defect.repo}/{defect.id}: defect tree is not RED -- the patch "
                    f"applies but does not break its oracle (broken fixture)",
                )


if __name__ == "__main__":
    unittest.main()
