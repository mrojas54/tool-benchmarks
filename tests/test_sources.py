import inspect
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.fakes import FakeRunner, completed
from toolbench.passive import classify_skip, filter_subagents
from toolbench.sources import (
    AGENTSVIEW_TIMEOUT_S,
    AgentCensus,
    AgentsViewExclusionWarning,
    AgentsViewLoader,
    AgentsViewTimeout,
    MissingSourceExport,
    NonTranscriptExport,
    RawFileLoader,
    SessionLoader,
    SessionRef,
    SkipReason,
    SkipRecord,
    _list_argv,
    _probe_agentsview,
    _run_agentsview,
    discover_agentsview,
    iter_agentsview_sessions,
    iter_session_files,
    iter_sessions,
    open_session_jsonl,
)

# `agentsview session export` returns rc=0 and a whole SQLite database for hermes
# cron sessions. First 16 bytes of that real payload (TB-10).
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Child-process source that emits a bare 0xa0 byte — the exact byte that aborted
# the live corpus scan (TB-10). Written as bytes so no encoding assumption applies.
_EMIT_NON_UTF8 = 'import sys; sys.stdout.buffer.write(b\'{"note": "caf\\xa0"}\\n\')'


def _page(*sessions: dict[str, str], cursor: str = "") -> str:
    return json.dumps({"sessions": list(sessions), "next_cursor": cursor, "total": len(sessions)})


def _total_page(total: int) -> str:
    """A `--limit 1` census response: we read `total`, never the rows."""
    return json.dumps({"sessions": [], "next_cursor": "", "total": total})


def _av(*pages: str, stderr: str = "") -> FakeRunner:
    """Script the two-pass agentsview discovery (TB-31).

    Call order is parent-probe pages first (no --include-children), then the full
    listing. `stderr` rides on every page so banner assertions do not depend on which
    page emitted it.
    """
    return FakeRunner([completed(stdout=p, stderr=stderr) for p in pages])


