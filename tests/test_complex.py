import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from toolbench.complex import (
    BANNED_TOOLS,
    DEFAULT_FIXTURE_ROOT,
    DEFECTS,
    build_arms,
    derive_test_gate,
    load_defects,
)


def _write_fixture(
    root: Path,
    name: str,
    *,
    lines: str = "[1, 2]",
    cmd: str = '["echo", "ok"]',
) -> None:
    """A minimal well-formed fixture dir, for the load_defects validation tests."""
    fixture = root / name
    fixture.mkdir()
    (fixture / "truth.json").write_text(
        f'{{"file": "a.ts", "symbol": "a", "lines": {lines}}}', encoding="utf-8"
    )
    (fixture / "oracle.json").write_text(
        f'{{"cmd": {cmd}, "cwd": ".", "language": "typescript"}}', encoding="utf-8"
    )
    (fixture / "prediction.md").write_text(
        "Predicted winner: neutral\n\nRationale: filler.\n", encoding="utf-8"
    )


class ArmSpecTests(unittest.TestCase):
    def test_every_arm_gets_read_todowrite_and_the_test_gate(self) -> None:
        for arm in build_arms("Bash(cargo test:*)"):
            self.assertIn("Read", arm.allowed_tools, arm.name)
            self.assertIn("TodoWrite", arm.allowed_tools, arm.name)

    def test_no_arm_may_carry_the_agent_tool(self) -> None:
        # A subagent inherits a full toolset: a serena-only arm could spawn one,
        # run rg inside it, and hand back the answer. The restriction would look
        # enforced and be void.
        for arm in build_arms("Bash(cargo test:*)"):
            for banned in BANNED_TOOLS:
                self.assertNotIn(banned, arm.allowed_tools, f"{arm.name} carries {banned}")

    def test_serena_arm_has_no_search_shell_only_the_test_gate(self) -> None:
        serena = next(a for a in build_arms("Bash(cargo test:*)") if a.name == "serena")
        self.assertNotIn("Bash", serena.allowed_tools)
        self.assertIn("Bash(cargo test:*)", serena.allowed_tools)

    def test_all_four_arms_are_built(self) -> None:
        names = {a.name for a in build_arms("Bash(cargo test:*)")}
        self.assertEqual(names, {"serena", "native", "bash", "control"})


