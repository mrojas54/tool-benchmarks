import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from toolbench.complex import (
    BANNED_TOOLS,
    DEFAULT_FIXTURE_ROOT,
    DEFECTS,
    ArmSpec,
    _command_escapes_gate,
    DefectSpec,
    ProfileRow,
    TrialResult,
    Truth,
    arm_violations,
    build_arms,
    build_profile,
    derive_test_gate,
    find_located,
    load_calls,
    load_defects,
    located_correct,
    read_escapes,
    render_profile,
    score_trial,
)
from toolbench.transcript import ToolCall, UsageProvenance

FIXTURE = "tests/fixtures/complex_session_located.jsonl"

# `DefectSpec` requires a real oracle (Tasks 1-3): a scoped `vitest run` command,
# and `GATE` is that command's OWN derivation (`derive_test_gate`), matching the
# convention enforced by `TestGateTests` above -- a gate must come from its own
# defect's oracle, never be hand-picked to match a fixture's expectations.
GATE = "Bash(npx vitest run:*)"

D_FIX = DefectSpec(
    id="DT",
    repo="wids",
    language="typescript",
    truth=Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20)),
    predicted_winner="native",
    rationale="test fixture",
    oracle_cmd=("npx", "vitest", "run", "tests/schedule.test.ts"),
    oracle_cwd=".",
    test_gate=GATE,
)


def _arm(name: str) -> ArmSpec:
    return next(a for a in build_arms(GATE) if a.name == name)


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


class LocatedTests(unittest.TestCase):
    def test_finds_the_located_line_and_its_timestamp(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        ts, obj = hit
        self.assertEqual(ts, "2026-07-12T10:00:02Z")
        self.assertEqual(obj["symbol"], "formatSlot")

    def test_correct_when_file_and_symbol_match_and_lines_overlap(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        self.assertTrue(
            located_correct(hit[1], Truth("web/src/lib/schedule.ts", "formatSlot", (15, 18)))
        )

    def test_right_symbol_in_the_wrong_file_is_not_a_hit(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        self.assertFalse(
            located_correct(hit[1], Truth("web/src/lib/other.ts", "formatSlot", (15, 18)))
        )

    def test_disjoint_line_ranges_are_not_a_hit(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        self.assertFalse(
            located_correct(hit[1], Truth("web/src/lib/schedule.ts", "formatSlot", (90, 99)))
        )

    def test_a_session_that_never_locates_returns_none(self) -> None:
        self.assertIsNone(find_located("tests/fixtures/complex_session_agent_escape.jsonl"))

    def test_an_overlap_everything_claim_is_rejected_not_scored_correct(self) -> None:
        # THE defect this guards against: overlap alone is not a guard, because
        # [0, 99999] overlaps every truth there is. A sloppy (or gaming) agent
        # claiming a vacuous span must not be scored as a correct localization --
        # that silently inflates the benchmark's one load-bearing number.
        truth = Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20))
        obj: dict[str, object] = {"file": truth.file, "symbol": truth.symbol, "lines": [0, 99999]}
        self.assertFalse(located_correct(obj, truth))

    def test_a_claim_spanning_most_of_a_real_file_is_rejected(self) -> None:
        # wids-D2's truth is a single line (32, 32). [1, 200] -- most of a
        # 200-line file -- overlaps it trivially but describes nothing: a
        # file-sized guess, not a find.
        truth = Truth("web/lib/paperpal/hint.ts", "fetchHint", (32, 32))
        obj: dict[str, object] = {"file": truth.file, "symbol": truth.symbol, "lines": [1, 200]}
        self.assertFalse(located_correct(obj, truth))

    def test_a_slightly_offset_claim_still_passes(self) -> None:
        # Guard against over-tightening: a range a few lines off from truth (not
        # exact, not huge) is exactly the "reported the whole function body"
        # case located_correct exists to accept.
        truth = Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20))
        obj: dict[str, object] = {"file": truth.file, "symbol": truth.symbol, "lines": [10, 22]}
        self.assertTrue(located_correct(obj, truth))

    def test_serena_slash_separated_name_path_matches_a_dotted_truth(self) -> None:
        # THE cell serena is pre-registered to win. serena's find_symbol reports
        # name paths slash-separated (`TaskProgressColumn/render`); rich-D1's truth
        # symbol is dotted (`TaskProgressColumn.render`). Raw string equality scores
        # a correct localization as wrong. Compare normalized name-paths instead.
        truth = Truth("rich/progress.py", "TaskProgressColumn.render", (756, 771))
        for claim in (
            "TaskProgressColumn.render",
            "TaskProgressColumn/render",
            "TaskProgressColumn::render",
            "render",  # bare leaf name is a suffix of the truth path
        ):
            obj: dict[str, object] = {
                "file": truth.file, "symbol": claim, "lines": [756, 771]
            }
            self.assertTrue(located_correct(obj, truth), claim)

    def test_a_dotted_claim_matches_a_bare_truth_in_the_other_direction(self) -> None:
        # Suffix match is symmetric in which side is longer: `hint.fetchHint`
        # matches truth `fetchHint`.
        truth = Truth("web/lib/paperpal/hint.ts", "fetchHint", (32, 32))
        obj: dict[str, object] = {
            "file": truth.file, "symbol": "hint.fetchHint", "lines": [32, 32]
        }
        self.assertTrue(located_correct(obj, truth))

    def test_a_same_leaf_wrong_owner_symbol_does_not_match(self) -> None:
        # Suffix, not shared-leaf: ["SpinnerColumn","render"] is not a suffix of
        # ["TaskProgressColumn","render"], so a wrong-class render is still wrong.
        truth = Truth("rich/progress.py", "TaskProgressColumn.render", (756, 771))
        obj: dict[str, object] = {
            "file": truth.file, "symbol": "SpinnerColumn.render", "lines": [756, 771]
        }
        self.assertFalse(located_correct(obj, truth))

    def test_the_widest_real_symbols_own_span_still_passes(self) -> None:
        # maltese-D5's commitOneHandle is 24 lines -- the widest real symbol
        # shipped in probes/complex/*/truth.json. Naming that symbol's own,
        # exact span must still score correct: a bound tighter than a real
        # symbol would score a CORRECT localization as wrong, which is the
        # opposite failure from the one this guard fixes, and worse.
        truth = Truth(
            "falcon-detective/src/handlers/commit.ts", "commitOneHandle", (29, 52)
        )
        obj: dict[str, object] = {"file": truth.file, "symbol": truth.symbol, "lines": [29, 52]}
        self.assertTrue(located_correct(obj, truth))