class IterAgentsviewSessionsTests(unittest.TestCase):
    """The listing is two passes: a parent probe, then the full corpus (TB-30/TB-31).

    `runner.calls[0]` is always the parent probe; the full listing starts at `calls[1]`.
    """

    def test_single_page(self) -> None:
        s1 = {"id": "s1", "project": "proj-a", "agent": "claude"}
        s2 = {"id": "s2", "project": "proj-a", "agent": "claude"}
        runner = _av(_page(s1, s2), _page(s1, s2))
        refs = list(iter_agentsview_sessions(runner=runner))
        self.assertEqual(len(refs), 2)
        self.assertEqual(
            refs[0],
            SessionRef(agent="claude", source="agentsview", project="proj-a", session_id="s1", path=None),
        )
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn("--cursor", runner.calls[1])

    def test_pagination_follows_cursor_until_empty(self) -> None:
        s1 = {"id": "s1", "project": "p", "agent": "claude"}
        s2 = {"id": "s2", "project": "p", "agent": "claude"}
        runner = _av(_page(s1, s2), _page(s1, cursor="CURSOR1"), _page(s2))
        refs = list(iter_agentsview_sessions(runner=runner))
        self.assertEqual([r.session_id for r in refs], ["s1", "s2"])
        self.assertEqual(len(runner.calls), 3)
        self.assertNotIn("--cursor", runner.calls[1])
        self.assertIn("--cursor", runner.calls[2])
        self.assertEqual(runner.calls[2][runner.calls[2].index("--cursor") + 1], "CURSOR1")

    def test_pagination_stops_when_cursor_key_absent(self) -> None:
        s1 = {"id": "s1", "project": "p", "agent": "claude"}
        page_without_cursor_key = json.dumps({"sessions": [s1], "total": 1})
        runner = _av(page_without_cursor_key, page_without_cursor_key)
        refs = list(iter_agentsview_sessions(runner=runner))
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(runner.calls), 2)

    def test_argv_includes_agent_project_since_limit(self) -> None:
        runner = _av(_page(), _page())
        list(
            iter_agentsview_sessions(
                agent="codex", project="tool-benchmarks", since="2026-07-01", limit=50, runner=runner
            )
        )
        # Both passes must carry identical filters, or the set difference between them
        # is taken across two different populations and every ref is mislabelled.
        for argv in runner.calls:
            self.assertEqual(argv[argv.index("--agent") + 1], "codex")
            self.assertEqual(argv[argv.index("--project") + 1], "tool-benchmarks")
            self.assertEqual(argv[argv.index("--date-from") + 1], "2026-07-01")
            self.assertEqual(argv[argv.index("--limit") + 1], "50")

    def test_agent_all_omits_agent_flag(self) -> None:
        runner = _av(_page(), _page())
        list(iter_agentsview_sessions(agent="all", runner=runner))
        for argv in runner.calls:
            self.assertNotIn("--agent", argv)

    def test_nonzero_exit_raises(self) -> None:
        runner = FakeRunner([completed(stdout="", stderr="boom", returncode=1)])
        with self.assertRaises(RuntimeError):
            list(iter_agentsview_sessions(runner=runner))

    # -- TB-30: the corpus the listing is allowed to see -----------------------------

    def test_full_listing_carries_all_three_include_flags(self) -> None:
        """Without these, agentsview drops one-shot/automated/child sessions by default
        -- 70% of the live archive, and not uniformly across agents (TB-30)."""
        runner = _av(_page(), _page())
        list(iter_agentsview_sessions(runner=runner))
        full_listing = runner.calls[1]
        self.assertIn("--include-children", full_listing)
        self.assertIn("--include-automated", full_listing)
        self.assertIn("--include-one-shot", full_listing)

    def test_parent_probe_omits_only_include_children(self) -> None:
        """The probe differs from the full listing in exactly one flag, so the
        difference between the two listings is precisely the child sessions."""
        runner = _av(_page(), _page())
        list(iter_agentsview_sessions(runner=runner))
        probe = runner.calls[0]
        self.assertNotIn("--include-children", probe)
        self.assertIn("--include-automated", probe)
        self.assertIn("--include-one-shot", probe)

    # -- TB-31: is_subagent, decided by agentsview rather than guessed ----------------

    def test_session_absent_from_parent_listing_is_stamped_subagent(self) -> None:
        parent = {"id": "s1", "project": "p", "agent": "claude"}
        child = {"id": "agent-abc123", "project": "p", "agent": "claude"}
        runner = _av(_page(parent), _page(parent, child))
        refs = {r.session_id: r for r in iter_agentsview_sessions(runner=runner)}
        self.assertFalse(refs["s1"].is_subagent)
        self.assertTrue(refs["agent-abc123"].is_subagent)

    def test_child_without_an_agent_id_token_is_still_stamped(self) -> None:
        """Codex subagents carry no `agent-` token and no parent pointer -- the live
        archive has 7. An id-shape heuristic misses them; the set difference does not."""
        parent = {"id": "codex:0001", "project": "p", "agent": "codex"}
        child = {"id": "codex:019e10c4-a227-74d2-b912-06f8a4fd5b13", "project": "p", "agent": "codex"}
        runner = _av(_page(parent), _page(parent, child))
        refs = {r.session_id: r for r in iter_agentsview_sessions(runner=runner)}
        self.assertTrue(refs[child["id"]].is_subagent)

    def test_resumed_session_pointing_at_another_session_is_not_a_subagent(self) -> None:
        """`source_session_id != id` looks like a parent pointer but also fires on
        resumed/compacted sessions -- 1631 false positives on the live archive. A ref
        the parent listing returned is a parent, whatever its fields suggest (TB-31)."""
        resumed = {
            "id": "s2",
            "project": "p",
            "agent": "claude",
            "source_session_id": "s1",  # an earlier session it was resumed from
        }
        runner = _av(_page(resumed), _page(resumed))
        (ref,) = list(iter_agentsview_sessions(runner=runner))
        self.assertFalse(ref.is_subagent)

    def test_unexpected_exclusion_banner_is_surfaced(self) -> None:
        """We now opt into every exclusion agentsview knows about, so a banner on the
        full listing means it dropped sessions we did not ask it to drop. The banner
        was previously parsed off stdout and discarded -- that is how TB-30 hid."""
        banner = "Excluded 7497 sessions by default: 7435 one-shot, 62 automated."
        s1 = {"id": "s1", "project": "p", "agent": "claude"}
        runner = _av(_page(s1), _page(s1), stderr=banner)
        with self.assertWarns(AgentsViewExclusionWarning) as caught:
            list(iter_agentsview_sessions(runner=runner))
        self.assertIn("7497", str(caught.warning))


