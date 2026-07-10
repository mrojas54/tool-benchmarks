# TB-18 Usage Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the absence of a usage channel explicit and typed, so `passive.py` stops reporting an unmeasured cache flag as `"no"` and `probe.py` stops silently regrouping turns by timestamp.

**Architecture:** Split the *producer* out of the *schema*. `hermes sessions export --format trace` emits Claude-shaped JSONL that self-declares via `version: "hermes-agent"`, so a `HermesTraceParser(ClaudeParser)` subclass claims it at dispatch and stamps `UsageProvenance.ABSENT_BY_EXPORT` on every row. Provenance rides on `ToolCall` into `Reducer`'s streaming fold as a `usage_missing` counter (a counter, not a scalar — trace and real-transcript rows can share one `(agent, tool)` bucket). `probe.py` refuses trace input at dispatch and raises on any entry lacking `requestId`.

**Tech Stack:** Python 3, stdlib only for `toolbench/*`. `uv` for dependency and command running. `pytest` for tests. `ruff` + `mypy --strict` for the gate.

## Global Constraints

- **Design of record:** `docs/superpowers/specs/2026-07-09-hermes-trace-usage-provenance-design.md` (commit `2c0b196`). Every task traces to a section there.
- **Repo:** `/Users/michellerojas/tool-benchmarks`, branch `chore/add-hermes-cli-export-plan`.
- **`toolbench/` is stdlib-only.** No new third-party dependencies. `enum` is stdlib.
- **The gate, run before every commit:**
  ```bash
  uv run ruff check .
  uv run mypy --strict
  uv run pytest -q
  ```
- **The test runner is `pytest`, not `unittest`.** The plan brief said `uv run python -m unittest`; that command does **not** collect this suite (`tests/test_adapters.py` uses bare `pytest` functions, which `unittest` discovery ignores) and it executes module-level code, printing a probe report instead of test results. Use `uv run pytest -q`.
- **Baseline is RED, not green.** The brief said "213 passing / 1 skipped." Actual, verified on this machine:
  ```
  1 failed, 212 passed
  FAILED tests/test_hermes.py::LiveArchive::test_live_archive_schema_envelope
  ```
  That failure is a real product bug. **Task 0 fixes it.** Do not start Task 1 until `uv run pytest -q` reports `213 passed`.
- **`usage=None` must never regain a second meaning.** No default value on the new `ToolCall` field; no `None` sentinel meaning "infer."
- **Strict TDD.** RED → GREEN → DOCS. Each task commits its own test-first cycle. Never write implementation before the failing test runs and fails for the stated reason.
- **Never open a hermes database writable.** `~/.hermes/*.db` is owned by a possibly-running hermes process.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `toolbench/hermes.py` | SQLite adapter. `_connect` read-only open; stamps `ABSENT_BY_SCHEMA`. | 0, 1, 6 |
| `toolbench/transcript.py` | `ToolCall`, `ParseResult`, **new** `UsageProvenance` enum. | 1 |
| `toolbench/parsers.py` | `ClaudeParser`, **new** `HermesTraceParser`, `_provenance` hook. | 1, 2 |
| `toolbench/adapters.py` | `detect_parser`, `PARSERS` tuple. | 2 |
| `toolbench/passive.py` | `ToolStats.usage_missing`, four-case `cache_note`. | 3 |
| `toolbench/probe.py` | `NonIsolableTurns`, `_turn_key` refusal, dispatch refusal. | 5 |
| `tests/fixtures/schema_hermes_trace.jsonl` | **New.** Trace-shaped golden fixture. | 2 |
| `tests/fixtures/probe_session*.jsonl` | Migrate 5 fixtures to carry `requestId`. | 4 |
| `SPEC.md` | S29, S30. | 6 |

---

## Task 0: Repair the read-only hermes open (unblocks a green baseline)

Design §8. Filed as out-of-scope there; promoted into this plan because it fails the suite today and TDD cannot proceed against a red baseline.

**Root cause, verified.** `~/.hermes/profiles/aphrodite-mood/state.db` is a WAL-mode database (file header bytes 18/19 both `2`) whose `-wal` and `-shm` sidecars are absent. SQLite cannot open a WAL database read-only without a `-shm`, so `mode=ro` raises `SQLITE_CANTOPEN`. The `sqlite3` CLI fails identically, so this is not a Python artifact. `PRAGMA journal_mode` reports `delete` when queried under `immutable=1` — it lies, because `immutable` disables WAL. Trust the header, not the pragma.

**Why the guard is `-wal`-existence and not a version check.** `immutable=1` ignores the WAL, so it can read stale data: on `tech-interviewing/state.db` it reports 639 messages where `mode=ro` reports 644. Falling back to `immutable` **only when no `-wal` sidecar exists** structurally excludes that risk — there are no pending frames to miss. Verified: with the guard, `tech-interviewing` stays on `mode=ro` and returns the correct 644.

**`sqlite3.connect()` is lazy.** It succeeds on an unopenable database; the error surfaces at the first `execute()`. A `try/except` around `connect` alone catches nothing. The connection must be probed.

**Files:**
- Modify: `toolbench/hermes.py:61-63` (`_connect`)
- Test: `tests/test_hermes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_connect(db: Path) -> sqlite3.Connection` — unchanged signature, now succeeds on a sidecar-less WAL database.

- [ ] **Step 1: Confirm the baseline is red for the stated reason**

```bash
cd /Users/michellerojas/tool-benchmarks
uv run pytest -q 2>&1 | tail -3
```
Expected: `1 failed, 212 passed`, naming `test_live_archive_schema_envelope`.