class LoadDefectsTests(unittest.TestCase):
    """DEFECTS must be derived from the committed fixtures under probes/complex/,
    never hand-written -- a hand-maintained tuple can silently drift from the
    patches it claims to describe. These tests read only committed fixtures, so
    they stay hermetic (no clone, no network).
    """

    def test_exactly_eight_fixtures_load(self) -> None:
        self.assertEqual(len(DEFECTS), 8)

    def test_module_level_defects_matches_a_fresh_load(self) -> None:
        self.assertEqual(DEFECTS, load_defects())

    def test_repo_id_pairs_are_unique(self) -> None:
        pairs = [(d.repo, d.id) for d in DEFECTS]
        self.assertEqual(len(pairs), len(set(pairs)), "duplicate (repo, id) pair")

    def test_load_defects_keys_on_repo_and_id_not_id_alone(self) -> None:
        # The real trap: an id is NOT unique -- wids and maltese both ship a D3.
        # Anything keying on id alone (a dict, a lookup, a dedupe) silently drops
        # one of them. Asserted directly on the loader with two same-id fixtures
        # in different repos, rather than by asserting the shipped fixtures happen
        # to collide -- renaming a fixture must not be able to fail this test.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, "wids-D9-same-id")
            _write_fixture(root, "maltese-D9-same-id")
            loaded = load_defects(root)
            self.assertEqual(len(loaded), 2)
            self.assertEqual({d.id for d in loaded}, {"D9"})
            self.assertEqual({d.repo for d in loaded}, {"wids", "maltese"})

    def test_every_predicted_winner_is_a_known_arm_or_neutral(self) -> None:
        allowed = {"serena", "native", "bash", "neutral"}
        for d in DEFECTS:
            self.assertIn(d.predicted_winner, allowed, d.id)

    def test_every_truth_file_is_a_nonempty_relative_path_with_ordered_lines(self) -> None:
        for d in DEFECTS:
            self.assertTrue(d.truth.file, d.id)
            self.assertFalse(Path(d.truth.file).is_absolute(), d.id)
            self.assertEqual(len(d.truth.lines), 2, d.id)
            start, end = d.truth.lines
            self.assertLessEqual(start, end, d.id)

    def test_every_oracle_cmd_is_a_nonempty_argv_list(self) -> None:
        for d in DEFECTS:
            self.assertIsInstance(d.oracle_cmd, tuple, d.id)
            self.assertGreater(len(d.oracle_cmd), 0, d.id)
            for token in d.oracle_cmd:
                self.assertIsInstance(token, str, d.id)

    def test_maltese_d3_oracle_is_the_scoped_lib_command_never_the_workspace_run(self) -> None:
        # maltese D3's scoped `cargo test -p falcon-mcp --lib sandbox` runs in 3.1s;
        # the unscoped workspace run measured 46.2s cold. This runs once per trial,
        # dozens of times -- getting this wrong is a real time bomb, not a nitpick.
        d3 = next(d for d in DEFECTS if d.repo == "maltese" and d.id == "D3")
        self.assertEqual(
            d3.oracle_cmd, ("cargo", "test", "-p", "falcon-mcp", "--lib", "sandbox")
        )

    def test_missing_truth_json_raises_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "wids-D9-missing-truth"
            fixture.mkdir()
            (fixture / "oracle.json").write_text(
                '{"cmd": ["echo", "ok"], "cwd": ".", "language": "python"}',
                encoding="utf-8",
            )
            (fixture / "prediction.md").write_text(
                "Predicted winner: neutral\n\nRationale: filler.\n", encoding="utf-8"
            )
            with self.assertRaises(FileNotFoundError) as ctx:
                load_defects(root)
            message = str(ctx.exception)
            self.assertIn("truth.json", message)
            self.assertIn("wids-D9-missing-truth", message)

    def test_a_repo_name_the_manifest_does_not_declare_is_rejected(self) -> None:
        # `wid-D2-...` (a typo) parses cleanly as repo "wid" and would otherwise
        # load as a real defect pointing at a corpus that does not exist.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, "wid-D2-typo")
            with self.assertRaises(ValueError) as ctx:
                load_defects(root)
            self.assertIn("wid", str(ctx.exception))
            self.assertIn("manifest", str(ctx.exception))

    def test_a_duplicate_repo_id_pair_is_rejected(self) -> None:
        # Two fixtures claiming the same (repo, id) collide in every lookup keyed
        # on the pair. Loud at load, or silent forever.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, "wids-D9-first")
            _write_fixture(root, "wids-D9-second")
            with self.assertRaises(ValueError) as ctx:
                load_defects(root)
            self.assertIn("duplicate", str(ctx.exception).lower())

    def test_defects_load_from_any_cwd_not_just_the_repo_root(self) -> None:
        # The fixture root is anchored to the module, not to the process cwd: a
        # relative default made `import toolbench.complex` raise FileNotFoundError
        # from anywhere else.
        self.assertTrue(DEFAULT_FIXTURE_ROOT.is_absolute())
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            try:
                os.chdir(tmp)
                self.assertEqual(load_defects(), DEFECTS)
            finally:
                os.chdir(cwd)


class TestGateTests(unittest.TestCase):
    """The gate must be derived from the defect's OWN oracle, never from its repo.

    The gate was per-repo once. maltese's repo gate is `Bash(cargo test:*)` while
    three of its four defects are verified by vitest, and rich's was
    `Bash(python -m pytest:*)` while its oracle runs the venv's pytest. Half the
    cells told the agent to "make the test suite pass" while denying it the only
    command that proves it.
    """

    def test_every_defects_gate_matches_its_own_oracles_program(self) -> None:
        for d in DEFECTS:
            program = d.oracle_cmd[0]
            self.assertTrue(
                d.test_gate.startswith(f"Bash({program}"),
                f"{d.repo}/{d.id}: gate {d.test_gate!r} does not gate {program!r}",
            )
            self.assertTrue(d.test_gate.endswith(":*)"), d.id)

    def test_the_gate_is_exactly_the_derivation_of_its_oracle_cmd(self) -> None:
        for d in DEFECTS:
            self.assertEqual(d.test_gate, derive_test_gate(d.oracle_cmd), f"{d.repo}/{d.id}")

    def test_the_prefix_stops_at_the_first_argument_shaped_token(self) -> None:
        self.assertEqual(
            derive_test_gate(("npx", "vitest", "run", "tests/x.test.ts")),
            "Bash(npx vitest run:*)",
        )
        self.assertEqual(
            derive_test_gate(("cargo", "test", "-p", "falcon-mcp", "--lib", "sandbox")),
            "Bash(cargo test:*)",
        )
        self.assertEqual(
            derive_test_gate((".venv/bin/pytest", "tests/test_progress.py", "-q")),
            "Bash(.venv/bin/pytest:*)",
        )

    def test_no_defects_gate_hands_a_restricted_arm_a_bare_interpreter(self) -> None:
        # `Bash(python:*)` would let the serena arm run `python -c ...` -- arbitrary
        # code execution, i.e. a shell, i.e. the exact capability the arm withholds.
        for d in DEFECTS:
            gate = d.test_gate
            self.assertNotIn("(python:", gate, d.id)
            self.assertNotIn("(python3:", gate, d.id)
            self.assertNotIn("(node:", gate, d.id)
            self.assertNotIn("(sh:", gate, d.id)
            self.assertNotIn("(bash:", gate, d.id)

    def test_the_gate_reaches_the_restricted_arms(self) -> None:
        for d in DEFECTS:
            for arm in build_arms(d.test_gate):
                if arm.name == "bash":
                    continue  # a full shell subsumes the gate
                self.assertIn(d.test_gate, arm.allowed_tools, f"{d.repo}/{d.id}/{arm.name}")


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_TARGET_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")