class AgentCensusTests(unittest.TestCase):
    """Per-agent denominators, gathered under the run's own filters (TB-33)."""

    def test_totals_reconcile_to_zero_residual(self) -> None:
        # probe pass: two agents present as non-children.
        probe = _page(
            {"id": "s1", "agent": "claude", "project": "p"},
            {"id": "s2", "agent": "codex", "project": "p"},
        )
        runner = FakeRunner([
            completed(stdout=probe),            # probe pass
            completed(stdout=_total_page(80)),  # census: --agent claude
            completed(stdout=_total_page(20)),  # census: --agent codex
            completed(stdout=_total_page(100)), # census: archive total (agent=all)
            completed(stdout=probe),            # full listing
        ])
        census, refs = discover_agentsview(runner, agent="all", project=None, since=None, limit=500)
        list(refs)

        self.assertIsInstance(census, AgentCensus)
        self.assertEqual(census.totals, {"claude": 80, "codex": 20})
        self.assertEqual(census.archive_total, 100)
        self.assertEqual(census.residual, 0)
        self.assertIsNone(census.unavailable_reason)

    def test_residual_names_an_agent_the_probe_never_saw(self) -> None:
        # The probe listing excludes children, so an agent whose sessions are ALL
        # children is invisible to it. Reconciliation is the only thing that catches it.
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout=_total_page(80)),   # claude
            completed(stdout=_total_page(100)),  # archive
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(runner, agent="all", project=None, since=None, limit=500)
        list(refs)

        self.assertEqual(census.residual, 20)

    def test_census_inherits_project_and_since_filters(self) -> None:
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout=_total_page(5)),
            completed(stdout=_total_page(5)),
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(
            runner, agent="all", project="tool-benchmarks", since="2026-07-01", limit=500
        )
        list(refs)

        # A denominator gathered under different filters describes a different
        # population than the numerator. Every census call must carry both filters.
        census_calls = [c for c in runner.calls if "--limit" in c and c[c.index("--limit") + 1] == "1"]
        self.assertEqual(len(census_calls), 2)
        for argv in census_calls:
            self.assertIn("--project", argv)
            self.assertIn("tool-benchmarks", argv)
            self.assertIn("--date-from", argv)
            self.assertIn("2026-07-01", argv)

    # -- TB-33 Finding 1/2: the census `--include-*` flags must track the numerator ---

    def _population_scripted_runner(self) -> FakeRunner:
        """Two agents, so the census makes 3 `--limit 1` calls: claude, codex, archive."""
        probe = _page(
            {"id": "s1", "agent": "claude", "project": "p"},
            {"id": "s2", "agent": "codex", "project": "p"},
        )
        return FakeRunner([
            completed(stdout=probe),            # probe pass
            completed(stdout=_total_page(80)),  # census: claude
            completed(stdout=_total_page(20)),  # census: codex
            completed(stdout=_total_page(100)), # census: archive total
            completed(stdout=probe),            # full listing
        ])

    @staticmethod
    def _census_calls(runner: FakeRunner) -> list[list[str]]:
        """The `--limit 1` calls only -- excludes the probe pass and full listing,
        which both page at the run's `limit` instead."""
        return [c for c in runner.calls if "--limit" in c and c[c.index("--limit") + 1] == "1"]

    def test_default_census_carries_all_three_include_flags(self) -> None:
        """`include_subagents=True` (the default -- no `--exclude-subagents`) must send
        every census call the SAME three flags the full listing uses. Mutating
        `discover_agentsview` to hardcode `_PROBE_INCLUDES` for the census regardless of
        `include_subagents` -- i.e. re-importing the TB-30 bug into the denominator --
        must fail this test; see the report for the mutation proof."""
        runner = self._population_scripted_runner()
        census, refs = discover_agentsview(
            runner, agent="all", project=None, since=None, limit=500, include_subagents=True
        )
        list(refs)

        census_calls = self._census_calls(runner)
        self.assertEqual(len(census_calls), 3)
        for argv in census_calls:
            self.assertIn("--include-children", argv)
            self.assertIn("--include-automated", argv)
            self.assertIn("--include-one-shot", argv)
        # Sanity: the census actually took (not the unavailable/error branch).
        self.assertEqual(census.totals, {"claude": 80, "codex": 20})

    def test_exclude_subagents_census_omits_include_children_only(self) -> None:
        """`include_subagents=False` (`--exclude-subagents`) must send every census call
        `--include-automated`/`--include-one-shot` but NOT `--include-children` -- the
        exact set `filter_subagents` (passive.py) leaves in the numerator, since it keeps
        only refs whose ids came back on the `_PROBE_INCLUDES` listing. Mutating
        `discover_agentsview` to keep sending `_ALL_INCLUDES` for the census regardless
        of `include_subagents` must fail this test; see the report for the mutation
        proof."""
        runner = self._population_scripted_runner()
        census, refs = discover_agentsview(
            runner, agent="all", project=None, since=None, limit=500, include_subagents=False
        )
        list(refs)

        census_calls = self._census_calls(runner)
        self.assertEqual(len(census_calls), 3)
        for argv in census_calls:
            self.assertNotIn("--include-children", argv)
            self.assertIn("--include-automated", argv)
            self.assertIn("--include-one-shot", argv)
        self.assertEqual(census.totals, {"claude": 80, "codex": 20})

    def test_scoped_agent_run_reconciles_to_zero(self) -> None:
        # Under `--agent codex` the run's population IS codex. An UNSCOPED archive total
        # would compute a residual of every other agent's sessions and scream about
        # thousands of "unenumerated" sessions that were never in scope.
        probe = _page({"id": "s1", "agent": "codex", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout=_total_page(183)),  # census: --agent codex
            completed(stdout=_total_page(183)),  # archive total, ALSO scoped to codex
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(runner, agent="codex", project=None, since=None, limit=500)
        list(refs)

        self.assertEqual(census.residual, 0)
        self.assertEqual(census.archive_total, 183)

    def test_census_failure_is_disclosed_not_dropped(self) -> None:
        # A census we cannot take is rendered as "unknown" WITH a reason. Discovery is
        # unaffected: the refs are already ours.
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout="", stderr="daemon down", returncode=1),  # census blows up
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(runner, agent="all", project=None, since=None, limit=500)

        self.assertIsNotNone(census.unavailable_reason)
        assert census.unavailable_reason is not None
        self.assertIn("daemon down", census.unavailable_reason)
        self.assertEqual(census.totals, {})
        self.assertEqual([r.session_id for r in refs], ["s1"])

    def test_iter_agentsview_sessions_takes_no_census(self) -> None:
        # The back-compat wrapper stays census-free: callers that only want refs must
        # not pay for denominators they will not render.
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([completed(stdout=probe), completed(stdout=probe)])

        refs = list(iter_agentsview_sessions(runner=runner))

        self.assertEqual([r.session_id for r in refs], ["s1"])
        self.assertEqual(len(runner.calls), 2)  # probe + full listing, nothing else


