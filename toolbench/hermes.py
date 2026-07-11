"""Direct SQLite reads for hermes sessions (TB-11). Stdlib only.

`agentsview session export hermes:<id>` returns rc=0 and streams the whole default
profile database instead of that session's JSONL transcript, so every hermes session
is demoted to `skipped_roots` and contributes zero tool calls. This module reads the
sessions straight from hermes' own archive instead.

Discovery deliberately stays with AgentsView. The corpus is *defined* as what
`agentsview session list` returns, and every agent is sampled through that one path;
enumerating the hermes archive here would redefine the corpus for a single agent and
skew every cross-agent rate. So `passive` still asks AgentsView which sessions exist
and only routes the *read* through this module.

Hermes is consequently under-sampled: `session list --agent hermes` returns 89 sessions
where `agentsview stats --agent hermes` counts 789 from the same archive. That is an
upstream defect (kenn-io/agentsview#1048), not one to route around here. The export bug
this module exists for is kenn-io/agentsview#1047.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from toolbench.adapters import SessionAdapter
from toolbench.parsers import _PendingCall
from toolbench.sources import NonTranscriptExport, SessionRef
from toolbench.transcript import ParseResult, ToolCall, UsageProvenance, result_len

# Session ids arrive from AgentsView namespaced by agent; the archive stores them bare.
_ID_PREFIX = "hermes:"


def hermes_home() -> Path:
    """The hermes archive root. `HERMES_HOME` overrides for tests and non-default installs."""
    override = os.environ.get("HERMES_HOME")
    return Path(override) if override else Path("~/.hermes").expanduser()


def iter_profile_dbs(home: Path | None = None) -> list[Path]:
    """Every profile database, default first, then profiles sorted by name.

    A session lives in exactly one of these. Two of the 29 in-corpus sessions live in
    `profiles/aphrodite-mood`, which `agentsview session export` never reaches -- it
    always streams the default profile, whichever session you ask for.
    """
    root = home if home is not None else hermes_home()
    if not root.is_dir():
        raise NonTranscriptExport(f"hermes archive not found: {root}")
    dbs = []
    default = root / "state.db"
    if default.is_file():
        dbs.append(default)
    dbs.extend(sorted((root / "profiles").glob("*/state.db")))
    return dbs


def _connect(db: Path) -> sqlite3.Connection:
    """Open a hermes archive database read-only.

    mode=ro: a running hermes owns this file. Never open it writable.

    On some SQLite builds (observed 3.43.x), a WAL-header database with no `-shm`
    sidecar cannot be read under `mode=ro` at all -- SQLite needs the shared-memory
    file to take a read lock, and read-only mode may not create it. Idle hermes
    profiles have been found in exactly that state. Newer builds (observed 3.45.x)
    can read the same on-disk shape under `mode=ro`; the fallback below is then
    unused but still correct.

    `immutable=1` opens a sidecar-less WAL file on the older builds, but it ignores
    the WAL entirely and will silently return stale rows if frames are pending.
    Fall back to it only when no `-wal` sidecar exists, which is precisely when
    there are no frames to miss.

    The probe must touch a page. `sqlite3.connect` is lazy, and `SELECT 1` is a
    constant expression that never opens a read transaction -- both succeed on a
    database that cannot actually be read. Reading `sqlite_master` is what fails
    on builds that still reject the sidecar-less shape.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError:
        if db.with_name(db.name + "-wal").exists():
            raise
        return sqlite3.connect(f"file:{db}?immutable=1", uri=True)


def _bare_id(session_id: str) -> str:
    return session_id[len(_ID_PREFIX) :] if session_id.startswith(_ID_PREFIX) else session_id


def resolve_session(session_id: str, home: Path | None = None) -> Path | None:
    """Return the profile database holding `session_id`, or None if no profile has it."""
    bare = _bare_id(session_id)
    for db in iter_profile_dbs(home):
        with closing(_connect(db)) as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (bare,)).fetchone():
                return db
    return None


