import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from toolbench.sources import (
    NonTranscriptExport,
    SessionRef,
    _run_agentsview,
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


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Scripted subprocess-runner seam (S24): argv -> CompletedProcess, in call order."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if not self._responses:
            raise AssertionError(f"FakeRunner exhausted, unexpected call: {argv}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class IterAgentsviewSessionsTests(unittest.TestCase):
    def test_single_page(self) -> None:
        payload = {
            "sessions": [
                {"id": "s1", "project": "proj-a", "agent": "claude"},
                {"id": "s2", "project": "proj-a", "agent": "claude"},
            ],
            "next_cursor": "",
            "total": 2,
        }
        runner = FakeRunner([_completed(stdout=json.dumps(payload))])
        refs = list(iter_agentsview_sessions(runner=runner))
        self.assertEqual(len(refs), 2)
        self.assertEqual(
            refs[0],
            SessionRef(agent="claude", source="agentsview", project="proj-a", session_id="s1", path=None),
        )
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("--cursor", runner.calls[0])

    def test_pagination_follows_cursor_until_empty(self) -> None:
        page1 = {
            "sessions": [{"id": "s1", "project": "p", "agent": "claude"}],
            "next_cursor": "CURSOR1",
            "total": 2,
        }
        page2 = {
            "sessions": [{"id": "s2", "project": "p", "agent": "claude"}],
            "next_cursor": "",
            "total": 2,
        }
        runner = FakeRunner([_completed(stdout=json.dumps(page1)), _completed(stdout=json.dumps(page2))])
        refs = list(iter_agentsview_sessions(runner=runner))
        self.assertEqual([r.session_id for r in refs], ["s1", "s2"])
        self.assertEqual(len(runner.calls), 2)
        self.assertNotIn("--cursor", runner.calls[0])
        self.assertIn("--cursor", runner.calls[1])
        self.assertEqual(runner.calls[1][runner.calls[1].index("--cursor") + 1], "CURSOR1")

    def test_pagination_stops_when_cursor_key_absent(self) -> None:
        page1 = {"sessions": [{"id": "s1", "project": "p", "agent": "claude"}], "total": 1}
        runner = FakeRunner([_completed(stdout=json.dumps(page1))])
        refs = list(iter_agentsview_sessions(runner=runner))
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(runner.calls), 1)

    def test_argv_includes_agent_project_since_limit(self) -> None:
        payload = {"sessions": [], "next_cursor": "", "total": 0}
        runner = FakeRunner([_completed(stdout=json.dumps(payload))])
        list(
            iter_agentsview_sessions(
                agent="codex", project="tool-benchmarks", since="2026-07-01", limit=50, runner=runner
            )
        )
        argv = runner.calls[0]
        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "codex")
        self.assertIn("--project", argv)
        self.assertEqual(argv[argv.index("--project") + 1], "tool-benchmarks")
        self.assertIn("--date-from", argv)
        self.assertEqual(argv[argv.index("--date-from") + 1], "2026-07-01")
        self.assertIn("--limit", argv)
        self.assertEqual(argv[argv.index("--limit") + 1], "50")

    def test_agent_all_omits_agent_flag(self) -> None:
        payload = {"sessions": [], "next_cursor": "", "total": 0}
        runner = FakeRunner([_completed(stdout=json.dumps(payload))])
        list(iter_agentsview_sessions(agent="all", runner=runner))
        self.assertNotIn("--agent", runner.calls[0])

    def test_nonzero_exit_raises(self) -> None:
        runner = FakeRunner([_completed(stdout="", stderr="boom", returncode=1)])
        with self.assertRaises(RuntimeError):
            list(iter_agentsview_sessions(runner=runner))


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

    def test_filters_by_project_substring(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "-Users-me-tool-benchmarks").mkdir()
            (Path(tmp) / "-Users-me-tool-benchmarks" / "s1.jsonl").write_text("{}\n")
            (Path(tmp) / "-Users-me-other-project").mkdir()
            (Path(tmp) / "-Users-me-other-project" / "s2.jsonl").write_text("{}\n")
            paths = list(iter_session_files(root=tmp, project="tool-benchmarks"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].name, "s1.jsonl")

    def test_project_filter_keeps_nested_subagent_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "-Users-me-tool-benchmarks"
            (proj / "subagents").mkdir(parents=True)
            (proj / "s1.jsonl").write_text("{}\n")
            (proj / "subagents" / "sub1.jsonl").write_text("{}\n")
            paths = list(iter_session_files(root=tmp, project="tool-benchmarks"))
            self.assertEqual(sorted(p.name for p in paths), ["s1.jsonl", "sub1.jsonl"])

    def test_project_filter_excludes_other_projects_nested_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            other = Path(tmp) / "-Users-me-other-project"
            (other / "subagents").mkdir(parents=True)
            (other / "subagents" / "sub2.jsonl").write_text("{}\n")
            paths = list(iter_session_files(root=tmp, project="tool-benchmarks"))
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
        runner = FakeRunner([_completed(stdout='{"a": 1}\n{"b": 2}\n')])
        lines = list(open_session_jsonl(ref, runner=runner))
        self.assertEqual(lines, ['{"a": 1}\n', '{"b": 2}\n'])
        self.assertEqual(runner.calls[0], ["agentsview", "session", "export", "abc123"])

    def test_export_nonzero_exit_raises(self) -> None:
        ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="abc123", path=None)
        runner = FakeRunner([_completed(stderr="not found", returncode=1)])
        with self.assertRaises(RuntimeError):
            list(open_session_jsonl(ref, runner=runner))