class IterSessionFilesTests(unittest.TestCase):
    def test_missing_root_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            list(iter_session_files(root="/nonexistent/definitely-not-here"))

    def test_yields_jsonl_files_only(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "-Users-me-tool-benchmarks"
            proj.mkdir()
            (proj / "session1.jsonl").write_text("{}\n")
            (proj / "notes.txt").write_text("ignore me")
            paths = list(iter_session_files(root=tmp))
            self.assertEqual([p.name for p in paths], ["session1.jsonl"])

    def test_filters_by_project_equality_on_first_segment(self) -> None:
        """CQ 3.2: --project matches the owning dir exactly, not a substring."""
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "-Users-me-tool-benchmarks").mkdir()
            (Path(tmp) / "-Users-me-tool-benchmarks" / "s1.jsonl").write_text("{}\n")
            (Path(tmp) / "-Users-me-tool-benchmarks-extra").mkdir()
            (Path(tmp) / "-Users-me-tool-benchmarks-extra" / "s2.jsonl").write_text("{}\n")
            (Path(tmp) / "-Users-me-other-project").mkdir()
            (Path(tmp) / "-Users-me-other-project" / "s3.jsonl").write_text("{}\n")
            # Substring "tool-benchmarks" must NOT match either encoded dir.
            self.assertEqual(
                list(iter_session_files(root=tmp, project="tool-benchmarks")),
                [],
            )
            paths = list(
                iter_session_files(root=tmp, project="-Users-me-tool-benchmarks")
            )
            self.assertEqual([p.name for p in paths], ["s1.jsonl"])

    def test_project_filter_keeps_nested_subagent_sessions(self) -> None:
        # Real layout (TB-29): <project>/<session-uuid>/subagents/<file>.jsonl.
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "-Users-me-tool-benchmarks"
            (proj / "116ef75f" / "subagents").mkdir(parents=True)
            (proj / "s1.jsonl").write_text("{}\n")
            (proj / "116ef75f" / "subagents" / "sub1.jsonl").write_text("{}\n")
            paths = list(
                iter_session_files(root=tmp, project="-Users-me-tool-benchmarks")
            )
            self.assertEqual(sorted(p.name for p in paths), ["s1.jsonl", "sub1.jsonl"])

    def test_project_filter_excludes_other_projects_nested_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            other = Path(tmp) / "-Users-me-other-project"
            (other / "116ef75f" / "subagents").mkdir(parents=True)
            (other / "116ef75f" / "subagents" / "sub2.jsonl").write_text("{}\n")
            paths = list(
                iter_session_files(root=tmp, project="-Users-me-tool-benchmarks")
            )
            self.assertEqual(paths, [])

    def test_filters_by_since_mtime(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            old = proj / "old.jsonl"
            old.write_text("{}\n")
            new = proj / "new.jsonl"
            new.write_text("{}\n")
            boundary = datetime.now().astimezone()
            old_ts = boundary.timestamp() - 3600
            new_ts = boundary.timestamp() + 3600
            os.utime(old, (old_ts, old_ts))
            os.utime(new, (new_ts, new_ts))
            paths = list(iter_session_files(root=tmp, since=boundary.isoformat()))
            self.assertEqual([p.name for p in paths], ["new.jsonl"])


class OpenSessionJsonlTests(unittest.TestCase):
    def test_reads_from_filesystem_path(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.jsonl"
            p.write_text('{"a": 1}\n{"b": 2}\n')
            ref = SessionRef(agent="claude-code", source="raw", project="p", session_id="s", path=str(p))
            lines = list(open_session_jsonl(ref, runner=FakeRunner([])))
            self.assertEqual(lines, ['{"a": 1}\n', '{"b": 2}\n'])

    def test_shells_to_export_when_no_path(self) -> None:
        ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="abc123", path=None)
        runner = FakeRunner([completed(stdout='{"a": 1}\n{"b": 2}\n')])
        lines = list(open_session_jsonl(ref, runner=runner))
        self.assertEqual(lines, ['{"a": 1}\n', '{"b": 2}\n'])
        self.assertEqual(runner.calls[0], ["agentsview", "session", "export", "abc123"])

    def test_export_nonzero_exit_raises(self) -> None:
        ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="abc123", path=None)
        runner = FakeRunner([completed(stderr="not found", returncode=1)])
        with self.assertRaises(RuntimeError):
            list(open_session_jsonl(ref, runner=runner))