class TrialScoringTests(unittest.TestCase):
    def test_n1_counts_only_calls_before_the_located_line(self) -> None:
        result = score_trial(FIXTURE, D_FIX, _arm("native"), trial=1, fixed=True, trial_root=TRIAL_ROOT)
        self.assertTrue(result.located)
        # Grep's tool_result is "web/src/lib/schedule.ts:12:formatSlot" (37 chars),
        # joined BEFORE the LOCATED: line -- DERIVED, not copied from the brief:
        # ToolCall.tokens == output_chars // 4 == 37 // 4 == 9.
        # Edit's tool_result is "ok" (2 chars // 4 == 0), joined AFTER LOCATED:.
        self.assertEqual(result.n1, 9)
        self.assertEqual(result.n2, 0)

    def test_unlocated_but_fixed_records_no_navigation_number(self) -> None:
        # Guessing its way to green is a real outcome and must stay visible.
        wrong = replace(D_FIX, truth=Truth("nope.ts", "nope", (1, 2)))
        result = score_trial(FIXTURE, wrong, _arm("native"), trial=1, fixed=True, trial_root=TRIAL_ROOT)
        self.assertFalse(result.located)
        self.assertIsNone(result.n1)
        self.assertTrue(result.fixed)

    def test_a_fixed_but_unlocated_trial_has_no_n2_at_all(self) -> None:
        # N2 is EDIT cost: tokens from the LOCATED: line onward. A trial that
        # never located has no such boundary, so it has no N2 -- back-filling it
        # with the whole trial silently books that arm's entire NAVIGATION cost as
        # its edit cost, and `median_n2` then medians two different quantities
        # under one label. The design's rule: "a solved fix with no navigation
        # measurement -- recorded as such, not back-filled."
        wrong = replace(D_FIX, truth=Truth("nope.ts", "nope", (1, 2)))
        result = score_trial(FIXTURE, wrong, _arm("native"), trial=1, fixed=True, trial_root=TRIAL_ROOT)
        self.assertFalse(result.located)
        self.assertTrue(result.fixed)
        self.assertIsNone(result.n2)

    def test_total_is_always_defined_so_an_unlocated_fix_keeps_a_real_cost(self) -> None:
        # Dropping the back-fill must not lose the number. `total` is every call's
        # tokens, defined for every trial -- it is simply not called N2.
        wrong = replace(D_FIX, truth=Truth("nope.ts", "nope", (1, 2)))
        result = score_trial(FIXTURE, wrong, _arm("native"), trial=1, fixed=True, trial_root=TRIAL_ROOT)
        # Grep's result is 37 chars (37 // 4 == 9), Edit's is 2 chars (2 // 4 == 0).
        self.assertEqual(result.total, 9)

    def test_total_equals_n1_plus_n2_when_the_trial_located(self) -> None:
        result = score_trial(FIXTURE, D_FIX, _arm("native"), trial=1, fixed=True, trial_root=TRIAL_ROOT)
        assert result.n1 is not None and result.n2 is not None
        self.assertEqual(result.total, result.n1 + result.n2)

    def test_a_call_outside_the_arm_is_a_violation(self) -> None:
        calls = load_calls(FIXTURE)  # fixture uses Grep + Edit
        self.assertEqual(arm_violations(calls, _arm("serena")), ("Edit", "Grep"))

    def test_the_agent_tool_is_a_violation_even_for_the_control_arm(self) -> None:
        # The ban is verified from the transcript, never trusted from the flag.
        calls = load_calls("tests/fixtures/complex_session_agent_escape.jsonl")
        self.assertIn("Task", arm_violations(calls, _arm("control")))

    def test_a_bash_call_that_chains_past_the_gate_is_a_violation(self) -> None:
        # The whole point of I5: a serena-arm agent that reaches rg by chaining
        # inside its test-command gate (`npx vitest run; rg formatSlot`) escaped
        # the restriction. Collapsing every Bash rule to the tool name "Bash" made
        # that invisible to the audit built to catch it -- inspect the command.
        calls = load_calls("tests/fixtures/complex_session_gate_escape.jsonl")
        violations = arm_violations(calls, _arm("serena"))
        self.assertTrue(
            any(v.startswith("Bash:") and "rg formatSlot" in v for v in violations),
            violations,
        )

    def test_a_clean_gated_bash_call_is_not_flagged(self) -> None:
        # The first call in the fixture is exactly the permitted oracle invocation
        # (`npx vitest run <path>`). It must NOT be reported, or the audit cries
        # wolf on every legitimate fix checkpoint.
        calls = load_calls("tests/fixtures/complex_session_gate_escape.jsonl")
        violations = arm_violations(calls, _arm("serena"))
        self.assertFalse(
            any(v.startswith("Bash:") and "hint.test.ts" in v for v in violations),
            violations,
        )

    def test_the_full_bash_arm_is_not_flagged_for_chaining(self) -> None:
        # The bash arm is granted a full shell on purpose; chaining is not an
        # escape there, so command inspection must not fire.
        calls = load_calls("tests/fixtures/complex_session_gate_escape.jsonl")
        self.assertEqual(arm_violations(calls, _arm("bash")), ())