def _error_of(payload: str | None) -> str | None:
    """Hermes' error convention: results are JSON dicts whose `error` key is null on
    success. The key is present on nearly every row, so only its value is a signal.
    Non-JSON results (plain strings) carry no signal at all.
    """
    if payload is None:
        return None
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(decoded, dict) and decoded.get("error") is not None:
        return "tool_error"
    return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def parse_hermes_session(
    session_id: str,
    *,
    agent: str = "hermes",
    source: str = "agentsview",
    project: str = "hermes",
    home: Path | None = None,
) -> ParseResult:
    """Join one hermes session's tool calls to their results, straight from SQLite.

    The join is `tool_calls[].id -> messages.tool_call_id`: assistant rows carry an
    OpenAI-shaped `tool_calls` blob, and each result arrives as its own `role='tool'`
    row. A call with no matching result is kept with `no_result=True` (S6), never
    dropped. A `tool_calls` blob that will not parse is counted as malformed and
    skipped, never fatal (S5).

    `usage` is always None: hermes carries usage on the *session* row
    (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens), not per
    tool call. `messages.token_count` exists in the schema but is NULL on all
    10,177 rows across every archive database, so there is no honest per-call
    usage record to report. The granularity gap is session -> call, not
    message -> call. Stamped ABSENT_BY_SCHEMA, never ABSENT_UNEXPECTED: the
    producer knows why it has nothing to say (S29).

    The session row's own `cache_read_tokens` IS read here and threaded onto
    `ParseResult.session_cache_read_tokens` (S32/TB-20) -- session-grain, never
    attributed to any one call. Dividing it by `tool_call_count` to fabricate a
    per-call rate is exactly the class of error S29 exists to eliminate, so this
    function does not do it; `passive.py` reports the session-grain figure as an
    agent-level caveat instead.
    """
    db = resolve_session(session_id, home)
    if db is None:
        raise NonTranscriptExport(f"hermes session not in local archive: {session_id}")
    bare = _bare_id(session_id)

    with closing(_connect(db)) as conn:
        row = conn.execute(
            "SELECT model, cache_read_tokens FROM sessions WHERE id = ?", (bare,)
        ).fetchone()
        model = row[0] if row else None
        session_cache_read_tokens = row[1] if row else None

        results: dict[str, str | None] = {
            call_id: content
            for call_id, content in conn.execute(
                "SELECT tool_call_id, content FROM messages"
                " WHERE session_id = ? AND role = 'tool' AND tool_call_id IS NOT NULL",
                (bare,),
            )
        }
        assistant_rows = conn.execute(
            "SELECT tool_calls, timestamp FROM messages"
            " WHERE session_id = ? AND tool_calls IS NOT NULL ORDER BY timestamp, id",
            (bare,),
        ).fetchall()

    calls: list[ToolCall] = []
    malformed = 0
    for blob, timestamp in assistant_rows:
        try:
            blocks = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            malformed += 1
            continue
        if not isinstance(blocks, list):
            malformed += 1
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            call_id = block.get("id") or block.get("call_id")
            function = block.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            arguments = function.get("arguments")
            found = call_id in results
            payload = results.get(call_id)
            model_str = model if isinstance(model, str) else None
            calls.append(
                _PendingCall(
                    name=name,
                    input_chars=result_len(arguments) if arguments is not None else 0,
                    session_id=bare,
                    ts=_iso(timestamp),
                    usage=None,
                    usage_provenance=UsageProvenance.ABSENT_BY_SCHEMA,
                    model=model_str,
                ).finish(
                    agent=agent,
                    source=source,
                    project=project,
                    output_chars=result_len(payload) if found and payload is not None else 0,
                    error=_error_of(payload) if found else None,
                    result_source="hermes_sqlite" if found else None,
                    no_result=not found,
                )
            )

    return ParseResult(
        calls=calls, malformed=malformed, session_cache_read_tokens=session_cache_read_tokens
    )


class HermesAdapter(SessionAdapter):
    """Hermes is keyed on source, not content: it is a SQLite read, not a transcript.

    It has no lines, so it implements `SessionAdapter` directly rather than being
    forced through the loader/parser pipe. Yielding synthetic JSON lines so a
    `HermesParser` could re-decode them buys symmetry and nothing else.
    """

    def claims(self, ref: SessionRef) -> bool:
        # `agentsview session export` returns rc=0 and the whole default-profile
        # database for these (kenn-io/agentsview#1047), so read the archive
        # directly. A hermes ref that DOES carry a path is a real transcript and
        # belongs to the composed adapter.
        return ref.agent == "hermes" and ref.path is None

    def parse(self, ref: SessionRef) -> ParseResult:
        return parse_hermes_session(
            ref.session_id, agent=ref.agent, source=ref.source, project=ref.project
        )