class IterSessionsIndexSourcePolicyTests(unittest.TestCase):
    def test_raw_mode_uses_filesystem_only(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "s1.jsonl").write_text("{}\n")
            refs, reason, _census = iter_sessions(index_source="raw", root=tmp, runner=FakeRunner([]))
            ref_list = list(refs)
            self.assertEqual(len(ref_list), 1)
            self.assertEqual(ref_list[0].source, "raw")
            self.assertIsNone(reason)

    def test_raw_refs_use_first_segment_project_and_is_subagent_flag(self) -> None:
        """CQ 3.2: subagent paths keep the owning project; is_subagent is set at discovery.

        TB-29 REGRESSION. The fixture mirrors the REAL on-disk layout --
        <project>/<session-uuid>/subagents/agent-*.jsonl -- which nests the subagent
        dir one level deeper than the flat <project>/subagents/ this suite used to
        build. Against that invented layout the old `rel.parts[1] == "subagents"`
        test passed, so the suite ratified the bug instead of catching it: on real
        scans parts[1] is the session UUID and is_subagent was NEVER True.
        """
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "-Users-me-tool-benchmarks"
            session = proj / "116ef75f-eb26-484d-84d7-fbdca43e246c"
            (session / "subagents").mkdir(parents=True)
            (proj / "parent.jsonl").write_text("{}\n")
            (session / "subagents" / "child.jsonl").write_text("{}\n")
            refs, _reason, _census = iter_sessions(index_source="raw", root=tmp, runner=FakeRunner([]))
            by_id = {r.session_id: r for r in refs}
            parent = by_id["parent"]
            child = by_id["child"]
            self.assertEqual(parent.project, "-Users-me-tool-benchmarks")
            self.assertFalse(parent.is_subagent)
            self.assertEqual(child.project, "-Users-me-tool-benchmarks")
            self.assertTrue(child.is_subagent)
            # Must not collapse the owning project to the "subagents" directory name,
            # nor to the intervening session UUID.
            self.assertNotEqual(child.project, "subagents")
            self.assertNotEqual(child.project, "116ef75f-eb26-484d-84d7-fbdca43e246c")

    def test_exclude_subagents_actually_drops_subagent_sessions(self) -> None:
        """TB-29: the flag was a silent no-op -- the report printed 'Subagents
        included: no' while the refs it counted still held every subagent. Asserting
        on the FILTERED refs, not just the flag, is what makes that unfakeable."""
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "-Users-me-tool-benchmarks"
            session = proj / "116ef75f-eb26-484d-84d7-fbdca43e246c"
            (session / "subagents").mkdir(parents=True)
            (proj / "parent.jsonl").write_text("{}\n")
            (session / "subagents" / "child.jsonl").write_text("{}\n")
            refs, _reason, _census = iter_sessions(index_source="raw", root=tmp, runner=FakeRunner([]))
            kept = filter_subagents(list(refs))
            self.assertEqual([r.session_id for r in kept], ["parent"])

    def test_agentsview_mode_strict_raises_on_missing_binary(self) -> None:
        # `discover_agentsview` runs its parent-probe pass EAGERLY (TB-33: the census
        # cannot be gathered lazily, since callers may break out of the ref loop
        # early), so a missing binary now surfaces from the `iter_sessions` call
        # itself rather than from consuming the returned iterator.
        runner = FakeRunner([FileNotFoundError("no agentsview")])
        with self.assertRaises(FileNotFoundError):
            iter_sessions(index_source="agentsview", runner=runner)

    def test_auto_uses_agentsview_when_available(self) -> None:
        payload = {
            "sessions": [{"id": "s1", "project": "p", "agent": "claude"}],
            "next_cursor": "",
            "total": 1,
        }
        # Availability probe, parent probe, per-agent census (--limit 1, one agent seen)
        # + archive total, then the full listing (TB-31, TB-33): 5 responses, not 3.
        runner = FakeRunner([completed(stdout=json.dumps(payload))] * 5)
        refs, reason, _census = iter_sessions(index_source="auto", runner=runner)
        ref_list = list(refs)
        self.assertIsNone(reason)
        self.assertEqual(len(ref_list), 1)
        self.assertEqual(ref_list[0].source, "agentsview")
        self.assertFalse(ref_list[0].is_subagent)

    def test_auto_falls_back_to_raw_and_records_reason_on_missing_binary(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "s1.jsonl").write_text("{}\n")
            runner = FakeRunner([FileNotFoundError("no agentsview")])
            refs, reason, _census = iter_sessions(index_source="auto", root=tmp, runner=runner)
            ref_list = list(refs)
            self.assertIsNotNone(reason)
            assert reason is not None
            self.assertIn("agentsview", reason)
            self.assertEqual(len(ref_list), 1)
            self.assertEqual(ref_list[0].source, "raw")

    def test_auto_falls_back_to_raw_and_records_reason_on_nonzero_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            runner = FakeRunner([completed(stderr="daemon down", returncode=1)])
            refs, reason, _census = iter_sessions(index_source="auto", root=tmp, runner=runner)
            list(refs)
            self.assertIsNotNone(reason)
            assert reason is not None
            self.assertIn("daemon down", reason)

    def test_unknown_index_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            iter_sessions(index_source="bogus", runner=FakeRunner([]))  # type: ignore[arg-type]