class GateTokenBoundaryTests(unittest.TestCase):
    """F3: `_command_escapes_gate` matched the prefix with a bare `startswith`, so
    `npx vitest runx` and `cargo testevil` shared the prefix, carried no shell
    operator, and were scored PERMITTED though they are different binaries. Since
    the gate audit is the primary enforcement (the --allowedTools flag does not
    restrict reads), a prefix must match only at a token boundary."""

    PREFIXES = ("npx vitest run", "cargo test")

    def test_prefix_glued_to_more_word_chars_escapes(self) -> None:
        self.assertTrue(_command_escapes_gate("npx vitest runx", self.PREFIXES))
        self.assertTrue(_command_escapes_gate("cargo testevil", self.PREFIXES))

    def test_exact_prefix_is_permitted(self) -> None:
        self.assertFalse(_command_escapes_gate("npx vitest run", self.PREFIXES))
        self.assertFalse(_command_escapes_gate("cargo test", self.PREFIXES))

    def test_prefix_then_space_is_permitted(self) -> None:
        self.assertFalse(
            _command_escapes_gate("npx vitest run tests/foo.test.ts", self.PREFIXES)
        )

    def test_prefix_then_tab_is_permitted(self) -> None:
        self.assertFalse(_command_escapes_gate("cargo test\t--lib", self.PREFIXES))

    def test_a_chained_command_after_the_prefix_still_escapes(self) -> None:
        # The shell-operator half of the check is unchanged.
        self.assertTrue(
            _command_escapes_gate("npx vitest run; rg formatSlot", self.PREFIXES)
        )