def _patch_facts(patch_text: str) -> tuple[str, set[int]]:
    """(target path, post-image line numbers of every line the patch adds).

    Line numbers are POST-PATCH, matching Truth's documented convention: the
    counter advances on context and added lines and holds on removed ones, which
    is exactly the numbering the agent sees in its worktree.
    """
    target = ""
    added: set[int] = set()
    new_no = 0
    in_hunk = False
    for line in patch_text.splitlines():
        found = _TARGET_RE.match(line)
        if found:
            target = found.group("path")
            in_hunk = False
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_no = int(hunk.group("start"))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            added.add(new_no)
            new_no += 1
        elif line.startswith("-"):
            continue
        else:
            new_no += 1
    return target, added


# The most lines of truth range allowed BEYOND the region the patch actually
# changes. Containment alone is not a guard: a truth of [1, 5000] contains every
# patch ever written, so a test that only checks containment cannot fail, and a
# guard that cannot fail is worthless. (Proved: reverting rich-D1's truth to the
# stale [756,769] did not trip the containment check.)
#
# Why 30. `truth.lines` is not the changed line -- it is meant to bound the
# ENCLOSING SYMBOL, so that `located_correct`'s overlap match still scores an
# agent that reports the function rather than the exact line. So the bound has to
# admit a symbol and reject a file. The widest symbol shipped is maltese-D5's
# `commitOneHandle`: a 24-line function around a 1-line change, i.e. 23 lines of
# slack. 30 clears that with room for a somewhat larger function, while a
# file-sized range (progress.py alone is ~1700 lines) misses by two orders of
# magnitude. There is no gap in between that a legitimate truth would land in.
#
# This is also the only tightness check available here: the suite is hermetic (it
# never reads corpus/), so it cannot bound a truth against the real file's length.
MAX_TRUTH_SLACK = 30


def _truth_violations(added: set[int], truth_lines: tuple[int, int]) -> list[str]:
    """Every way a truth range can fail to describe the patch it ships with.

    Two independent failures, because each catches what the other cannot:
      - containment: the truth must cover every line the patch changed (a truth
        pointing somewhere else describes a defect that is not there);
      - tightness:   the truth must not be much wider than that changed region
        (a truth covering everything localizes nothing, and would score a
        blind guess as a correct find).
    """
    if not added:
        return ["defect.patch adds no line -- it seeds nothing"]

    start, end = truth_lines
    problems = [
        f"defect.patch changes post-patch line {lineno}, outside truth.lines "
        f"{truth_lines} -- the ground truth does not describe the defect it ships"
        for lineno in sorted(added)
        if not start <= lineno <= end
    ]

    changed_span = max(added) - min(added) + 1
    truth_span = end - start + 1
    slack = truth_span - changed_span
    if slack > MAX_TRUTH_SLACK:
        problems.append(
            f"truth.lines {truth_lines} spans {truth_span} lines around a "
            f"{changed_span}-line change -- {slack} lines of slack, over the "
            f"{MAX_TRUTH_SLACK} allowed. A truth this wide contains the defect "
            f"only in the sense that it contains everything; it localizes nothing."
        )
    return problems