class RawCensusTests(unittest.TestCase):
    """`--limit` truncates the raw path too, and MORE arbitrarily (TB-33)."""

    def test_raw_census_counts_every_discoverable_file(self) -> None:
        with TemporaryDirectory() as tmp:
            for name in ("a", "b", "c"):
                proj = Path(tmp) / "proj"
                proj.mkdir(exist_ok=True)
                (proj / f"{name}.jsonl").write_text("{}\n")

            _refs, _reason, census = iter_sessions(
                index_source="raw", root=tmp, runner=FakeRunner([])
            )

            # iter_session_files sorts by PATH, so --limit takes an alphabetical slice of
            # the project tree -- not even a recency window. One agent, so no cross-agent
            # skew; but "you scanned 1 of 3" still has to be sayable.
            self.assertEqual(census.totals, {"claude-code": 3})
            self.assertEqual(census.archive_total, 3)
            self.assertEqual(census.residual, 0)
            self.assertIsNone(census.unavailable_reason)

    def test_raw_census_on_a_missing_root_is_unavailable_not_a_crash(self) -> None:
        _refs, _reason, census = iter_sessions(
            index_source="raw", root="/nonexistent/root", runner=FakeRunner([])
        )

        self.assertIsNotNone(census.unavailable_reason)
        self.assertEqual(census.totals, {})

    def test_raw_census_mixed_tree_pins_the_include_subagents_branch(self) -> None:
        """Direct pin for `_raw_census`'s `include_subagents` branch (TB-33 Finding 1).

        Real subagent nesting is <project>/<session-uuid>/subagents/*.jsonl -- NOT
        <project>/subagents/*.jsonl (TB-29 was exactly this path-shape mistake, and
        the existing raw+exclude CLI test builds a tree where EVERY session is a
        subagent, so it exits at "no sessions matched" and never renders a
        denominator). Two parent sessions plus three children nested under one
        parent's subagents/ dir: the denominator must be 5 with subagents included,
        2 without. Mutating `_raw_census`'s `if include_subagents:` to `if True:`
        must fail this test.
        """
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "sess-parent-1.jsonl").write_text("{}\n")
            (proj / "sess-parent-2.jsonl").write_text("{}\n")
            sub = proj / "sess-parent-1" / "subagents"
            sub.mkdir(parents=True)
            for name in ("agent-a", "agent-b", "agent-c"):
                (sub / f"{name}.jsonl").write_text("{}\n")

            _refs, _reason, included = iter_sessions(
                index_source="raw", root=tmp, runner=FakeRunner([]), include_subagents=True
            )
            self.assertEqual(included.totals, {"claude-code": 5})
            self.assertEqual(included.archive_total, 5)

            _refs2, _reason2, excluded = iter_sessions(
                index_source="raw", root=tmp, runner=FakeRunner([]), include_subagents=False
            )
            self.assertEqual(excluded.totals, {"claude-code": 2})
            self.assertEqual(excluded.archive_total, 2)


class NonUtf8DecodeTests(unittest.TestCase):
    """A stray non-UTF-8 byte must degrade to U+FFFD, never abort the scan (TB-10)."""

    def test_run_agentsview_decodes_child_stdout_leniently(self) -> None:
        # Drives a real subprocess: strict `text=True` raises inside communicate(),
        # so a fixture-shaped CompletedProcess could never catch this regression.
        result = _run_agentsview([sys.executable, "-c", _EMIT_NON_UTF8])
        self.assertEqual(result.returncode, 0)
        self.assertIn("�", result.stdout)
        self.assertIn('"note"', result.stdout)

    def test_open_session_jsonl_reads_non_utf8_file_leniently(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess-bad.jsonl"
            path.write_bytes(b'{"note": "caf\xa0"}\n{"note": "ok"}\n')
            ref = SessionRef(
                agent="claude-code", source="raw", project="p", session_id="sess-bad", path=str(path)
            )
            lines = list(open_session_jsonl(ref))
        self.assertEqual(len(lines), 2)
        self.assertIn("�", lines[0])


class NonTranscriptExportTests(unittest.TestCase):
    """A payload that is not a transcript at all must be rejected, not absorbed (TB-10).

    Lenient decode alone would turn a 37MB SQLite file into ~351k 'malformed
    lines', drowning the provenance signal that reads 0 on a clean corpus.
    """

    def test_export_of_binary_payload_is_rejected(self) -> None:
        payload = _SQLITE_MAGIC.decode("utf-8", errors="replace") + "\x10\x00\x02tablemessages"
        runner = FakeRunner([completed(stdout=payload)])
        ref = SessionRef(agent="hermes", source="agentsview", project="p", session_id="cron_1", path=None)
        with self.assertRaises(NonTranscriptExport):
            list(open_session_jsonl(ref, runner=runner))

    def test_raw_binary_session_file_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sess.jsonl"
            path.write_bytes(_SQLITE_MAGIC + b"\x10\x00\x02tablemessages")
            ref = SessionRef(
                agent="hermes", source="raw", project="p", session_id="cron_1", path=str(path)
            )
            with self.assertRaises(NonTranscriptExport):
                list(open_session_jsonl(ref))

    def test_non_transcript_export_is_a_runtimeerror(self) -> None:
        # Subclassing RuntimeError is load-bearing: passive.main()'s per-session
        # guard already catches it, so binary sessions demote to skipped_roots.
        self.assertTrue(issubclass(NonTranscriptExport, RuntimeError))

    def test_stray_byte_without_nul_is_not_treated_as_binary(self) -> None:
        # A good session with one bad byte must still parse; only NUL means binary.
        runner = FakeRunner([completed(stdout='{"note": "caf�"}\n')])
        ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="s1", path=None)
        self.assertEqual(len(list(open_session_jsonl(ref, runner=runner))), 1)