def _rc(name: str, **inp: object) -> ToolCall:
    """A ToolCall carrying `inp` as its kept raw_input -- the shape read_escapes sees."""
    return ToolCall(
        agent="claude-code",
        source="raw",
        project="p",
        name=name,
        input_chars=0,
        output_chars=0,
        session_id="s",
        ts="2026-01-01T00:00:00Z",
        usage=None,
        usage_provenance=UsageProvenance.ABSENT_BY_SCHEMA,
        duration_ms=None,
        error=None,
        model=None,
        raw_input=json.dumps(inp),
    )


_SERENA = "mcp__plugin_serena_serena__"
# A concrete, absent-from-disk temp path: read_escapes must be pure lexical and
# never stat it, so it need not exist.
TRIAL_ROOT = Path("/tmp/tb-trial-xyz")


class ReadEscapeTests(unittest.TestCase):
    """The primary arm-enforcement gate: any read outside the trial tree voids
    the trial. Precise for structured read tools; best-effort for full-shell Bash."""

    def test_native_read_escaping_the_tree_is_flagged_intree_is_not(self) -> None:
        escaped = read_escapes([_rc("Read", file_path="../../secret.txt")], TRIAL_ROOT)
        self.assertTrue(any(e.startswith("ReadEscape:") for e in escaped), escaped)
        clean = read_escapes([_rc("Read", file_path="src/schedule.ts")], TRIAL_ROOT)
        self.assertEqual(clean, ())

    def test_native_grep_with_absolute_path_outside_the_tree_is_flagged(self) -> None:
        calls = [_rc("Grep", pattern="x", path="/Users/me/corpus/wids")]
        escaped = read_escapes(calls, TRIAL_ROOT)
        self.assertTrue(any("/Users/me/corpus/wids" in e for e in escaped), escaped)

    def test_serena_read_file_escaping_relative_path_is_flagged_intree_is_not(self) -> None:
        out = read_escapes(
            [_rc(f"{_SERENA}read_file", relative_path="../../corpus/wids/web/lib/x.ts")],
            TRIAL_ROOT,
        )
        self.assertTrue(any(e.startswith("ReadEscape:") for e in out), out)
        intree = read_escapes(
            [_rc(f"{_SERENA}read_file", relative_path="web/lib/x.ts")], TRIAL_ROOT
        )
        self.assertEqual(intree, ())

    def test_bash_absolute_read_outside_is_flagged_oracle_and_intree_are_not(self) -> None:
        outside = read_escapes(
            [_rc("Bash", command="cat /Users/me/corpus/wids/web/lib/x.ts")], TRIAL_ROOT
        )
        self.assertTrue(any("/Users/me/corpus/wids/web/lib/x.ts" in e for e in outside), outside)
        self.assertEqual(read_escapes([_rc("Bash", command="npx vitest run")], TRIAL_ROOT), ())
        self.assertEqual(read_escapes([_rc("Bash", command="cat ./src/x.ts")], TRIAL_ROOT), ())

    def test_bash_dotdot_read_escaping_the_tree_is_flagged(self) -> None:
        out = read_escapes([_rc("Bash", command="rg formatSlot ../../corpus")], TRIAL_ROOT)
        self.assertTrue(any("../../corpus" in e for e in out), out)

    def test_escapes_are_returned_sorted(self) -> None:
        calls = [
            _rc("Bash", command="cat /z/late.ts"),
            _rc("Read", file_path="/a/early.ts"),
        ]
        out = read_escapes(calls, TRIAL_ROOT)
        self.assertEqual(list(out), sorted(out))
        self.assertEqual(len(out), 2)


def _trial(
    arm: str,
    located: bool,
    fixed: bool,
    n1: int | None,
    n2: int | None,
    violations: tuple[str, ...] = (),
    total: int = 0,
    read_escapes: tuple[str, ...] = (),
) -> TrialResult:
    return TrialResult(
        "D1", "wids", arm, 1, located, fixed, n1, n2, total, 3, violations, read_escapes
    )


