import tempfile
import unittest
from pathlib import Path

from toolbench.complex import BANNED_TOOLS, DEFECTS, build_arms, load_defects

FIXTURE_ROOT = Path("probes/complex")


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

    def test_repo_id_pairs_are_unique_but_ids_alone_are_not(self) -> None:
        # This is the real trap: both wids and maltese have a D3. Anything that
        # keys on id alone (a dict, a lookup) would silently collide them.
        pairs = [(d.repo, d.id) for d in DEFECTS]
        self.assertEqual(len(pairs), len(set(pairs)), "duplicate (repo, id) pair")

        ids = [d.id for d in DEFECTS]
        self.assertNotEqual(len(ids), len(set(ids)), "expected id collisions across repos")

        self.assertIn(("wids", "D3"), pairs)
        self.assertIn(("maltese", "D3"), pairs)

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
            fixture = root / "acme-D9-missing-truth"
            fixture.mkdir()
            (fixture / "oracle.json").write_text(
                '{"cmd": ["echo", "ok"], "cwd": ".", "language": "python"}',
                encoding="utf-8",
            )
            (fixture / "prediction.md").write_text(
                "Predicted winner: neutral\n\nRationale: filler.\n", encoding="utf-8"
            )
            with self.assertRaises(Exception) as ctx:
                load_defects(root)
            message = str(ctx.exception)
            self.assertIn("truth.json", message)
            self.assertIn("acme-D9-missing-truth", message)
