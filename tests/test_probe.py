import tempfile
import unittest
from pathlib import Path

from toolbench.probe import (
    PROBE_SPECS,
    SEED_BASELINES,
    ArmMatch,
    SeededReportError,
    build_comparison_table,
    find_probe_calls,
    render_report,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


class ProbeSpecTests(unittest.TestCase):
    def test_five_specs(self) -> None:
        self.assertEqual(len(PROBE_SPECS), 5)

    def test_corpus_paths_are_real_vendored_files(self) -> None:
        for spec in PROBE_SPECS:
            self.assertTrue(spec.corpus_path.startswith("tools/"))
            self.assertTrue(
                (REPO_ROOT / spec.corpus_path).is_file(),
                f"{spec.corpus_path} missing on disk",
            )

    def test_sentinels_globally_unique_and_no_substrings(self) -> None:
        sentinels = []
        for spec in PROBE_SPECS:
            sentinels.append(spec.tool_sentinel)
            sentinels.append(spec.bash_sentinel)
        self.assertEqual(len(sentinels), len(set(sentinels)))
        for i, a in enumerate(sentinels):
            for j, b in enumerate(sentinels):
                if i == j:
                    continue
                self.assertNotIn(a, b, f"{a!r} is a substring of {b!r}")

    def test_active_probes_md_lists_all_five_paths(self) -> None:
        content = (REPO_ROOT / "protocols" / "active-probes.md").read_text()
        for spec in PROBE_SPECS:
            self.assertIn(spec.corpus_path, content)
            self.assertFalse(spec.corpus_path.startswith("reports/"))


class FindProbeCallsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matches = find_probe_calls(FIXTURES / "probe_session.jsonl")

    def test_full_match_tool_arm_isolable(self) -> None:
        arm = self.matches["01"]
        self.assertIsNotNone(arm.tool)
        assert arm.tool is not None
        self.assertEqual(arm.tool.name, "mcp__serena__find_file")
        self.assertEqual(arm.tool.output_chars, len("tools/regex_check.py"))
        self.assertTrue(arm.tool_isolable)

    def test_full_match_bash_arm_not_isolable(self) -> None:
        arm = self.matches["01"]
        self.assertIsNotNone(arm.bash)
        assert arm.bash is not None
        self.assertEqual(arm.bash.name, "Bash")
        expected = "12:SENTINEL TB_PROBE_01_BASH_V2 anchor comment"
        self.assertEqual(arm.bash.output_chars, len(expected))
        self.assertFalse(arm.bash_isolable, "turn had 2 tool_use blocks, usage isn't attributable")

    def test_right_tool_wrong_sentinel_is_not_a_match(self) -> None:
        # toolu_c1 uses find_file (the tool expected by probes 03/05 too) but
        # carries probe 02's sentinel -- must not match 03 or 05.
        self.assertIsNone(self.matches["03"].tool)
        self.assertIsNone(self.matches["05"].tool)

    def test_right_sentinel_wrong_tool_is_not_a_match(self) -> None:
        # toolu_d1 carries probe 01's TOOL sentinel but via Bash, not
        # mcp__serena__find_file -- must not overwrite the real tool-arm match
        # from toolu_a1, and must not be picked up as a bash-arm match either
        # (wrong sentinel for that arm).
        arm = self.matches["01"]
        assert arm.tool is not None
        self.assertEqual(arm.tool.output_chars, len("tools/regex_check.py"))

    def test_unrelated_probes_have_no_matches(self) -> None:
        for probe_id in ("02", "04"):
            arm = self.matches[probe_id]
            self.assertIsNone(arm.tool)
            self.assertIsNone(arm.bash)


class BuildComparisonTableTests(unittest.TestCase):
    def test_all_seeded_when_no_matches(self) -> None:
        empty: dict[str, ArmMatch] = {}
        rows = build_comparison_table(empty)
        self.assertEqual(len(rows), 5)
        by_id = {row.probe_id: row for row in rows}

        find_row = by_id["01"]
        self.assertEqual(find_row.task, "find")
        self.assertTrue(find_row.tool_seeded)
        self.assertTrue(find_row.bash_seeded)
        self.assertEqual(find_row.tool_tokens, SEED_BASELINES[("find", "serena")])
        self.assertEqual(find_row.bash_tokens, SEED_BASELINES[("find", "bash")])
        self.assertEqual(find_row.tool_tokens, 68)
        self.assertEqual(find_row.bash_tokens, 89)
        self.assertIsNone(find_row.tool_usage_tokens)

        search_row = by_id["02"]
        self.assertEqual(search_row.task, "search")
        self.assertEqual(search_row.tool_tokens, 723)
        self.assertEqual(search_row.bash_tokens, 794)

    def test_real_tokens_used_when_matched_isolable_usage_attributed(self) -> None:
        matches = find_probe_calls(FIXTURES / "probe_session.jsonl")
        rows = build_comparison_table(matches)
        row = {r.probe_id: r for r in rows}["01"]

        self.assertFalse(row.tool_seeded)
        self.assertEqual(row.tool_tokens, len("tools/regex_check.py") // 4)
        self.assertEqual(row.tool_usage_tokens, 9)

    def test_non_isolable_turn_context_tokens_kept_but_usage_omitted(self) -> None:
        matches = find_probe_calls(FIXTURES / "probe_session.jsonl")
        rows = build_comparison_table(matches)
        row = {r.probe_id: r for r in rows}["01"]

        self.assertFalse(row.bash_seeded)
        expected = "12:SENTINEL TB_PROBE_01_BASH_V2 anchor comment"
        self.assertEqual(row.bash_tokens, len(expected) // 4)
        self.assertIsNone(row.bash_usage_tokens)


class RenderReportTests(unittest.TestCase):
    def test_seeded_rows_are_marked(self) -> None:
        rows = build_comparison_table({})
        report = render_report(rows, allow_seeded=True)
        self.assertIn("68*", report)
        self.assertIn("seeded", report.lower())

    def test_main_writes_report_to_custom_out_path(self) -> None:
        from toolbench.probe import main

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "reports" / "active-probe-comparison.md"
            main(["--out", str(out_path), "--allow-seeded"])
            self.assertTrue(out_path.is_file())
            content = out_path.read_text()
            self.assertIn("Active probe comparison", content)


class SerenaToolNamingTests(unittest.TestCase):
    """The serena arm must name the tool as the runtime actually records it."""

    def test_specs_accept_plugin_namespaced_serena_names(self) -> None:
        for spec in PROBE_SPECS:
            bare = spec.tool_names[0].rsplit("__", 1)[-1]
            self.assertIn(
                f"mcp__plugin_serena_serena__{bare}",
                spec.tool_names,
                f"probe {spec.id} cannot match the name Claude Code records",
            )

    def test_primary_tool_name_is_the_plugin_namespaced_one(self) -> None:
        for spec in PROBE_SPECS:
            self.assertTrue(spec.tool_name.startswith("mcp__plugin_serena_serena__"))

    def test_plugin_namespaced_call_matches_the_tool_arm(self) -> None:
        matches = find_probe_calls(FIXTURES / "probe_session_plugin_names.jsonl")
        arm = matches["01"]
        self.assertIsNotNone(arm.tool, "plugin-namespaced serena call must match")
        assert arm.tool is not None
        self.assertEqual(arm.tool.name, "mcp__plugin_serena_serena__find_file")
        self.assertTrue(arm.tool_isolable)

    def test_plugin_namespaced_session_yields_unseeded_row(self) -> None:
        matches = find_probe_calls(FIXTURES / "probe_session_plugin_names.jsonl")
        row = {r.probe_id: r for r in build_comparison_table(matches)}["01"]
        self.assertFalse(row.tool_seeded)
        self.assertFalse(row.bash_seeded)
        self.assertEqual(row.tool_usage_tokens, 11)
        self.assertEqual(row.bash_usage_tokens, 7)


class ContaminationGuardTests(unittest.TestCase):
    """A call that *mentions* a sentinel is not a call that *performs* the probe."""

    def setUp(self) -> None:
        self.matches = find_probe_calls(FIXTURES / "probe_session_contaminated.jsonl")

    def test_multi_sentinel_grep_is_not_an_arm(self) -> None:
        for probe_id in ("01", "02"):
            arm = self.matches[probe_id]
            self.assertIsNone(arm.tool, f"probe {probe_id} tool arm matched a grep")
            self.assertIsNone(arm.bash, f"probe {probe_id} bash arm matched a grep")

    def test_single_sentinel_grep_over_transcripts_is_not_an_arm(self) -> None:
        self.assertIsNone(self.matches["03"].bash, "grep over ~/.claude/projects scored as an arm")

    def test_read_of_probe_source_is_not_an_arm(self) -> None:
        self.assertIsNone(self.matches["04"].tool)

    def test_contaminated_session_produces_a_fully_seeded_table(self) -> None:
        rows = build_comparison_table(self.matches)
        self.assertTrue(all(r.tool_seeded and r.bash_seeded for r in rows))


class SeededReportRefusalTests(unittest.TestCase):
    """A table with zero measurements must not render as a report."""

    def test_render_raises_when_every_arm_is_seeded(self) -> None:
        rows = build_comparison_table({})
        with self.assertRaises(SeededReportError):
            render_report(rows)

    def test_render_allows_fully_seeded_only_when_explicit(self) -> None:
        rows = build_comparison_table({})
        self.assertIn("Active probe comparison", render_report(rows, allow_seeded=True))

    def test_partially_seeded_table_still_renders(self) -> None:
        matches = find_probe_calls(FIXTURES / "probe_session.jsonl")
        rows = build_comparison_table(matches)
        self.assertIn("Active probe comparison", render_report(rows))

    def test_main_refuses_to_write_a_fully_seeded_report(self) -> None:
        from toolbench.probe import main

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "reports" / "x.md"
            with self.assertRaises(SeededReportError):
                main(["--out", str(out_path)])
            self.assertFalse(out_path.exists(), "refused report must not be written")


if __name__ == "__main__":
    unittest.main()