If instead you see `213 passed`, your machine has no `aphrodite-mood` profile and the test is skipping. **Do not skip this task** — the tests below are hermetic and must still be written.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_hermes.py`:

```python
class ConnectWalWithoutShm(unittest.TestCase):
    """A WAL database whose -wal/-shm sidecars are absent must still open read-only.

    A clean SQLite close removes both sidecars but leaves the WAL flag in the file
    header, which is exactly the state of ~/.hermes/profiles/aphrodite-mood/state.db.
    """

    def _make_wal_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE messages (id INTEGER)")
        conn.execute("INSERT INTO messages VALUES (1)")
        conn.commit()
        conn.close()  # clean close: -wal and -shm are removed, header stays WAL

    def test_wal_header_survives_clean_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            self.assertFalse(db.with_name(db.name + "-wal").exists())
            self.assertFalse(db.with_name(db.name + "-shm").exists())
            header = db.read_bytes()[:20]
            self.assertEqual((header[18], header[19]), (2, 2), "not a WAL header")

    def test_connect_reads_a_wal_db_with_no_shm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            conn = _connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_connect_refuses_immutable_when_wal_frames_may_be_pending(self) -> None:
        """A -wal sidecar means immutable=1 could silently skip committed frames."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            db.with_name(db.name + "-wal").touch()  # pending frames may exist
            db.with_name(db.name + "-shm").unlink(missing_ok=True)
            with self.assertRaises(sqlite3.OperationalError):
                _connect(db)

    def test_connect_never_opens_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self._make_wal_db(db)
            conn = _connect(db)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO messages VALUES (2)")
```

Ensure `tests/test_hermes.py` imports what these need. Add any that are missing to the existing import block:

```python
import sqlite3
import tempfile
import unittest
from pathlib import Path

from toolbench.hermes import _connect
```

- [ ] **Step 3: Run the new tests to verify they fail**

```bash
uv run pytest tests/test_hermes.py::ConnectWalWithoutShm -q
```
Expected: `test_connect_reads_a_wal_db_with_no_shm` FAILS with `sqlite3.OperationalError: unable to open database file`. The other three pass already (they describe existing behaviour we must not break).

- [ ] **Step 4: Implement the fix**

Replace `toolbench/hermes.py:61-63` entirely:

```python
def _connect(db: Path) -> sqlite3.Connection:
    """Open a hermes archive database read-only.

    mode=ro: a running hermes owns this file. Never open it writable.

    A WAL database with no `-shm` sidecar cannot be opened `mode=ro` at all --
    SQLite needs the shared-memory file to read the WAL index. A clean hermes
    shutdown removes `-wal` and `-shm` but leaves the WAL flag in the header, so
    this is the normal state of an idle profile, not a corrupt one.

    `immutable=1` opens such a file, but it ignores the WAL entirely and will
    silently return stale rows if frames are pending. Fall back to it only when
    no `-wal` sidecar exists, which is precisely when there are no frames to miss.
    `sqlite3.connect` is lazy, so the connection must be exercised to know whether
    it actually opened.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.OperationalError:
        if db.with_name(db.name + "-wal").exists():
            raise
        return sqlite3.connect(f"file:{db}?immutable=1", uri=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_hermes.py::ConnectWalWithoutShm -q
```
Expected: `4 passed`.

- [ ] **Step 6: Verify the baseline is now green**

```bash
uv run pytest -q 2>&1 | tail -2
```
Expected: `217 passed` (213 pre-existing + 4 new). **If anything else fails, stop and report — do not proceed.**

- [ ] **Step 7: Gate and commit**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q
git add toolbench/hermes.py tests/test_hermes.py
git commit -m "fix(hermes): open sidecar-less WAL databases read-only

_connect used mode=ro, which cannot open a WAL database whose -shm is
absent -- the normal state of a cleanly-closed profile. aphrodite-mood
(2,006 messages) was invisible to the adapter and failed the live-archive
test.

Falls back to immutable=1 only when no -wal sidecar exists, so there are
no pending frames to miss. immutable is not a general substitute: it
ignores the WAL and reads 639 rows where mode=ro reads 644 on
tech-interviewing.

sqlite3.connect is lazy, so the connection is probed before returning."
```

---

## Task 1: `UsageProvenance` on `ToolCall`

Design §4.1. This task is **atomic**: adding a non-defaulted field breaks every construction site, so `transcript.py`, `parsers.py`, `hermes.py`, and both test factories move together or the suite never goes green.

**Files:**
- Modify: `toolbench/transcript.py:42-59` (add enum, add field)
- Modify: `toolbench/parsers.py:19-28` (`_PendingCall`), `:130-139`, `:160-176`, `:180-197`
- Modify: `toolbench/hermes.py:166-183`
- Modify: `tests/test_transcript.py:43-59`, `tests/test_passive.py:56-72` (factories)
- Test: `tests/test_transcript.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `UsageProvenance` — `Enum` in `toolbench.transcript` with members `PRESENT`, `ABSENT_BY_SCHEMA`, `ABSENT_BY_EXPORT`, `ABSENT_UNEXPECTED`.
  - `ToolCall.usage_provenance: UsageProvenance` — **no default**, positioned immediately after `usage`.
  - `ClaudeParser._provenance(usage: object) -> UsageProvenance` — classmethod, overridable.

**Why no default.** A default silently stamps every unconverted call site `PRESENT`, reintroducing the fabricated certainty this ticket exists to remove. With no default, `mypy --strict` enumerates every site for you.

**Field ordering is forced.** `ToolCall` ends with `no_result: bool = False` and `result_source: str | None = None`. A dataclass cannot place a non-defaulted field after a defaulted one. Insert `usage_provenance` directly after `usage`, which it annotates.

**Positional construction is not a risk here** (verified): all four construction sites use keywords, and both test factories build via `ToolCall(**fields)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript.py`:

```python
class UsageProvenanceTests(unittest.TestCase):
    def test_enum_has_exactly_four_arms(self) -> None:
        self.assertEqual(
            {m.name for m in UsageProvenance},
            {"PRESENT", "ABSENT_BY_SCHEMA", "ABSENT_BY_EXPORT", "ABSENT_UNEXPECTED"},
        )

    def test_tool_call_has_no_default_provenance(self) -> None:
        """A default would silently mark unconverted call sites PRESENT."""
        field = {f.name: f for f in dataclasses.fields(ToolCall)}["usage_provenance"]
        self.assertIs(field.default, dataclasses.MISSING)
        self.assertIs(field.default_factory, dataclasses.MISSING)

    def test_provenance_precedes_the_defaulted_fields(self) -> None:
        names = [f.name for f in dataclasses.fields(ToolCall)]
        self.assertLess(names.index("usage_provenance"), names.index("no_result"))
        self.assertEqual(names[names.index("usage") + 1], "usage_provenance")


class ClaudeProvenanceHookTests(unittest.TestCase):
    def test_dict_usage_is_present(self) -> None:
        self.assertIs(ClaudeParser._provenance({"input_tokens": 1}), UsageProvenance.PRESENT)

    def test_empty_dict_usage_is_present_a_measured_zero(self) -> None:
        """The channel existed and reported nothing. That is a measurement."""
        self.assertIs(ClaudeParser._provenance({}), UsageProvenance.PRESENT)

    def test_missing_usage_is_absent_unexpected(self) -> None:
        self.assertIs(ClaudeParser._provenance(None), UsageProvenance.ABSENT_UNEXPECTED)

    def test_non_dict_usage_is_absent_unexpected(self) -> None:
        self.assertIs(ClaudeParser._provenance("42"), UsageProvenance.ABSENT_UNEXPECTED)
```

Add to that file's imports:

```python
import dataclasses

from toolbench.parsers import ClaudeParser
from toolbench.transcript import UsageProvenance
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_transcript.py -q
```
Expected: FAIL — `ImportError: cannot import name 'UsageProvenance' from 'toolbench.transcript'`.

- [ ] **Step 3: Add the enum and the field**

In `toolbench/transcript.py`, add to the imports:

```python
from enum import Enum
```

Insert above `class ToolCall`:

```python
class UsageProvenance(Enum):
    """Why a `ToolCall` does or does not carry a usage record (S29).

    `usage=None` previously meant three different things at once. Each arm below
    is one of them, made explicit. Only a producer may assert ABSENT_BY_SCHEMA or
    ABSENT_BY_EXPORT; a parser that merely fails to find usage on a schema that
    promises it records ABSENT_UNEXPECTED and says nothing about the cause.
    """

    PRESENT = "present"
    ABSENT_BY_SCHEMA = "absent_by_schema"      # producer has no per-call usage (hermes SQLite)
    ABSENT_BY_EXPORT = "absent_by_export"      # producer had usage; the export dropped it (trace)
    ABSENT_UNEXPECTED = "absent_unexpected"    # claude schema, claude producer, no usage: anomaly
```

In `ToolCall`, insert the field immediately after `usage`:

```python
    usage: dict[str, object] | None
    usage_provenance: UsageProvenance
    duration_ms: float | None
```

- [ ] **Step 4: Thread it through `parsers.py`**

Add to imports: `from toolbench.transcript import ParseResult, ToolCall, UsageProvenance, result_len`

Add the field to `_PendingCall` (after `usage`):

```python
@dataclass
class _PendingCall:
    """A `tool_use` block awaiting its matching result."""

    name: str
    input_chars: int
    session_id: str
    ts: str
    usage: dict[str, object] | None
    usage_provenance: UsageProvenance
    model: str | None
```

Add the hook to `ClaudeParser`, directly below `claims_line`:

```python
    @classmethod
    def _provenance(cls, usage: object) -> UsageProvenance:
        """Overridden by producers that know why usage is absent (S29).

        A classmethod, not a ClassVar: a ClassVar would need a `None` sentinel on
        ClaudeParser meaning "infer per row", reintroducing a null with two
        meanings inside the design meant to eliminate one.
        """
        return (
            UsageProvenance.PRESENT
            if isinstance(usage, dict)
            else UsageProvenance.ABSENT_UNEXPECTED
        )
```

In `ClaudeParser.parse`, where `_PendingCall` is built (currently `parsers.py:132-139`):

```python
                    pending[tool_use_id] = _PendingCall(
                        name=name,
                        input_chars=result_len(tool_use_block.get("input")),
                        session_id=session_id_str,
                        ts=ts_str,
                        usage=usage if isinstance(usage, dict) else None,
                        usage_provenance=self._provenance(usage),
                        model=model if isinstance(model, str) else None,
                    )
```

`self._provenance(...)` dispatches on `type(self)`, so `HermesTraceParser` overrides it in Task 2 with no further edit here.

In **both** `ToolCall(...)` constructions in `parse` (the joined call at `:160` and the S6 unmatched call at `:180`), add one line after `usage=pending_call.usage,`:

```python
                        usage_provenance=pending_call.usage_provenance,
```

- [ ] **Step 5: Thread it through `hermes.py`**

Add `UsageProvenance` to the `toolbench.transcript` import. At `hermes.py:176`, replace `usage=None,` with:

```python
                    usage=None,
                    usage_provenance=UsageProvenance.ABSENT_BY_SCHEMA,
```

Correct the misleading rationale at `hermes.py:116` (the docstring paragraph explaining `usage`). Replace the sentence claiming hermes "records `token_count` per message" with:

```
    `usage` is always None: hermes carries usage on the *session* row
    (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens), not per
    tool call. `messages.token_count` exists in the schema but is NULL on all
    10,133 rows across every archive database, so there is no honest per-call
    usage record to report. The granularity gap is session -> call.
```

- [ ] **Step 6: Update both test factories**

`tests/test_transcript.py` `_make` and `tests/test_passive.py` `make_call` both build `ToolCall(**fields)` and will now raise `TypeError: missing 1 required positional argument`. In **each**, after `fields.update(overrides)` and before the `return`, add:

```python
        fields.setdefault(
            "usage_provenance",
            UsageProvenance.PRESENT
            if fields["usage"] is not None
            else UsageProvenance.ABSENT_UNEXPECTED,
        )
```

This mirrors `ClaudeParser._provenance`, so every existing test keeps its intended meaning: a test passing `usage={...}` still reads as a real measurement, and one passing `usage=None` still reads as an absence. An explicit `usage_provenance=` override wins.

Import `UsageProvenance` in both test modules.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
uv run pytest tests/test_transcript.py -q && uv run pytest -q 2>&1 | tail -2
```
Expected: new tests pass; full suite `225 passed` (217 + 8 new). If `mypy` later names a `ToolCall(` site you missed, that is the safety net working.

- [ ] **Step 8: Gate and commit**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q
git add toolbench/transcript.py toolbench/parsers.py toolbench/hermes.py tests/test_transcript.py tests/test_passive.py
git commit -m "feat(transcript): type the absence of usage with UsageProvenance

usage=None meant three things: absent by schema (hermes SQLite), absent by
export (trace), and present-but-empty. Each is now an explicit enum arm on
ToolCall, with no default -- a default would stamp unconverted call sites
PRESENT and re-fabricate the certainty this removes.

Provenance is a polymorphic _provenance() classmethod rather than a ClassVar,
so ClaudeParser needs no 'None = infer' sentinel.

Also corrects hermes.py's docstring: hermes does not record token_count per
message (NULL on all 10,133 rows); usage lives on the session row."
```

---

## Task 2: `HermesTraceParser` — split the producer from the schema

Design §4.1, §2.3. Acceptance S29.

**The discriminator is a positive producer tag, not an absence.** Trace exports carry `version: "hermes-agent"` on every record. Verified as a total partition over the full local archive: 4,036/4,036 real transcripts still claimed by `ClaudeParser` (all at decodable line 1), zero tagged `hermes-agent`; 705/705 trace records claimed by `HermesTraceParser`, zero by `ClaudeParser`, zero by both.

**`detect_parser` has no precedence.** It collects every parser whose `claims_line` returns `True` and raises `AmbiguousSchema` on more than one — documented as "a programming error, not a data error." The two predicates must therefore *partition*. They do, on `version`.

**Files:**
- Modify: `toolbench/parsers.py` (add `HERMES_TRACE_VERSION`, tighten `ClaudeParser.claims_line`, add `HermesTraceParser`)
- Modify: `toolbench/adapters.py:17`, `:29` (import and register)
- Create: `tests/fixtures/schema_hermes_trace.jsonl`
- Modify: `tests/test_adapters.py:24,31,78,83` (tighten `isinstance` asserts)
- Test: `tests/test_adapters.py`, `tests/test_parsers.py`

**Interfaces:**
- Consumes: `UsageProvenance`, `ClaudeParser._provenance` (Task 1).
- Produces:
  - `HERMES_TRACE_VERSION: str = "hermes-agent"` in `toolbench.parsers`
  - `HermesTraceParser(ClaudeParser)` with `schema_tag = "hermes-trace"`
  - `PARSERS = (ClaudeParser, HermesTraceParser)` in `toolbench.adapters`

**The `isinstance` tax.** `HermesTraceParser` **is-a** `ClaudeParser`, so the four existing `assert isinstance(parser, ClaudeParser)` assertions keep passing on trace input. They are *silently weakened*, not broken — the worst failure mode a test has. Tighten them to `type(parser) is ClaudeParser`.

- [ ] **Step 1: Create the golden fixture**

Create `tests/fixtures/schema_hermes_trace.jsonl` with exactly these three lines (shapes taken from a real `hermes sessions export --format trace` run; note `version`, and the absence of both `requestId` and `message.usage`):

```jsonl
{"parentUuid":null,"isSidechain":false,"userType":"external","cwd":"/tmp/proj","sessionId":"20260630_041900_cb98e9","version":"hermes-agent","gitBranch":"","uuid":"aaaa0000-0000-4000-8000-000000000001","timestamp":"2026-07-10T00:50:30.000Z","type":"user","message":{"role":"user","content":[{"type":"text","text":"read notes.md"}]}}
{"parentUuid":"aaaa0000-0000-4000-8000-000000000001","isSidechain":false,"userType":"external","cwd":"/tmp/proj","sessionId":"20260630_041900_cb98e9","version":"hermes-agent","gitBranch":"","uuid":"aaaa0000-0000-4000-8000-000000000002","timestamp":"2026-07-10T00:50:31.403Z","type":"assistant","message":{"role":"assistant","model":"anthropic/claude-sonnet-4.6","content":[{"type":"text","text":"I'll read it."},{"type":"tool_use","id":"chatcmpl-tool-bb09f5b3","name":"read_file","input":{"path":"notes.md"}}]}}
{"parentUuid":"aaaa0000-0000-4000-8000-000000000002","isSidechain":false,"userType":"external","cwd":"/tmp/proj","sessionId":"20260630_041900_cb98e9","version":"hermes-agent","gitBranch":"","uuid":"aaaa0000-0000-4000-8000-000000000003","timestamp":"2026-07-10T00:50:31.403Z","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"chatcmpl-tool-bb09f5b3","content":"line one\nline two"}]}}
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_adapters.py`:

```python
from toolbench.parsers import HermesTraceParser


def test_hermes_trace_fixture_detects_as_hermes_trace_not_claude():
    parser, _ = detect_parser(_lines("schema_hermes_trace.jsonl"))
    assert type(parser) is HermesTraceParser


def test_claude_and_hermes_trace_predicates_partition():
    claude_line = {"sessionId": "s1", "version": "2.1.205"}
    trace_line = {"sessionId": "s1", "version": "hermes-agent"}
    assert ClaudeParser.claims_line(claude_line)
    assert not HermesTraceParser.claims_line(claude_line)
    assert HermesTraceParser.claims_line(trace_line)
    assert not ClaudeParser.claims_line(trace_line)


def test_claude_claims_a_line_with_no_version_field():
    """Real transcripts open with a preamble record that carries no `version`."""
    assert ClaudeParser.claims_line({"sessionId": "s1"})


def test_hermes_trace_needs_session_id_too():
    assert not HermesTraceParser.claims_line({"version": "hermes-agent"})
```

Append to `tests/test_parsers.py`:

```python
def test_hermes_trace_parses_cleanly_and_stamps_absent_by_export():
    """The hazard: it parses, raises nothing, reports 0 malformed -- and has no usage."""
    lines = (FIXTURES / "schema_hermes_trace.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    result = HermesTraceParser().parse(iter(lines), agent="claude-code", source="raw", project="p")
    assert result.malformed == 0
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.name == "read_file"
    assert call.usage is None
    assert call.usage_provenance is UsageProvenance.ABSENT_BY_EXPORT


def test_hermes_trace_provenance_ignores_a_usage_dict_entirely():
    """Unconditional: trace never carries usage, so the arm cannot depend on the value."""
    assert HermesTraceParser._provenance({"input_tokens": 5}) is UsageProvenance.ABSENT_BY_EXPORT
    assert HermesTraceParser._provenance(None) is UsageProvenance.ABSENT_BY_EXPORT
```

Import `HermesTraceParser`, `UsageProvenance`, and `FIXTURES` in `tests/test_parsers.py` as that module already does for `ClaudeParser`.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_adapters.py tests/test_parsers.py -q
```
Expected: FAIL — `ImportError: cannot import name 'HermesTraceParser' from 'toolbench.parsers'`.

- [ ] **Step 4: Implement**

In `toolbench/parsers.py`, add below the imports:

```python
# `hermes sessions export --format trace` stamps this on every record. It is a
# positive producer declaration, not an inference from a missing field: verified
# on 618/618 trace records, and absent from all 4036 real claude transcripts.
HERMES_TRACE_VERSION = "hermes-agent"
```

Tighten `ClaudeParser.claims_line`:

```python
    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        # Every claude/cowork control and message record carries `sessionId`.
        # `tool_use` itself is NOT a usable discriminator: line 0 is a
        # `last-prompt` / `mode` record, and a session that used no tools has
        # no `tool_use` block anywhere.
        #
        # `version` excludes hermes trace exports, which are claude-SHAPED but
        # have a different producer and different guarantees. Detection asserts
        # exactly one parser claims a line, so this predicate must not overlap
        # HermesTraceParser's.
        return "sessionId" in entry and entry.get("version") != HERMES_TRACE_VERSION
```

Append at the end of `toolbench/parsers.py`:

```python
class HermesTraceParser(ClaudeParser):
    """`hermes sessions export --format trace`: the claude schema, a different producer.

    Inherits the entire parse path -- the export really is claude-shaped, which is
    why a lone ClaudeParser once swallowed it silently (TB-18). What differs is the
    guarantee: the trace serializer drops `message.usage` and `requestId`, so every
    call it yields has an unmeasurable usage channel. Because the producer declares
    itself in `version`, this parser can name the cause rather than merely observe
    the absence.

    `requestId` is likewise absent. That is `probe.py`'s problem, not this parser's;
    see S30.
    """

    schema_tag: ClassVar[str] = "hermes-trace"

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        return "sessionId" in entry and entry.get("version") == HERMES_TRACE_VERSION

    @classmethod
    def _provenance(cls, usage: object) -> UsageProvenance:
        # Unconditional: trace never carries usage, so this must not consult the value.
        return UsageProvenance.ABSENT_BY_EXPORT
```

In `toolbench/adapters.py`, update the import and the registry:

```python
from toolbench.parsers import ClaudeParser, HermesTraceParser, TranscriptParser
```

```python
# Ordered by nothing in particular: detection asserts exactly one parser claims a
# line, so order cannot silently decide a tie. ClaudeParser and HermesTraceParser
# partition on `version`. `CodexParser` joins here in TB-12.
PARSERS: tuple[type[TranscriptParser], ...] = (ClaudeParser, HermesTraceParser)
```

- [ ] **Step 5: Tighten the weakened `isinstance` assertions**

In `tests/test_adapters.py`, at lines 24, 31, 78, and 83, replace each

```python
    assert isinstance(parser, ClaudeParser)
```

with

```python
    assert type(parser) is ClaudeParser  # not a subclass: HermesTraceParser is-a ClaudeParser
```

Leave `test_detect_raises_ambiguous_when_two_parsers_claim_one_line` alone: its `Greedy(ClaudeParser)` inherits the tightened `claims_line`, still claims `{"sessionId":"s1"}`, and must still raise `AmbiguousSchema`. That test now also proves the guard fires live rather than merely never firing.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/test_adapters.py tests/test_parsers.py -q && uv run pytest -q 2>&1 | tail -2
```
Expected: new tests pass; full suite `231 passed` (225 + 6 new).

- [ ] **Step 7: Verify the partition against the real archive (not just fixtures)**

```bash
uv run python - <<'PY'
import glob, json, os
from toolbench.parsers import ClaudeParser, HermesTraceParser
files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
claimed = both = 0
for f in files:
    with open(f) as fh:
        for raw in fh:
            s = raw.strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            c, h = ClaudeParser.claims_line(e), HermesTraceParser.claims_line(e)
            if c and h:
                both += 1
            if c:
                claimed += 1
                break
print(f"real transcripts claimed by ClaudeParser: {claimed}/{len(files)}")
print(f"lines claimed by BOTH (must be 0):        {both}")
PY
```
Expected: every transcript claimed; `0` double-claims. If `both > 0`, the partition is broken — stop.

- [ ] **Step 8: Gate and commit**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q
git add toolbench/parsers.py toolbench/adapters.py tests/fixtures/schema_hermes_trace.jsonl tests/test_adapters.py tests/test_parsers.py
git commit -m "feat(parsers): route hermes trace exports to HermesTraceParser

Trace exports carry version: 'hermes-agent' on every record -- a positive
producer declaration. ClaudeParser and HermesTraceParser now partition on it,
so detect_parser's AmbiguousSchema guard can never fire between them.

Splitting the producer out of the schema lets the parser name the cause of a
missing usage channel (ABSENT_BY_EXPORT) rather than merely observe the
absence, which is all a lone ClaudeParser could ever do.

Tightens four isinstance(parser, ClaudeParser) asserts to type(...) is: a
subclass would have satisfied them silently on trace input."
```

---

## Task 3: `passive.py` stops fabricating the cache flag

Design §4.2, §4.3. Acceptance S29.

**Why a counter and not a scalar enum on `ToolStats`.** `tool_stats` is keyed `(agent, call.name)` (`passive.py:106`). The `agent` label comes from the ref, not the parser, so a real Claude transcript (`PRESENT`) and a trace export (`ABSENT_BY_EXPORT`) can land in one bucket. A scalar has no correct value there. `Reducer` is a streaming aggregator that "never stores a corpus-wide call list" (S11), so there is nothing to re-scan afterwards. A counter composes under addition and preserves the mixed case.

**Files:**
- Modify: `toolbench/passive.py:31-40` (`ToolStats`), `:129-131` (`absorb`), `:340-351` (`render_report`)
- Test: `tests/test_passive.py`

**Interfaces:**
- Consumes: `ToolCall.usage_provenance`, `UsageProvenance` (Task 1).
- Produces: `ToolStats.usage_missing: int = 0`; `render_report` emits one of `yes` / `no` / `n/a` / `n/a*`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_passive.py`:

```python
class UsageMissingCounterTests(unittest.TestCase):
    def _absorb(self, *calls: ToolCall) -> Reducer:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=list(calls), malformed=0))
        return reducer

    def test_present_usage_does_not_increment(self) -> None:
        r = self._absorb(make_call(usage={"input_tokens": 1}))
        self.assertEqual(r.tools[("claude-code", "Read")].usage_missing, 0)

    def test_every_absent_arm_increments(self) -> None:
        for arm in (
            UsageProvenance.ABSENT_BY_SCHEMA,
            UsageProvenance.ABSENT_BY_EXPORT,
            UsageProvenance.ABSENT_UNEXPECTED,
        ):
            with self.subTest(arm=arm):
                r = self._absorb(make_call(usage=None, usage_provenance=arm))
                self.assertEqual(r.tools[("claude-code", "Read")].usage_missing, 1)

    def test_empty_usage_dict_is_a_measured_zero_not_a_miss(self) -> None:
        r = self._absorb(make_call(usage={}))
        stats = r.tools[("claude-code", "Read")]
        self.assertEqual(stats.usage_missing, 0)
        self.assertEqual(stats.cache_hits, 0)


class CacheNoteRenderTests(unittest.TestCase):
    def _note(self, *calls: ToolCall) -> str:
        reducer = Reducer()
        reducer.absorb("claude-code", ParseResult(calls=list(calls), malformed=0))
        report = render_report(reducer)
        row = next(l for l in report.splitlines() if "| Read |" in l)
        return row.rstrip("|").rsplit("|", 1)[-1].strip()

    def test_yes_when_a_hit_was_observed(self) -> None:
        self.assertEqual(self._note(make_call(usage={"cache_read_input_tokens": 5})), "yes")

    def test_no_when_usage_was_available_and_zero_hits(self) -> None:
        self.assertEqual(self._note(make_call(usage={"input_tokens": 1})), "no")

    def test_na_when_no_call_could_be_measured(self) -> None:
        self.assertEqual(
            self._note(make_call(usage=None, usage_provenance=UsageProvenance.ABSENT_BY_EXPORT)),
            "n/a",
        )

    def test_na_star_when_only_some_calls_could_be_measured(self) -> None:
        """A trace export and a real transcript share one (agent, tool) bucket.

        Synthetic by necessity: no natural trace corpus carries enough tool calls
        to form a mixed bucket. This is the case a scalar enum cannot express.
        """
        self.assertEqual(
            self._note(
                make_call(usage={"input_tokens": 1}),
                make_call(usage=None, usage_provenance=UsageProvenance.ABSENT_BY_EXPORT),
            ),
            "n/a*",
        )

    def test_yes_survives_surrounding_blindness(self) -> None:
        """One observed hit is a positive existence proof."""
        self.assertEqual(
            self._note(
                make_call(usage={"cache_read_input_tokens": 5}),
                make_call(usage=None, usage_provenance=UsageProvenance.ABSENT_BY_EXPORT),
            ),
            "yes",
        )
```

Ensure `tests/test_passive.py` imports `ParseResult`, `UsageProvenance`, and `render_report`.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_passive.py -k "UsageMissing or CacheNote" -q
```
Expected: FAIL — `AttributeError: 'ToolStats' object has no attribute 'usage_missing'`, and the `n/a` tests fail with `'no' != 'n/a'` (the bug, reproduced).

- [ ] **Step 3: Add the counter**

`toolbench/passive.py`, in `ToolStats`:

```python
@dataclass
class ToolStats:
    """Aggregate counters for one (agent, tool) pair — S14 tool leaderboard."""

    calls: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    errors: int = 0
    no_result: int = 0
    cache_hits: int = 0
    usage_missing: int = 0
```

Add to the `toolbench.transcript` import: `UsageProvenance`.

In `absorb`, directly below the existing `_is_cache_hit` block (`passive.py:129-131`):

```python
            if call.usage_provenance is not UsageProvenance.PRESENT:
                # Every flavour of absence means the same thing here: not measurable.
                # The arms differ for diagnostics, not for this flag.
                tool_stats.usage_missing += 1
                model_stats.usage_missing += 1
```

- [ ] **Step 4: Make the render rule total**

Replace `toolbench/passive.py:346`:

```python
        if stats.cache_hits > 0:
            cache_note = "yes"                        # a hit was observed; blindness elsewhere is irrelevant
        elif stats.usage_missing == 0:
            cache_note = "no"                         # measured, and it was zero
        elif stats.usage_missing == stats.calls:
            cache_note = "n/a"                        # never measurable
        else:
            cache_note = "n/a*"                       # partially measurable; some rows blind
```

After `lines.append("")` that closes the Tool Leaderboard table (currently `passive.py:351`), add the legend — `n/a*` otherwise reads as a footnote marker pointing nowhere:

```python
    lines.append(
        "`n/a` = usage channel unavailable for every call (S29); "
        "`n/a*` = unavailable for some. Neither is a measured zero. "
        "Per S19 this flag is caveat-only and never affects ranking."
    )
    lines.append("")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_passive.py -q && uv run pytest -q 2>&1 | tail -2
```
Expected: new tests pass; full suite `239 passed` (231 + 8 new).

- [ ] **Step 6: Confirm the real-world fix on the trace corpus**

```bash
uv run python - <<'PY'
from toolbench.adapters import detect_parser
from toolbench.passive import Reducer, render_report
p = "/Users/michellerojas/aphrodite-oracle/hermes-aphrodite-sessions-traces"
parser, stream = detect_parser(open(p).readlines())
print("parser:", type(parser).__name__)
r = parser.parse(stream, agent="claude-code", source="raw", project="aphrodite-oracle")
red = Reducer(); red.absorb("claude-code", r)
print(next(l for l in render_report(red).splitlines() if "| read_file |" in l))
PY
```
Expected: `parser: HermesTraceParser`, and the row ends `| n/a |` — not `| no |`. This is the bug, fixed, on the corpus that exposed it.

- [ ] **Step 7: Gate and commit**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q
git add toolbench/passive.py tests/test_passive.py
git commit -m "fix(passive): render n/a when the cache signal was never measurable

cache_note was 'yes' if cache_hits > 0 else 'no', so a corpus with no usage
channel reported a measured zero it never measured. On a hermes trace export
whose jsonl sibling records 2.4M cache tokens, it printed 'no'.

ToolStats gains a usage_missing counter rather than a provenance enum: trace
and real-transcript rows share an (agent, tool) bucket, and Reducer is
streaming (S11), so the mixed case must be representable. That case renders
n/a*, which a scalar could not express.

'yes' stays conditioned only on cache_hits > 0 -- one observed hit is a
positive existence proof."
```

---

## Task 4: Migrate probe fixtures to carry `requestId` (behaviour-preserving)

Preparatory for Task 5. **This task changes no production code and must not change a single test expectation.**

**Why it is needed.** `_turn_key`'s docstring says: *"Fixtures predating this discovery have no `requestId`; they fall back to the timestamp."* That is still true — five of six probe fixtures carry **zero** `requestId`. Task 5 deletes that fallback, so without this migration Task 5 breaks the probe suite wholesale.

**Why it is safe.** Every assistant record in those five fixtures has a **distinct** timestamp (verified: 4/4, 4/4, 2/2, 3/3, 6/6 distinct), so each already forms its own turn. Giving each assistant record a **unique** `requestId` reproduces the identical grouping. `probe_session_response_pooled.jsonl` already carries `requestId` (9/9) and tests genuine pooling — **do not touch it**. `sample.jsonl` is never read by the probe scanner and needs no change.

**Files:**
- Modify: `tests/fixtures/probe_session.jsonl`, `probe_session_contaminated.jsonl`, `probe_session_plugin_names.jsonl`, `probe_session_prose.jsonl`, `probe_session_real_schema.jsonl`
- Do **not** modify: `probe_session_response_pooled.jsonl`, `sample.jsonl`

**Interfaces:**
- Consumes: nothing.
- Produces: five fixtures whose every assistant record carries a unique `requestId`.

- [ ] **Step 1: Record the current probe test results as the invariant**

```bash
uv run pytest tests/test_probe.py -q 2>&1 | tail -2
```
Write the number down. It must be identical after Step 3.

- [ ] **Step 2: Apply the migration**

```bash
cd /Users/michellerojas/tool-benchmarks
uv run python - <<'PY'
import json, pathlib

targets = [
    "probe_session.jsonl",
    "probe_session_contaminated.jsonl",
    "probe_session_plugin_names.jsonl",
    "probe_session_prose.jsonl",
    "probe_session_real_schema.jsonl",
]
for name in targets:
    p = pathlib.Path("tests/fixtures") / name
    stem = name.removesuffix(".jsonl")
    out, n = [], 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            out.append(raw)
            continue
        e = json.loads(raw)
        msg = e.get("message")
        is_assistant = (
            isinstance(msg, dict)
            and e.get("type") != "user"
            and msg.get("role") != "user"
        )
        if is_assistant and "requestId" not in e:
            n += 1
            e["requestId"] = f"req_{stem}_{n:02d}"   # unique => one record per turn, as before
        out.append(json.dumps(e))
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{name}: stamped {n} assistant records")
PY
```
Expected: `4, 4, 2, 3, 6` records stamped respectively.

- [ ] **Step 3: Verify grouping is unchanged and no expectation moved**

```bash
uv run python - <<'PY'
from toolbench.probe import _scan_tool_use_blocks
for n in ["probe_session", "probe_session_contaminated", "probe_session_plugin_names",
          "probe_session_prose", "probe_session_real_schema"]:
    r = _scan_tool_use_blocks(f"tests/fixtures/{n}.jsonl")
    keys = list(r.turns)
    assert all(k.startswith("req:") for k in keys), (n, keys)
    print(f"{n:36s} turns={len(r.turns):2d} records={len(r.records):2d} all req: ✓")
PY
uv run pytest tests/test_probe.py -q 2>&1 | tail -2
```
Expected: every turn key now starts `req:`; the probe test count is **identical** to Step 1. Any change in pass count means the grouping moved — stop and investigate.

- [ ] **Step 4: Confirm the pooled fixture was not touched**

```bash
git diff --name-only tests/fixtures/
```
Expected: exactly the five files above. `probe_session_response_pooled.jsonl` and `sample.jsonl` must **not** appear.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q
git add tests/fixtures/probe_session.jsonl tests/fixtures/probe_session_contaminated.jsonl tests/fixtures/probe_session_plugin_names.jsonl tests/fixtures/probe_session_prose.jsonl tests/fixtures/probe_session_real_schema.jsonl
git commit -m "test(probe): give every assistant fixture record a requestId

Five probe fixtures carried no requestId and relied on _turn_key's timestamp
fallback, which the next commit deletes. Each of their assistant records already
had a distinct timestamp, so a unique requestId per record reproduces the exact
same one-record-per-turn grouping. No expectation changes.

probe_session_response_pooled.jsonl already carries requestId and exercises real
pooling; it is untouched."
```

---

## Task 5: `probe.py` refuses rather than degrades

Design §4.4. Acceptance S30.

`probe.py` measures per-turn token cost. TB-16 established `requestId` as the billing unit (S26). A corpus it cannot group does not yield a degraded result — it yields a **wrong** one. There is no partial mode.

**Two guards, deliberately redundant.** The door check names the format and suggests a fix. The invariant check is load-bearing: it defends S26 for *any* corpus lacking `requestId`, whatever parser claimed it. Gating the fallback behind a schema check alone would make the TB-16 regression conditional rather than gone.

**No import cycle** (verified): `probe → adapters → parsers → transcript`, and nothing under `toolbench/` imports `probe`.

**Files:**
- Modify: `toolbench/probe.py:13` (imports), `:139-151` (`_turn_key`), `:172-201` (`_scan_tool_use_blocks`), add `NonIsolableTurns`
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: `detect_parser` (Task 2), `HermesTraceParser.schema_tag` (Task 2).
- Produces: `NonIsolableTurns(RuntimeError)`; `_turn_key(entry: dict[str, object]) -> str` — **the `ts` parameter is removed**.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_probe.py`:

```python
class NonIsolableTurnsTests(unittest.TestCase):
    def test_it_is_a_runtime_error_like_its_siblings(self) -> None:
        self.assertTrue(issubclass(NonIsolableTurns, RuntimeError))

    def test_turn_key_returns_the_request_id(self) -> None:
        self.assertEqual(_turn_key({"requestId": "req_abc"}), "req:req_abc")

    def test_turn_key_raises_without_a_request_id(self) -> None:
        with self.assertRaises(NonIsolableTurns):
            _turn_key({"timestamp": "2026-07-10T00:00:00Z"})

    def test_turn_key_raises_on_an_empty_request_id(self) -> None:
        with self.assertRaises(NonIsolableTurns):
            _turn_key({"requestId": ""})

    def test_no_timestamp_fallback_survives_anywhere(self) -> None:
        """The `ts:` prefix was the TB-16 defect. It must not exist in the source."""
        source = Path(probe.__file__).read_text(encoding="utf-8")
        self.assertNotIn('f"ts:{', source)


class ProbeRefusesTraceInput(unittest.TestCase):
    FIXTURE = Path(__file__).parent / "fixtures" / "schema_hermes_trace.jsonl"

    def test_scan_refuses_a_trace_export_at_dispatch(self) -> None:
        with self.assertRaises(NonIsolableTurns) as ctx:
            _scan_tool_use_blocks(self.FIXTURE)
        self.assertIn("trace", str(ctx.exception).lower())

    def test_the_invariant_guard_is_not_shadowed_by_the_schema_check(self) -> None:
        """A claude-tagged corpus with requestId stripped must still raise.

        Proves the two guards are independent: the door check is diagnostics,
        the _turn_key check is the invariant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude_no_request_id.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "sessionId": "s1",
                        "type": "assistant",
                        "timestamp": "2026-07-10T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(NonIsolableTurns):
                _scan_tool_use_blocks(path)
```

Add the imports these need to `tests/test_probe.py`:

```python
import json
import tempfile
from pathlib import Path

from toolbench import probe
from toolbench.probe import NonIsolableTurns, _scan_tool_use_blocks, _turn_key
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_probe.py -k "NonIsolable or RefusesTrace" -q
```
Expected: FAIL — `ImportError: cannot import name 'NonIsolableTurns'`.

- [ ] **Step 3: Add the exception and remove the fallback**

In `toolbench/probe.py`, update the imports:

```python
from toolbench.adapters import detect_parser
from toolbench.parsers import HermesTraceParser
from toolbench.transcript import ToolCall, parse_session
```

Add below `SeededReportError`:

```python
class NonIsolableTurns(RuntimeError):
    """Raised when turns cannot be keyed to the billing unit (S26).

    A probe that cannot group by `requestId` does not produce an incomplete
    measurement -- it produces a confidently wrong one, by silently treating each
    JSONL record as its own API response. There is no useful degraded mode, so
    there is no partial-corpus path.
    """
```

Replace `_turn_key` entirely (`probe.py:139-151`):

```python
def _turn_key(entry: dict[str, object]) -> str:
    """The unit `output_tokens` is billed against: the API response (S26).

    Claude Code writes one API response as several JSONL entries -- `thinking`,
    `text`, and each `tool_use` -- sharing a `requestId` and a single `usage`
    figure, but carrying *distinct* timestamps. Grouping by timestamp therefore
    sees every response as a lone block, which is the TB-16 defect. There is no
    timestamp fallback: an entry that cannot be keyed to a response is refused.
    """
    request_id = entry.get("requestId")
    if not (isinstance(request_id, str) and request_id):
        raise NonIsolableTurns(
            "probe requires requestId to group turns by the billing unit (S26); "
            "this entry has none. hermes --format trace exports never carry it."
        )
    return f"req:{request_id}"
```

Update the sole caller (`probe.py:201`):

```python
            key = _turn_key(entry)
```

`ts_str` is still used by the `records` tuple below it, so leave its assignment in place.

- [ ] **Step 4: Route the scanner through dispatch**

In `_scan_tool_use_blocks`, replace the `with session_path.open(...)` header so detection runs first and the sniffed lines are replayed:

```python
    with session_path.open(encoding="utf-8") as handle:
        parser, replayed = detect_parser(handle)
        if parser.schema_tag == HermesTraceParser.schema_tag:
            raise NonIsolableTurns(
                "hermes --format trace carries no requestId, so turns cannot be "
                "keyed to the billing unit (S30). Trace exports are valid input to "
                "passive.py but not to probe.py. Use a native Claude transcript."
            )
        for raw_line in replayed:
```

The body below is unchanged. `detect_parser` chains the sniffed lines back onto the iterator, so the file is still read exactly once and no record is lost.

Extend the function's docstring with a second paragraph:

```
    Routes through `adapters.detect_parser` for one reason: to refuse a hermes
    trace export by name before it silently produces a plausible, wrong answer.
    The `_turn_key` guard below is the load-bearing check -- it defends S26 for any
    corpus, whatever parser claimed it -- but a refusal at the door can say why.
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_probe.py -q && uv run pytest -q 2>&1 | tail -2
```
Expected: new tests pass; full suite `246 passed` (239 + 7 new). If `tests/test_probe.py` shows *other* failures, Task 4's migration is incomplete — go back, do not weaken these guards.

- [ ] **Step 6: Confirm the refusal on the real trace export**

```bash
uv run python - <<'PY'
from toolbench.probe import NonIsolableTurns, _scan_tool_use_blocks
try:
    _scan_tool_use_blocks("/Users/michellerojas/aphrodite-oracle/hermes-aphrodite-sessions-traces")
except NonIsolableTurns as e:
    print("refused, as designed:", e)
else:
    raise SystemExit("BUG: probe accepted a trace export")
PY
```
Expected: `refused, as designed: hermes --format trace carries no requestId ...`

- [ ] **Step 7: Gate and commit**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q
git add toolbench/probe.py tests/test_probe.py
git commit -m "fix(probe): refuse corpora that cannot be keyed to the billing unit

_turn_key silently fell back to 'ts:{timestamp}' when requestId was absent --
the exact grouping TB-16 was filed to eliminate. On a hermes trace export
(0 requestId on every record) probe reported confidently wrong per-turn costs.

Two guards. _scan_tool_use_blocks now routes through detect_parser and refuses
hermes-trace by schema_tag, so the error names the format. _turn_key raises
NonIsolableTurns on any entry lacking requestId, so the invariant holds for any
corpus and the TB-16 regression is gone rather than made conditional.

The ts: fallback is removed, not guarded."
```

---

## Task 6: Documentation — S29, S30, and the corrected ticket claim

Design §5. DOCS phase.

**Files:**
- Modify: `SPEC.md` (append S29, S30 after S28 at line 178)
- Modify: `README.md` (`## Status` block)
- Test: none — this task is prose. The gate still runs.

**Interfaces:**
- Consumes: everything above.
- Produces: nothing consumed by code.

- [ ] **Step 1: Append the acceptance criteria to `SPEC.md`**

Verify S28 is still the highest ID before writing (acceptance IDs are append-only and shared across concurrent branches):

```bash
rg -n "^- \*\*S2[0-9]" SPEC.md | tail -2
```
If an `S29` already exists on another merged branch, use the next free pair and update this plan.

Append after S28:

```markdown
- **S29 — producer provenance for usage.** Schema and producer are separate axes.
  A transcript claimed by the claude schema is routed by producer: `version ==
  "hermes-agent"` selects `HermesTraceParser`, otherwise `ClaudeParser`. The two
  claim predicates partition, so `AmbiguousSchema` never fires between them. Every
  `ToolCall` carries a `UsageProvenance` of `PRESENT`, `ABSENT_BY_SCHEMA`,
  `ABSENT_BY_EXPORT`, or `ABSENT_UNEXPECTED`, stamped by its producer. The passive
  cache-hit flag renders `n/a` when no call in a bucket could be measured, `n/a*`
  when only some could, and `no` only when usage was available and zero hits were
  observed. Per S19 the flag remains caveat-only and never affects ranking.

- **S30 — probe requires the billing unit.** `probe.py` groups turns solely by
  `requestId` (S26). It rejects `hermes-trace` input at dispatch, and `_turn_key`
  raises `NonIsolableTurns` on any entry lacking `requestId`. There is no timestamp
  fallback and no partial-corpus mode. `hermes sessions export --format trace`
  output is therefore valid input to `passive.py` and invalid input to `probe.py`.
```

- [ ] **Step 2: Update the README status block**

Refresh the test count in `README.md`'s `## Status` section to the final number from Task 5 (`246 passed`, or whatever `uv run pytest -q` actually reports — quote it, do not guess). State the strict gate is green.

- [ ] **Step 3: Correct TB-18's overstated token claim on the lattice board**

The ticket says "`tokens` is NOT poisoned." The measurement (design §2.4) says redaction perturbs `output_chars` by 12 characters — 3 tokens across 270 calls, ~0.004%. Negligible, but **not zero**. Post the correction as a comment so the absolute claim does not outlive the evidence:

```bash
lattice comment TB-18 --actor agent:claude --body "Correction to the ticket body: '--format trace: tokens is NOT poisoned' overstates the evidence. Measured redacted vs --no-redact across 43 sessions / 270 calls: output_chars delta 12 => 3 tokens (~0.004%). Negligible but nonzero. The usage hazard is confirmed harder than filed: 0/270 calls carry usage in both trees. See docs/superpowers/specs/2026-07-09-hermes-trace-usage-provenance-design.md §2.4."
```

- [ ] **Step 4: Run the full gate one final time**

```bash
uv run ruff check . && uv run mypy --strict && uv run pytest -q 2>&1 | tail -2
```
Expected: clean, clean, `246 passed`. **Report the actual count.**

- [ ] **Step 5: Commit**

```bash
git add SPEC.md README.md
git commit -m "docs: add S29 (usage provenance) and S30 (probe billing unit)

Also corrects TB-18's absolute claim that trace redaction leaves tokens
unpoisoned: measured at 3 tokens across 270 calls. Negligible, not zero."
```

- [ ] **Step 6: Move TB-18 to review and push**

```bash
lattice status TB-18 review --actor agent:claude
git add .lattice/
git commit -m "Move TB-18 to review"
git push
/usr/local/bin/gh pr view 20 --json state,mergeable,mergeStateStatus,statusCheckRollup
```
`lattice status` requires `--actor` (or `--name`). GitHub computes `mergeable` lazily — if it returns `UNKNOWN`, re-query rather than reporting it.

---

## Self-Review

**Spec coverage.** Design §4.1 → Tasks 1, 2. §4.2 → Task 3 (counter). §4.3 → Task 3 (render). §4.4 → Task 5 (both guards). §5 → Task 6. §2.1's docstring correction → Task 1 Step 5. §2.4's ticket correction → Task 6 Step 3. §7's `isinstance` tax → Task 2 Step 5. §7's field-ordering constraint → Task 1 Step 3, asserted by `test_provenance_precedes_the_defaulted_fields`. §8 → Task 0 (promoted from out-of-scope; it fails the suite today).

**Gaps found and closed while writing this plan.**
1. **The design's test plan missed the probe fixtures.** Five of six carry no `requestId` and depend on the `ts:` fallback Task 5 deletes. Task 4 exists solely to migrate them, behaviour-preservingly, before that deletion. Without it Task 5 turns the probe suite red and the cause looks like the guard rather than the fixtures.
2. **The stated gate command was wrong.** `unittest` does not collect this suite.
3. **The stated baseline was wrong.** It is red, not green. Task 0 fixes the cause.
4. **`sqlite3.connect` is lazy**, so the §8 fix sketch (`try/except` around `connect`) would have caught nothing. Task 0 probes the connection.
5. **The design listed "positional construction breaks" as a risk.** Verified false — all four `ToolCall(` sites use keywords, and both test factories splat a dict. The real breakage is the *missing keyword*, handled in Task 1 Step 6.

**Type consistency.** `UsageProvenance` is defined in `toolbench.transcript` (Task 1) and imported by `parsers.py`, `hermes.py`, `passive.py`, and three test modules — never redefined. `_provenance(usage: object) -> UsageProvenance` has the same signature on `ClaudeParser` and `HermesTraceParser`. `usage_missing` is spelled identically in `ToolStats`, `absorb`, and the render rule. `_turn_key` loses its `ts` parameter in Task 5 and its only caller is updated in the same step. `NonIsolableTurns` is defined once and imported by tests.

**Placeholder scan.** No TBDs. Every code step carries the literal code. Every command carries its expected output. Expected test counts are cumulative and stated per task (217 → 225 → 231 → 239 → 246); if yours drift, report the real number rather than editing the plan to match.