class ProfileTests(unittest.TestCase):
    def test_median_cost_counts_only_solved_trials(self) -> None:
        rows: list[ProfileRow] = build_profile([
            _trial("serena", True, True, 100, 10),
            _trial("serena", True, True, 300, 10),
            _trial("serena", False, False, None, None),  # must not drag the median
        ])
        row = next(r for r in rows if r.arm == "serena")
        self.assertEqual(row.median_n1, 200)
        self.assertAlmostEqual(row.locate_rate, 2 / 3)

    def test_median_n2_medians_only_trials_that_both_located_and_fixed(self) -> None:
        # N1 and N2 partition ONE trial's cost at the LOCATED: line. A trial that
        # fixed without locating has no such line, so it contributes no N2 -- and
        # if it were allowed to, `median_n2` would be the median of a mixture of
        # edit costs and whole-trial costs, reported under the edit-cost label.
        rows = build_profile([
            _trial("serena", True, True, 100, 10),
            _trial("serena", True, True, 100, 20),
            _trial("serena", False, True, None, None, total=9999),
        ])
        row = next(r for r in rows if r.arm == "serena")
        self.assertEqual(row.median_n2, 15)

    def test_fixed_but_unlocated_trials_are_counted_in_the_row(self) -> None:
        # Dropping N2 must not drop the TRIAL. Visibly incomplete, never quietly
        # wrong: the row has to say how many of its fixes carry no N1/N2 at all.
        rows = build_profile([
            _trial("serena", True, True, 100, 10),
            _trial("serena", False, True, None, None, total=9999),
        ])
        row = next(r for r in rows if r.arm == "serena")
        self.assertEqual(row.fixed_unlocated, 1)
        self.assertEqual(row.unsolved, 0)

    def test_fixed_but_unlocated_trials_are_rendered_not_silently_dropped(self) -> None:
        text = render_profile(build_profile([
            _trial("serena", False, True, None, None, total=9999),
        ]))
        self.assertIn("fixed, unlocated", text)

    def test_the_dash_legend_does_not_call_a_solved_cell_unsolved(self) -> None:
        # F4: C2 made median N2 render `—` for fixed-but-unlocated trials, which ARE
        # solved. The old legend said `—` means "no solved trial", which misreads
        # that solved cell as unsolved. `—` means the cost metric has no defined
        # sample; the fixed-but-unlocated case is explained on its own line.
        text = render_profile(build_profile([
            _trial("serena", False, True, None, None, total=9999),
        ]))
        self.assertNotIn("no solved trial", text)
        self.assertIn("no defined sample", text)

    def test_an_arm_that_never_solves_reports_no_cost_at_all(self) -> None:
        # Its cheapness is meaningless; a number here would be a lie.
        rows = build_profile([_trial("bash", False, False, None, None)])
        self.assertIsNone(rows[0].median_n1)

    def test_unsolved_trials_are_named_in_the_report_not_dropped(self) -> None:
        text = render_profile(build_profile([_trial("bash", False, False, None, None)]))
        self.assertIn("Unsolved trials: 1", text)

    def test_a_violation_is_shouted_because_it_voids_the_arm(self) -> None:
        text = render_profile(build_profile([_trial("serena", True, True, 5, 5, ("Task",))]))
        self.assertIn("VIOLATION", text)

    def test_a_void_trial_is_excluded_from_rates_and_medians_and_counted(self) -> None:
        # A read-scope escape voids the trial: its numbers may be free, so they
        # must not enter locate/fix rates or the cost medians. OLD behavior counted
        # every trial in the rates -- with the void trial folded in, median_n1 would
        # be median(100, 999) == 549 and both rates 100%. The change: only the clean
        # trial is measured, so median_n1 == 100, rates over 1 valid trial, void == 1.
        rows = build_profile([
            _trial("serena", True, True, 100, 10),
            _trial(
                "serena", True, True, 999, 999,
                read_escapes=("ReadEscape:Bash:/Users/me/corpus/x.ts",),
            ),
        ])
        row = next(r for r in rows if r.arm == "serena")
        self.assertEqual(row.void, 1)
        self.assertEqual(row.median_n1, 100)
        self.assertEqual(row.median_n2, 10)
        self.assertAlmostEqual(row.locate_rate, 1.0)
        self.assertAlmostEqual(row.fix_rate, 1.0)

    def test_a_void_trial_is_named_in_the_report(self) -> None:
        text = render_profile(build_profile([
            _trial(
                "serena", True, True, 5, 5,
                read_escapes=("ReadEscape:Bash:/Users/me/corpus/x.ts",),
            ),
        ]))
        self.assertIn("/Users/me/corpus/x.ts", text)

    def test_full_shell_arm_gets_a_best_effort_audit_disclosure(self) -> None:
        # A full shell can read via indirection no static audit sees, so the report
        # must disclose that bash/control are audited best-effort -- never claim the
        # Bash read audit is complete.
        text = render_profile(build_profile([_trial("bash", True, True, 5, 5)]))
        self.assertIn("best-effort", text.lower())