# --- TB-13: SessionLoader splits acquisition out of open_session_jsonl ---------


def _ok(stdout: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_raw_file_loader_yields_lines(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="s", path=str(p))
    assert list(RawFileLoader().lines(ref)) == ['{"a":1}\n', '{"b":2}\n']


def test_raw_file_loader_rejects_binary_before_any_parse(tmp_path: Path) -> None:
    p = tmp_path / "s.db"
    p.write_bytes(b"SQLite format 3\x00rest")
    ref = SessionRef(agent="hermes", source="raw", project="p", session_id="s", path=str(p))
    with pytest.raises(NonTranscriptExport):
        list(RawFileLoader().lines(ref))


def test_raw_file_loader_missing_file_raises_missing_source(tmp_path: Path) -> None:
    # A frozen ref whose raw transcript has since been deleted is a vanished source,
    # not a generic export failure (TB-22): raise the same typed MissingSourceExport
    # the AgentsView path raises so `classify_skip` buckets both as missing_source.
    gone = tmp_path / "gone.jsonl"
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="gone", path=str(gone))
    with pytest.raises(MissingSourceExport):
        list(RawFileLoader().lines(ref))


def test_raw_file_loader_decodes_leniently(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_bytes(b'{"a":"\xa0"}\n')  # stray non-UTF-8 byte
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="s", path=str(p))
    assert list(RawFileLoader().lines(ref)) == ['{"a":"�"}\n']


def test_agentsview_loader_yields_lines() -> None:
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="c:1", path=None)
    loader = AgentsViewLoader(runner=lambda argv: _ok('{"a":1}\n{"b":2}\n'))
    assert list(loader.lines(ref)) == ['{"a":1}\n', '{"b":2}\n']


def test_agentsview_loader_rejects_binary_payload() -> None:
    ref = SessionRef(agent="hermes", source="agentsview", project="p", session_id="h:1", path=None)
    loader = AgentsViewLoader(runner=lambda argv: _ok("SQLite format 3\x00junk"))
    with pytest.raises(NonTranscriptExport):
        list(loader.lines(ref))


def test_agentsview_loader_raises_on_nonzero_returncode() -> None:
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="c:1", path=None)
    bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    loader = AgentsViewLoader(runner=lambda argv: bad)
    with pytest.raises(RuntimeError, match="boom"):
        list(loader.lines(ref))


def test_loaders_are_session_loaders() -> None:
    assert issubclass(RawFileLoader, SessionLoader)
    assert issubclass(AgentsViewLoader, SessionLoader)


# --- TB-23: a dead index entry raises a distinct, typed exception --------------


def test_agentsview_loader_raises_missing_source_when_transcript_is_gone() -> None:
    # AgentsView lists a session whose .jsonl no longer exists on disk. `export`
    # exits non-zero with a `source file not found` stderr. That is a categorically
    # different diagnosis from a generic export failure and gets its own type, so
    # the reason survives to the report without a regex scan of the prose (TB-23).
    ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="claude-ai:x", path=None)
    stderr = "fatal: source file not found: /Users/x/.claude/projects/p/x.jsonl"
    bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)
    loader = AgentsViewLoader(runner=lambda argv: bad)
    with pytest.raises(MissingSourceExport):
        list(loader.lines(ref))


def test_agentsview_loader_other_failure_is_not_missing_source() -> None:
    # A non-zero export for any OTHER reason stays a plain RuntimeError (EXPORT_FAILED),
    # never mis-typed as a dead index entry.
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="c:1", path=None)
    bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="database is locked")
    loader = AgentsViewLoader(runner=lambda argv: bad)
    with pytest.raises(RuntimeError) as exc_info:
        list(loader.lines(ref))
    assert not isinstance(exc_info.value, MissingSourceExport)


def test_missing_source_export_is_a_runtimeerror_but_not_a_non_transcript_export() -> None:
    # RuntimeError so passive.main's per-session guard still demotes it to a skip.
    # NOT a NonTranscriptExport: "the file is gone" and "the file is binary" are
    # different reasons, so the type hierarchy keeps classify_skip unambiguous.
    assert issubclass(MissingSourceExport, RuntimeError)
    assert not issubclass(MissingSourceExport, NonTranscriptExport)


def test_skip_record_carries_a_typed_reason() -> None:
    rec = SkipRecord(
        session_id="c:1", agent="codex", reason=SkipReason.UNKNOWN_SCHEMA, detail="no parser"
    )
    assert rec.reason is SkipReason.UNKNOWN_SCHEMA
    assert rec.session_id == "c:1"
    assert rec.detail == "no parser"


# -- TB-32: a hung AgentsView daemon must never block a scan --------------------------
#
# S10 names two failure modes (binary missing, nonzero exit). The third -- the daemon
# accepts the connection and never answers -- produces NEITHER signal, and an unbounded
# subprocess.run() blocks forever. Every other test in this suite injects a FakeRunner
# that returns instantly, so the hang lives in the real `_run_agentsview` default that
# no fixture can reach; the first test here drives a REAL child process that really
# hangs, which is the only way to prove the bound is actually on the production path.