class PatchTruthTests(unittest.TestCase):
    """Every truth.json must agree with the patch it claims to describe.

    Loading DEFECTS from the fixtures killed DEFECTS<->truth drift but not
    truth<->patch drift: a truth.json still asserted line numbers that nothing
    checked against the patch that produced them. Ground truth nobody can
    contradict is ground truth nobody can trust. Hermetic: reads only the
    committed patches, never corpus/.
    """

    def test_the_parser_agrees_with_a_known_patch(self) -> None:
        # Guard the guard: a hunk parser that silently returns nothing would make
        # every assertion below vacuous.
        text = (
            "--- a/x.ts\n"
            "+++ b/x.ts\n"
            "@@ -10,4 +10,5 @@\n"
            " ctx\n"
            "-old\n"
            "+new\n"
            "+extra\n"
            " ctx\n"
        )
        target, added = _patch_facts(text)
        self.assertEqual(target, "x.ts")
        self.assertEqual(added, {11, 12})

    def test_every_truth_both_contains_its_patch_and_stays_tight_around_it(self) -> None:
        for defect in DEFECTS:
            fixture = next(
                d
                for d in DEFAULT_FIXTURE_ROOT.iterdir()
                if d.is_dir() and d.name.startswith(f"{defect.repo}-{defect.id}-")
            )
            target, added = _patch_facts(
                (fixture / "defect.patch").read_text(encoding="utf-8")
            )
            label = f"{defect.repo}/{defect.id}"

            self.assertEqual(target, defect.truth.file, f"{label}: patch target != truth.file")
            self.assertTrue(added, f"{label}: defect.patch adds no line -- it seeds nothing")
            self.assertEqual(
                _truth_violations(added, defect.truth.lines), [], f"{label}"
            )

    def test_an_absurdly_wide_truth_is_rejected_even_though_it_contains_the_patch(
        self,
    ) -> None:
        # THE point of the tightness rule. [1, 5000] passes containment trivially --
        # it contains every patch there is -- and the containment-only guard this
        # replaces waved it through. If this test ever stops failing the wide truth,
        # the guard has gone vacuous again.
        wide = _truth_violations({760, 761, 762}, (1, 5000))
        self.assertTrue(wide, "a [1, 5000] truth must be rejected, and was not")
        self.assertIn("slack", " ".join(wide))
        # ...and the same patch under its real, symbol-sized truth passes.
        self.assertEqual(_truth_violations({760, 761, 762}, (756, 771)), [])

    def test_a_truth_pointing_away_from_the_patch_is_rejected(self) -> None:
        off = _truth_violations({112}, (200, 210))
        self.assertTrue(off)
        self.assertIn("outside truth.lines", " ".join(off))

    def test_the_widest_shipped_truth_sits_inside_the_slack_bound(self) -> None:
        # maltese-D5 (`commitOneHandle`, 24 lines around a 1-line change, 23 slack)
        # is what MAX_TRUTH_SLACK=30 is calibrated against. If a future fixture
        # needs more, that is a deliberate decision to make -- not a number to
        # quietly raise until the suite goes green.
        worst = max(
            (
                (defect.truth.lines[1] - defect.truth.lines[0] + 1)
                - (max(added) - min(added) + 1)
                for defect in DEFECTS
                for added in [
                    _patch_facts(
                        next(
                            d
                            for d in DEFAULT_FIXTURE_ROOT.iterdir()
                            if d.is_dir()
                            and d.name.startswith(f"{defect.repo}-{defect.id}-")
                        )
                        .joinpath("defect.patch")
                        .read_text(encoding="utf-8")
                    )[1]
                ]
            )
        )
        self.assertLessEqual(worst, MAX_TRUTH_SLACK)
        # The bound is not slack-plus-epsilon fitted to the fixtures, but it is
        # also not so wide it admits a file: assert it stays in symbol territory.
        self.assertLess(MAX_TRUTH_SLACK, 100)

    def test_every_fixture_ships_a_prompt(self) -> None:
        for fixture in sorted(DEFAULT_FIXTURE_ROOT.iterdir()):
            if fixture.is_dir():
                prompt = fixture / "prompt.md"
                self.assertTrue(prompt.exists(), fixture.name)
                text = prompt.read_text(encoding="utf-8")
                # The LOCATED: contract is what makes N1 measurable at all.
                self.assertIn("LOCATED:", text, fixture.name)
                self.assertIn("make the test suite pass", text, fixture.name)

    def test_rich_truth_is_recorded_in_post_patch_coordinates(self) -> None:
        # rich-D1 is the only patch that changes the line count (+2). Its truth was
        # recorded pre-patch, so its end line was short by exactly that delta --
        # the convention, once undefined, decided itself by accident.
        rich = next(d for d in DEFECTS if d.repo == "rich")
        self.assertEqual(rich.truth.lines, (756, 771))
        raw = json.loads(
            (DEFAULT_FIXTURE_ROOT / "rich-D1-render-collision" / "truth.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("POST-PATCH", raw["_convention"])
