import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from toolbench.hermes import (
    _connect,
    hermes_home,
    iter_profile_dbs,
    parse_hermes_session,
    resolve_session,
)
from toolbench.sources import NonTranscriptExport
from toolbench.transcript import ParseResult

# The seven columns the adapter reads. Present in schema_version 16 and 19 alike;
# `test_live_archive_schema_envelope` asserts that against the real DBs.
_SESSION_COLS = ("id", "source", "model", "started_at", "tool_call_count")
_MESSAGE_COLS = ("session_id", "role", "content", "tool_call_id", "tool_calls", "timestamp")

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    model TEXT,
    started_at REAL NOT NULL,
    tool_call_count INTEGER DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL
);
"""


def _build_db(path: Path, session_id: str = "s1", *, model: str = "anthropic/claude-opus-4.8") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, tool_call_count) VALUES (?,?,?,?,?)",
        (session_id, "cron", model, 1_760_000_000.0, 0),
    )
    conn.commit()
    conn.close()


def _add_call(
    path: Path,
    session_id: str,
    call_id: str,
    name: str,
    arguments: str,
    result: str | None,
    *,
    ts: float = 1_760_000_001.0,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)",
        (
            session_id,
            "assistant",
            json.dumps(
                [{"id": call_id, "call_id": call_id, "type": "function",
                  "function": {"name": name, "arguments": arguments}}]
            ),
            ts,
        ),
    )
    if result is not None:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, "tool", result, call_id, name, ts + 1),
        )
    conn.commit()
    conn.close()


class HermesHome(unittest.TestCase):
    def test_env_override(self) -> None:
        with TemporaryDirectory() as tmp:
            os.environ["HERMES_HOME"] = tmp
            try:
                self.assertEqual(hermes_home(), Path(tmp))
            finally:
                del os.environ["HERMES_HOME"]

    def test_default_is_dot_hermes(self) -> None:
        os.environ.pop("HERMES_HOME", None)
        self.assertEqual(hermes_home(), Path("~/.hermes").expanduser())

    def test_iter_profile_dbs_orders_default_first(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "profiles" / "zeta").mkdir(parents=True)
            (home / "profiles" / "alpha").mkdir(parents=True)
            for p in (home / "state.db", home / "profiles" / "zeta" / "state.db",
                      home / "profiles" / "alpha" / "state.db"):
                _build_db(p)
            dbs = iter_profile_dbs(home)
            self.assertEqual(dbs[0], home / "state.db")
            self.assertEqual(
                dbs[1:], [home / "profiles" / "alpha" / "state.db", home / "profiles" / "zeta" / "state.db"]
            )

    def test_missing_home_raises_non_transcript(self) -> None:
        with self.assertRaises(NonTranscriptExport):
            iter_profile_dbs(Path("/nonexistent/hermes/home"))


class ResolveSession(unittest.TestCase):
    def test_finds_session_in_default_db(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            self.assertEqual(resolve_session("s1", home), home / "state.db")

    def test_finds_session_only_in_a_profile_db(self) -> None:
        # The two aphrodite-mood sessions the export can never reach (TB-11).
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            (home / "profiles" / "aphrodite-mood").mkdir(parents=True)
            _build_db(home / "profiles" / "aphrodite-mood" / "state.db", "s2")
            self.assertEqual(
                resolve_session("s2", home), home / "profiles" / "aphrodite-mood" / "state.db"
            )

    def test_strips_hermes_prefix(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "cron_abc_123")
            self.assertEqual(resolve_session("hermes:cron_abc_123", home), home / "state.db")

    def test_unresolvable_session_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            self.assertIsNone(resolve_session("nope", home))


class ParseHermesSession(unittest.TestCase):
    def _parse(self, home: Path, session_id: str = "s1") -> ParseResult:
        return parse_hermes_session(
            session_id, agent="hermes", source="agentsview", project="hermes-cron", home=home
        )

    def test_joins_call_to_result(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "call_1", "terminal", '{"cmd": "ls"}',
                      json.dumps({"output": "a\nb", "error": None}))
            result = self._parse(home)
            self.assertEqual(result.malformed, 0)
            self.assertEqual(len(result.calls), 1)
            call = result.calls[0]
            self.assertEqual(call.name, "terminal")
            self.assertEqual(call.agent, "hermes")
            self.assertEqual(call.project, "hermes-cron")
            self.assertEqual(call.session_id, "s1")
            self.assertEqual(call.input_chars, len('{"cmd": "ls"}'))
            self.assertFalse(call.no_result)
            self.assertIsNone(call.error)
            self.assertIsNone(call.usage)
            self.assertEqual(call.model, "anthropic/claude-opus-4.8")

    def test_output_chars_uses_result_len(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            payload = json.dumps({"output": "hello", "error": None})
            _add_call(home / "state.db", "s1", "c1", "terminal", "{}", payload)
            self.assertEqual(self._parse(home).calls[0].output_chars, len(payload))

    def test_timestamp_is_iso8601(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "c1", "terminal", "{}", "ok", ts=1_760_000_000.0)
            ts = self._parse(home).calls[0].ts
            self.assertTrue(ts.startswith("20"), ts)
            self.assertIn("T", ts)

    def test_call_without_result_is_kept_as_no_result(self) -> None:
        # S6 semantics: a call with no matching result survives with output_chars=0.
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "c1", "terminal", "{}", None)
            call = self._parse(home).calls[0]
            self.assertTrue(call.no_result)
            self.assertEqual(call.output_chars, 0)
            self.assertIsNone(call.error)

    def test_null_error_value_is_success(self) -> None:
        # The `error` key is present on ~every hermes result. Only its VALUE is a signal.
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "c1", "process", "{}",
                      json.dumps({"output": "fine", "error": None}))
            self.assertIsNone(self._parse(home).calls[0].error)

    def test_non_null_error_value_is_tool_error(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "c1", "process", "{}",
                      json.dumps({"error": "No process with ID lang"}))
            self.assertEqual(self._parse(home).calls[0].error, "tool_error")

    def test_non_json_result_carries_no_error_signal(self) -> None:
        # 409 live results are plain strings. Absence of evidence, not evidence of success.
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "c1", "read_file", "{}", "plain text output")
            call = self._parse(home).calls[0]
            self.assertIsNone(call.error)
            self.assertEqual(call.output_chars, len("plain text output"))

    def test_malformed_tool_calls_json_is_counted_not_fatal(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            conn = sqlite3.connect(home / "state.db")
            conn.execute(
                "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)",
                ("s1", "assistant", "{not json", 1.0),
            )
            conn.commit()
            conn.close()
            _add_call(home / "state.db", "s1", "c1", "terminal", "{}", "ok")
            result = self._parse(home)
            self.assertEqual(len(result.calls), 1)
            self.assertEqual(result.malformed, 1)

    def test_resolves_across_profile_dbs(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            (home / "profiles" / "aphrodite-mood").mkdir(parents=True)
            db = home / "profiles" / "aphrodite-mood" / "state.db"
            _build_db(db, "s2")
            _add_call(db, "s2", "c1", "clarify", "{}", "ok")
            calls = self._parse(home, "s2").calls
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].name, "clarify")

    def test_unresolvable_session_raises_non_transcript(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            with self.assertRaises(NonTranscriptExport):
                self._parse(home, "missing")

    def test_never_writes_to_the_database(self) -> None:
        # A live app owns this file. Read-only is a hard requirement, not a nicety.
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            _build_db(db, "s1")
            _add_call(db, "s1", "c1", "terminal", "{}", "ok")
            before = db.stat().st_mtime_ns
            self._parse(home)
            self.assertEqual(db.stat().st_mtime_ns, before)

    def test_calls_are_ordered_by_timestamp(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "s1")
            _add_call(home / "state.db", "s1", "c2", "second", "{}", "ok", ts=200.0)
            _add_call(home / "state.db", "s1", "c1", "first", "{}", "ok", ts=100.0)
            names = [c.name for c in self._parse(home).calls]
            self.assertEqual(names, ["first", "second"])


class ParseRefDispatch(unittest.TestCase):
    """`_parse_ref` must route hermes to the adapter and leave every other agent alone."""

    def test_hermes_ref_reads_sqlite_and_never_shells_out(self) -> None:
        import subprocess

        from toolbench.passive import _parse_ref
        from toolbench.sources import SessionRef

        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _build_db(home / "state.db", "cron_abc")
            _add_call(home / "state.db", "cron_abc", "c1", "terminal", "{}", "ok")
            os.environ["HERMES_HOME"] = tmp
            try:
                ref = SessionRef(
                    agent="hermes",
                    source="agentsview",
                    project="hermes-cron",
                    session_id="hermes:cron_abc",
                    path=None,
                )

                def explode(argv: list[str]) -> subprocess.CompletedProcess[str]:
                    raise AssertionError(f"hermes must not shell out to agentsview: {argv}")

                result = _parse_ref(ref, explode)
            finally:
                del os.environ["HERMES_HOME"]
            self.assertEqual([c.name for c in result.calls], ["terminal"])
            self.assertEqual(result.calls[0].agent, "hermes")

    def test_non_hermes_agentsview_ref_still_uses_the_runner(self) -> None:
        import subprocess

        from toolbench.passive import _parse_ref
        from toolbench.sources import SessionRef

        line = json.dumps(
            {
                "sessionId": "s",
                "timestamp": "2026-07-08T00:00:00Z",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}
                    ]
                },
            }
        )
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=line + "\n", stderr="")

        ref = SessionRef(
            agent="claude", source="agentsview", project="p", session_id="claude:x", path=None
        )
        result = _parse_ref(ref, runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual([c.name for c in result.calls], ["Bash"])


class PassiveIntegration(unittest.TestCase):
    def test_missing_archive_degrades_to_skipped_not_fatal(self) -> None:
        import io
        import subprocess
        from contextlib import redirect_stdout

        from toolbench.passive import main

        payload = {
            "sessions": [{"id": "hermes:cron_1", "project": "hermes-cron", "agent": "hermes"}],
            "next_cursor": "",
            "total": 1,
        }
        replies = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")
        ]

        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            return replies.pop(0)

        with TemporaryDirectory() as tmp:
            # Point at an empty dir: no state.db, so the archive is unresolvable.
            os.environ["HERMES_HOME"] = tmp
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["--index-source", "agentsview"], runner=runner)
            finally:
                del os.environ["HERMES_HOME"]

        # Exit 0 and name the skipped session: a hermes archive we cannot read
        # degrades the run, never aborts it.
        self.assertEqual(code, 0)
        report = out.getvalue()
        self.assertIn("cron_1", report)
        self.assertIn("skipped roots", report)
        self.assertIn("not in local archive", report)


class LiveArchive(unittest.TestCase):
    """Guards the schema compatibility envelope against the real DBs (v16 and v19)."""

    def test_live_archive_schema_envelope(self) -> None:
        home = Path("~/.hermes").expanduser()
        if not home.is_dir():
            self.skipTest("no live hermes archive")
        dbs = iter_profile_dbs(home)
        if not dbs:
            self.skipTest("no hermes profile databases")
        for db in dbs:
            conn = _connect(db)  # an idle profile has no -shm; raw mode=ro cannot read it
            try:
                scols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
                mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            finally:
                conn.close()
            self.assertLessEqual(set(_SESSION_COLS), scols, f"{db} sessions")
            self.assertLessEqual(set(_MESSAGE_COLS), mcols, f"{db} messages")


class ConnectWalWithoutShm(unittest.TestCase):
    """A WAL database whose -wal/-shm sidecars are absent must still open read-only.

    This is the state of ~/.hermes/profiles/aphrodite-mood/state.db: the WAL flag
    is set in the file header, but neither sidecar is on disk. SQLite cannot take a
    read lock without recreating the -shm, which `mode=ro` forbids.
    """

    def _make_wal_db(self, path: Path, *, sidecars: bool = False) -> None:
        """Build a WAL database, then model an idle profile by removing the sidecars.

        SQLite does *not* delete -wal/-shm on close here (sqlite 3.43.2, macOS), so
        the sidecar-less state has to be produced explicitly rather than assumed.
        """
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("CREATE TABLE messages (id INTEGER)")
        conn.execute("INSERT INTO messages VALUES (1)")
        conn.commit()
        conn.close()
        if not sidecars:
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)

    def test_fixture_models_an_idle_profile(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            self.assertFalse(db.with_name(db.name + "-wal").exists())
            self.assertFalse(db.with_name(db.name + "-shm").exists())
            header = db.read_bytes()[:20]
            self.assertEqual((header[18], header[19]), (2, 2), "not a WAL header")

    def test_plain_mode_ro_cannot_read_such_a_db(self) -> None:
        """Pins the bug itself, and pins why `SELECT 1` is not a health check.

        `SELECT 1` is a constant expression: it reads no page, opens no read
        transaction, and therefore succeeds on a database SQLite cannot read.
        """
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.execute("SELECT 1").fetchone()  # succeeds — proves the probe is useless
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()

    def test_connect_reads_a_wal_db_with_no_shm(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            conn = _connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_mode_ro_tolerates_a_present_wal(self) -> None:
        """Pins the real SQLite behaviour the guard is written against.

        `mode=ro` fails only when the -wal is *absent* while the header says WAL.
        A -wal that is present -- empty, truncated, or carrying frames -- reads
        fine. So the guard below defends a state that cannot arise naturally.
        """
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            db.with_name(db.name + "-wal").touch()
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_connect_refuses_immutable_when_wal_frames_may_be_pending(self) -> None:
        """Defensive invariant, exercised by fault injection.

        Should `mode=ro` ever fail while a -wal exists, `immutable=1` would ignore
        that WAL and could return stale rows. `_connect` must re-raise instead.
        """
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db, sidecars=True)
            self.assertTrue(db.with_name(db.name + "-wal").exists())

            real_connect = sqlite3.connect

            def fail_mode_ro(dsn: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
                if "mode=ro" in dsn:
                    raise sqlite3.OperationalError("unable to open database file")
                conn: sqlite3.Connection = real_connect(dsn, *args, **kwargs)
                return conn

            with mock.patch.object(sqlite3, "connect", side_effect=fail_mode_ro):
                with self.assertRaises(sqlite3.OperationalError):
                    _connect(db)

    def test_connect_reads_a_healthy_wal_db_without_falling_back(self) -> None:
        """Sidecars present: `mode=ro` works and the immutable path must not engage."""
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db, sidecars=True)
            conn = _connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_connect_never_opens_writable(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            conn = _connect(db)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO messages VALUES (2)")


if __name__ == "__main__":
    unittest.main()