class IterSessionsIndexSourcePolicyTests(unittest.TestCase):
    def test_raw_mode_uses_filesystem_only(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "s1.jsonl").write_text("{}\n")
            refs, reason = iter_sessions(index_source="raw", root=tmp, runner=FakeRunner([]))
            ref_list = list(refs)
            self.assertEqual(len(ref_list), 1)
            self.assertEqual(ref_list[0].source, "raw")
            self.assertIsNone(reason)

    def test_agentsview_mode_strict_raises_on_missing_binary(self) -> None:
        runner = FakeRunner([FileNotFoundError("no agentsview")])
        refs, _reason = iter_sessions(index_source="agentsview", runner=runner)
        with self.assertRaises(FileNotFoundError):
            list(refs)

    def test_auto_uses_agentsview_when_available(self) -> None:
        payload = {
            "sessions": [{"id": "s1", "project": "p", "agent": "claude"}],
            "next_cursor": "",
            "total": 1,
        }
        runner = FakeRunner([_completed(stdout=json.dumps(payload)), _completed(stdout=json.dumps(payload))])
        refs, reason = iter_sessions(index_source="auto", runner=runner)
        ref_list = list(refs)
        self.assertIsNone(reason)
        self.assertEqual(len(ref_list), 1)
        self.assertEqual(ref_list[0].source, "agentsview")

    def test_auto_falls_back_to_raw_and_records_reason_on_missing_binary(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "s1.jsonl").write_text("{}\n")
            runner = FakeRunner([FileNotFoundError("no agentsview")])
            refs, reason = iter_sessions(index_source="auto", root=tmp, runner=runner)
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
            runner = FakeRunner([_completed(stderr="daemon down", returncode=1)])
            refs, reason = iter_sessions(index_source="auto", root=tmp, runner=runner)
            list(refs)
            self.assertIsNotNone(reason)
            assert reason is not None
            self.assertIn("daemon down", reason)

    def test_unknown_index_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            iter_sessions(index_source="bogus", runner=FakeRunner([]))  # type: ignore[arg-type]


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
        runner = FakeRunner([_completed(stdout=payload)])
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
        runner = FakeRunner([_completed(stdout='{"note": "caf�"}\n')])
        ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="s1", path=None)
        self.assertEqual(len(list(open_session_jsonl(ref, runner=runner))), 1)


if __name__ == "__main__":
    unittest.main()