class AgentsViewTimeoutTests(unittest.TestCase):
    def test_run_agentsview_raises_typed_timeout_on_a_real_hang(self) -> None:
        # A real child that sleeps far past the bound. `timeout` is passed explicitly so
        # the test costs 0.2s instead of AGENTSVIEW_TIMEOUT_S; the DEFAULT is asserted
        # separately below, so the production path stays covered without a slow test.
        with self.assertRaises(AgentsViewTimeout) as caught:
            _run_agentsview([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
        self.assertIn("timed out", str(caught.exception))

    def test_default_timeout_is_bounded(self) -> None:
        """The regression that matters: shipping `timeout=None` re-opens TB-32."""
        default = inspect.signature(_run_agentsview).parameters["timeout"].default
        self.assertIsNotNone(default)
        self.assertEqual(default, AGENTSVIEW_TIMEOUT_S)
        self.assertGreater(AGENTSVIEW_TIMEOUT_S, 0)

    def test_agentsview_timeout_is_a_runtimeerror(self) -> None:
        """Load-bearing, not cosmetic. subprocess.TimeoutExpired subclasses
        SubprocessError, so it escapes BOTH of passive.main's guards -- the
        (FileNotFoundError, RuntimeError) around ref collection and the
        (OSError, RuntimeError, UnicodeDecodeError) around each session. Re-typing the
        timeout as a RuntimeError is what routes it into the handling that exists."""
        self.assertTrue(issubclass(AgentsViewTimeout, RuntimeError))
        self.assertFalse(issubclass(AgentsViewTimeout, subprocess.SubprocessError))

    def test_probe_argv_is_built_by_the_sole_builder(self) -> None:
        """TB-36: `_probe_agentsview` routes through `_list_argv` like every other
        `session list` call site, even though it is the filter-free exception to the
        invariant `_list_argv` exists to enforce -- it discards the payload and never
        feeds a census denominator or a discovery numerator."""
        runner = FakeRunner([completed(stdout=_total_page(0))])
        reason = _probe_agentsview(runner)
        self.assertIsNone(reason)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            runner.calls[0],
            _list_argv(agent="all", project=None, since=None, limit=1, includes=()),
        )

    def test_auto_falls_back_to_raw_when_the_probe_times_out(self) -> None:
        """S10's intent: an unhealthy AgentsView degrades the scan, never blocks it."""
        runner = FakeRunner([AgentsViewTimeout("agentsview timed out after 60.0s")])
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "s1.jsonl").write_text("{}\n")
            refs, reason, _census = iter_sessions(index_source="auto", root=tmp, runner=runner)
            ref_list = list(refs)
        self.assertIsNotNone(reason)
        assert reason is not None  # narrow for mypy
        self.assertIn("timed out", reason)
        self.assertEqual([r.source for r in ref_list], ["raw"])

    def test_explicit_agentsview_does_not_swallow_a_timeout(self) -> None:
        """`--index-source agentsview` is an explicit demand: falling back to raw here
        would answer a question the operator did not ask. It must surface instead --
        as a RuntimeError, which passive.main reports as a fatal source error."""
        runner = FakeRunner([AgentsViewTimeout("agentsview timed out after 60.0s")])
        with self.assertRaises(AgentsViewTimeout):
            refs, _reason, _census = iter_sessions(index_source="agentsview", runner=runner)
            list(refs)

    def test_mid_discovery_timeout_is_fatal_like_any_other_source_error(self) -> None:
        """Scope boundary, asserted so it stays a decision rather than an accident.

        `auto`'s fallback covers the PROBE, not the pagination that follows it. A daemon
        that answers the probe and then dies mid-listing is fatal (passive.main reports
        "fatal source error" and exits 1) -- and that is PRE-EXISTING behaviour for a
        nonzero exit, on main, untouched by TB-32. This test pins the timeout to the SAME
        behaviour, because the alternative is incoherent: hangs falling back to raw while
        an equally-broken daemon that exits 1 stays fatal.

        Widening `auto` to re-discover from raw after a partial listing is a real S10 gap,
        but it belongs to all three failure modes at once, not to the timeout alone -- so
        it is TB-38, not this ticket.
        """
        ok = completed(stdout=_total_page(0))
        with self.assertRaises(AgentsViewTimeout):
            refs, _reason, _census = iter_sessions(
                index_source="auto",
                root="/tmp",
                runner=FakeRunner([ok, AgentsViewTimeout("agentsview timed out after 60.0s")]),
            )
            list(refs)
        # The pre-existing sibling, for contrast: same shape, same fatality.
        with self.assertRaises(RuntimeError):
            refs, _reason, _census = iter_sessions(
                index_source="auto",
                root="/tmp",
                runner=FakeRunner([ok, completed(stderr="boom", returncode=1)]),
            )
            list(refs)

    def test_export_timeout_is_classified_as_its_own_skip_reason(self) -> None:
        """A daemon healthy at probe time can hang later, on export #4000 of 8591. That
        must cost one session, not the whole scan -- and TB-23's rule is to type the
        absence rather than fold it into the generic EXPORT_FAILED bucket."""
        self.assertIs(
            classify_skip(AgentsViewTimeout("agentsview timed out after 60.0s")),
            SkipReason.EXPORT_TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
