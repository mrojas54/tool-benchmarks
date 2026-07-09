# Transcript Schema Dispatch (TB-13) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `toolbench` a schema-dispatch seam so an unrecognized transcript raises `UnknownSchema` and lands in `skipped_roots`, instead of falling through to the Claude parser and reporting a healthy zero.

**Architecture:** Two orthogonal ABCs — `SessionLoader` (acquisition: raw file / AgentsView subprocess) and `TranscriptParser` (schema: claude) — composed behind a single `SessionAdapter.parse(ref) -> ParseResult`. `HermesAdapter` implements `SessionAdapter` directly from SQLite because it has no lines. `pick_adapter` walks an ordered registry of `claims(ref)` predicates; `ComposedAdapter` is the terminal fallback and content-sniffs to choose its parser over a bounded 100-line window.

**Tech Stack:** Python 3.13, stdlib only (no third-party imports in `toolbench/*`), `uv` for execution, `pytest`, `ruff`, `mypy --strict`.

Spec: `docs/superpowers/specs/2026-07-09-transcript-schema-dispatch-design.md`
Ticket: TB-13

## Global Constraints

- **Stdlib only** in every `toolbench/*` module. No new dependencies.
- Run everything through `uv run` (e.g. `uv run pytest`), never bare `pytest`.
- **S5 preserved:** malformed lines counted and skipped, never fatal. The per-line `json.JSONDecodeError` guard and `errors="replace"` decoding both stay.
- **S6 preserved:** the end-of-file `pending` drain emits `no_result=True` calls with `output_chars=0`. Never drop unmatched calls.
- **S1/S2 preserved:** Claude join and payload precedence — `tool_use_id` over `toolUseID`; block-local `content` over `toolUseResult`.
- `result_len`, `ToolCall`, `ParseResult` stay in `transcript.py`, unchanged. Reuse, never fork.
- The NUL sniff runs in the **loader**, before schema detection. A SQLite dump has no first JSON line.
- `UnknownSchema` and `AmbiguousSchema` subclass `RuntimeError` so `passive.main`'s existing guard at `passive.py:456` — `except (OSError, RuntimeError, UnicodeDecodeError)` — catches them with **no edit to that guard**.
- Strict TDD: separate `RED:`, `GREEN:`, and `DOCS:` commits per task.
- All 145 existing tests stay green. `uv run ruff check .` clean. `uv run mypy --strict toolbench` clean.

## Corrections to the spec, applied here

1. The spec quotes the guard as `except (OSError, RuntimeError)`. The real tuple is `(OSError, RuntimeError, UnicodeDecodeError)` at `passive.py:456`. The conclusion is unchanged.
2. The spec's acceptance criterion "byte-identical agent-breakdown rows vs the 2026-07-09 baseline" is **not implementable as a test**: `reports/` is gitignored (`.gitignore:31`) and the rows are computed from the live, growing archive. Replaced by committed golden fixtures (Task 6), plus a **manual** pre/post live run recorded in the PR body.
3. The spec's module list omits `registry.py`. It is required: `hermes.py` must import `SessionAdapter` from `adapters.py`, and the registry must import `HermesAdapter` — a cycle. `registry.py` imports both and exposes `pick_adapter`.

## File Structure

| File | Status | Responsibility |
| --- | --- | --- |
| `toolbench/transcript.py` | modify | `ToolCall`, `ParseResult`, `result_len` (unchanged). `parse_session` becomes a compat shim. |
| `toolbench/parsers.py` | **create** | `TranscriptParser` ABC + `ClaudeParser`. Schema only; knows nothing about acquisition. |
| `toolbench/adapters.py` | **create** | `SessionAdapter` ABC, `ComposedAdapter`, `UnknownSchema`, `AmbiguousSchema`, `detect_parser`, `PARSERS`. |
| `toolbench/sources.py` | modify | `SessionLoader` ABC, `RawFileLoader`, `AgentsViewLoader`. `open_session_jsonl` becomes a compat wrapper. |
| `toolbench/hermes.py` | modify | Add `HermesAdapter(SessionAdapter)`. `parse_hermes_session` unchanged. |
| `toolbench/registry.py` | **create** | `ADAPTERS` ordered list + `pick_adapter(ref, runner)`. Breaks the import cycle. |
| `toolbench/passive.py` | modify | `_parse_ref` reduced to `pick_adapter(ref, runner).parse(ref)`. `NamedTemporaryFile` deleted. |
| `tests/test_parsers.py` | **create** | `ClaudeParser` over `Iterable[str]`. |
| `tests/test_adapters.py` | **create** | `detect_parser`, `UnknownSchema`, `AmbiguousSchema`, `ComposedAdapter`. |
| `tests/test_registry.py` | **create** | `pick_adapter` ordering; hermes claims before the fallback. |
| `tests/fixtures/schema_*.jsonl` | **create** | One fixture per observed line-0 shape + a golden claude session. |

---

### Task 1: `ClaudeParser` over lines, `parse_session` as compat shim

**Files:**
- Create: `toolbench/parsers.py`
- Modify: `toolbench/transcript.py:111-235` (replace `parse_session` body with a shim)
- Test: `tests/test_parsers.py`

**Interfaces:**
- Consumes: `ToolCall`, `ParseResult`, `result_len` from `toolbench.transcript` (unchanged).
- Produces:
  - `TranscriptParser` ABC with `schema_tag: ClassVar[str]`, `claims_line(cls, entry: dict[str, object]) -> bool`, `parse(self, lines: Iterable[str], *, agent: str, source: str, project: str) -> ParseResult`
  - `ClaudeParser(TranscriptParser)` with `schema_tag = "claude"`
  - `transcript.parse_session(path, *, agent, source, project=None) -> ParseResult` (compat shim, unchanged signature and defaults)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parsers.py
import pytest
from toolbench.parsers import ClaudeParser, TranscriptParser


def test_claude_parser_claims_a_line_carrying_session_id():
    assert ClaudeParser.claims_line({"type": "last-prompt", "sessionId": "s1"}) is True


def test_claude_parser_does_not_claim_a_codex_line():
    assert ClaudeParser.claims_line({"type": "session_meta", "payload": {}}) is False


def test_claude_parser_joins_tool_use_to_tool_result_from_lines():
    lines = [
        '{"sessionId":"s1","timestamp":"t0","message":{"model":"m","content":'
        '[{"type":"tool_use","id":"u1","name":"Bash","input":{"command":"ls"}}]}}\n',
        '{"sessionId":"s1","timestamp":"t1","message":{"content":'
        '[{"type":"tool_result","tool_use_id":"u1","content":"hello"}]}}\n',
    ]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.name == "Bash"
    assert call.output_chars == 5          # len("hello")
    assert call.no_result is False
    assert call.result_source == "block_local"
    assert result.malformed == 0


def test_claude_parser_drains_unmatched_call_at_eof():
    lines = [
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Read","input":{}}]}}\n',
    ]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert len(result.calls) == 1
    assert result.calls[0].no_result is True
    assert result.calls[0].output_chars == 0


def test_claude_parser_counts_malformed_lines_without_raising():
    lines = ['{"sessionId":"s1"}\n', "not json\n", "\n"]
    result = ClaudeParser().parse(lines, agent="claude", source="raw", project="p")
    assert result.malformed == 1
    assert result.calls == []


def test_claude_parser_is_a_transcript_parser():
    assert issubclass(ClaudeParser, TranscriptParser)
    assert ClaudeParser.schema_tag == "claude"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_parsers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'toolbench.parsers'`

- [ ] **Step 3: Commit the RED phase**

```bash
git add tests/test_parsers.py
git commit -m "RED: ClaudeParser parses an Iterable[str], not a path"
```

- [ ] **Step 4: Write `toolbench/parsers.py`**

Move the body of today's `parse_session` verbatim, changing only its input: it iterates `lines` instead of opening a file, and takes `project` as a required `str`.

```python
"""Schema parsers (TB-13). One class per transcript schema. Stdlib only.

A parser interprets already-acquired lines. It never opens a file, never shells
out, and never decides which schema it is looking at -- `adapters.detect_parser`
does that by asking each parser's `claims_line`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from toolbench.transcript import ParseResult, ToolCall, result_len


@dataclass
class _PendingCall:
    name: str
    input_chars: int
    session_id: str
    ts: str
    usage: dict[str, object] | None
    model: str | None


class TranscriptParser(ABC):
    """Interpretation. Knows nothing about acquisition."""

    schema_tag: ClassVar[str]

    @classmethod
    @abstractmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        """True if `entry` is discriminating evidence for this schema."""

    @abstractmethod
    def parse(
        self, lines: Iterable[str], *, agent: str, source: str, project: str
    ) -> ParseResult: ...


def _result_id(entry: dict[str, object], block: dict[str, object] | None) -> str | None:
    """Join key (S1): block-local `tool_use_id` first, else top-level `toolUseID`."""
    if block is not None:
        block_id = block.get("tool_use_id")
        if isinstance(block_id, str):
            return block_id
    top_level_id = entry.get("toolUseID")
    return top_level_id if isinstance(top_level_id, str) else None


def _result_payload(
    entry: dict[str, object], block: dict[str, object] | None
) -> tuple[object, str | None]:
    """Payload resolution (S2): block-local `content` wins over top-level `toolUseResult`."""
    if block is not None and "content" in block:
        return block["content"], "block_local"
    if "toolUseResult" in entry:
        return entry["toolUseResult"], "top_level"
    return None, None


class ClaudeParser(TranscriptParser):
    """Claude Code / cowork JSONL: `tool_use` blocks joined to `tool_result` by id.

    `cowork` emits this schema too. That is why detection is by payload, not by
    producer: one parser, two agents, zero registry entries for either name.
    """

    schema_tag: ClassVar[str] = "claude"

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        # Every claude/cowork control and message record carries `sessionId`.
        # `tool_use` itself is NOT a usable discriminator: line 0 is a
        # `last-prompt` / `mode` record, and a session that used no tools has
        # no `tool_use` block anywhere.
        return "sessionId" in entry

    def parse(
        self, lines: Iterable[str], *, agent: str, source: str, project: str
    ) -> ParseResult:
        pending: dict[str, _PendingCall] = {}
        calls: list[ToolCall] = []
        malformed = 0

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(entry, dict):
                malformed += 1
                continue

            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            session_id = entry.get("sessionId")
            ts = entry.get("timestamp")
            session_id_str = session_id if isinstance(session_id, str) else ""
            ts_str = ts if isinstance(ts, str) else ""

            if isinstance(content, list):
                for tool_use_block in content:
                    if not isinstance(tool_use_block, dict):
                        continue
                    if tool_use_block.get("type") != "tool_use":
                        continue
                    tool_use_id = tool_use_block.get("id")
                    name = tool_use_block.get("name")
                    if not isinstance(tool_use_id, str) or not isinstance(name, str):
                        continue
                    usage = message.get("usage") if isinstance(message, dict) else None
                    model = message.get("model") if isinstance(message, dict) else None
                    pending[tool_use_id] = _PendingCall(
                        name=name,
                        input_chars=result_len(tool_use_block.get("input")),
                        session_id=session_id_str,
                        ts=ts_str,
                        usage=usage if isinstance(usage, dict) else None,
                        model=model if isinstance(model, str) else None,
                    )

            result_blocks: list[dict[str, object] | None] = []
            if isinstance(content, list):
                result_blocks = [
                    block
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
            if not result_blocks and "toolUseID" in entry:
                result_blocks = [None]

            for result_block in result_blocks:
                result_id = _result_id(entry, result_block)
                if result_id is None or result_id not in pending:
                    continue
                payload, payload_source = _result_payload(entry, result_block)
                pending_call = pending.pop(result_id)
                error = None
                if isinstance(result_block, dict) and result_block.get("is_error"):
                    error = "tool_error"
                calls.append(
                    ToolCall(
                        agent=agent,
                        source=source,
                        project=project,
                        name=pending_call.name,
                        input_chars=pending_call.input_chars,
                        output_chars=result_len(payload),
                        session_id=pending_call.session_id,
                        ts=pending_call.ts,
                        usage=pending_call.usage,
                        duration_ms=None,
                        error=error,
                        model=pending_call.model,
                        result_source=payload_source,
                    )
                )

        # S6: an unmatched `tool_use` at EOF is kept, never dropped.
        for pending_call in pending.values():
            calls.append(
                ToolCall(
                    agent=agent,
                    source=source,
                    project=project,
                    name=pending_call.name,
                    input_chars=pending_call.input_chars,
                    output_chars=0,
                    session_id=pending_call.session_id,
                    ts=pending_call.ts,
                    usage=pending_call.usage,
                    duration_ms=None,
                    error=None,
                    model=pending_call.model,
                    no_result=True,
                    result_source=None,
                )
            )

        return ParseResult(calls=calls, malformed=malformed)
```

- [ ] **Step 5: Replace `parse_session` in `transcript.py` with a compat shim**

Delete `_PendingCall`, `_result_id`, `_result_payload`, and the body of `parse_session` from `transcript.py` (they now live in `parsers.py`). Keep `result_len`, `_block_list_len`, `ToolCall`, `ParseResult` exactly as they are. Replace `parse_session` with:

```python
def parse_session(
    path: str | os.PathLike[str],
    *,
    agent: str = "claude-code",
    source: str = "raw",
    project: str | None = None,
) -> ParseResult:
    """Deprecated: parse a Claude Code session JSONL by path.

    Retained because it is the documented entry point and `probe.py` imports it.
    A `TranscriptParser` consumes an `Iterable[str]` and so cannot derive
    `project` from a path; this shim resolves it before delegating, preserving
    the historical `project=None -> path.parent.name` default.

    Prefer `registry.pick_adapter(ref).parse(ref)`, which detects the schema
    instead of assuming Claude's.
    """
    from toolbench.parsers import ClaudeParser  # local: avoids an import cycle

    session_path = Path(path)
    resolved_project = project if project is not None else session_path.parent.name
    with session_path.open(encoding="utf-8", errors="replace") as handle:
        return ClaudeParser().parse(
            handle, agent=agent, source=source, project=resolved_project
        )
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS — 145 existing + 6 new. `tests/test_transcript.py` must pass untouched; that is the proof the shim preserved behavior.

- [ ] **Step 7: Lint and typecheck**

Run: `uv run ruff check . && uv run mypy --strict toolbench`
Expected: no output, exit 0.

- [ ] **Step 8: Commit the GREEN phase**

```bash
git add toolbench/parsers.py toolbench/transcript.py
git commit -m "GREEN: extract ClaudeParser; parse_session becomes a compat shim"
```

---

### Task 2: `detect_parser` and the schema errors

**Files:**
- Create: `toolbench/adapters.py`
- Test: `tests/test_adapters.py`

**Interfaces:**
- Consumes: `TranscriptParser`, `ClaudeParser` from `toolbench.parsers`.
- Produces:
  - `UnknownSchema(RuntimeError)`, `AmbiguousSchema(RuntimeError)`
  - `PARSERS: tuple[type[TranscriptParser], ...]` — currently `(ClaudeParser,)`
  - `DETECT_WINDOW: int = 100`
  - `detect_parser(lines: Iterator[str], *, window: int = DETECT_WINDOW) -> tuple[TranscriptParser, Iterator[str]]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapters.py
import pytest
from toolbench.adapters import (
    AmbiguousSchema,
    UnknownSchema,
    detect_parser,
)
from toolbench.parsers import ClaudeParser


def test_detect_returns_claude_parser_and_replays_every_line():
    lines = iter(['{"sessionId":"s1","type":"last-prompt"}\n', '{"sessionId":"s1"}\n'])
    parser, replayed = detect_parser(lines)
    assert isinstance(parser, ClaudeParser)
    assert len(list(replayed)) == 2          # the sniffed line is chained back


def test_detect_skips_preamble_before_the_discriminating_line():
    lines = iter(["\n", "not json\n", '{"unknown":1}\n', '{"sessionId":"s1"}\n'])
    parser, replayed = detect_parser(lines)
    assert isinstance(parser, ClaudeParser)
    assert len(list(replayed)) == 4          # nothing is consumed away


def test_detect_raises_unknown_schema_on_a_codex_line():
    lines = iter(['{"type":"session_meta","payload":{},"timestamp":"t"}\n'])
    with pytest.raises(UnknownSchema):
        detect_parser(lines)


def test_detect_raises_unknown_schema_on_a_cursor_line():
    lines = iter(['{"role":"user","message":{}}\n'])
    with pytest.raises(UnknownSchema):
        detect_parser(lines)


def test_detect_is_bounded_and_does_not_read_past_the_window():
    lines = iter(['{"filler":1}\n'] * 500)
    with pytest.raises(UnknownSchema):
        detect_parser(lines, window=100)


def test_detect_raises_unknown_schema_on_empty_input():
    with pytest.raises(UnknownSchema):
        detect_parser(iter([]))


def test_detect_raises_ambiguous_when_two_parsers_claim_one_line(monkeypatch):
    class Greedy(ClaudeParser):
        schema_tag = "greedy"

    monkeypatch.setattr("toolbench.adapters.PARSERS", (ClaudeParser, Greedy))
    with pytest.raises(AmbiguousSchema):
        detect_parser(iter(['{"sessionId":"s1"}\n']))


def test_unknown_and_ambiguous_are_runtime_errors():
    # passive.main's guard catches RuntimeError; this is why no guard edit is needed.
    assert issubclass(UnknownSchema, RuntimeError)
    assert issubclass(AmbiguousSchema, RuntimeError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_adapters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'toolbench.adapters'`

- [ ] **Step 3: Commit the RED phase**

```bash
git add tests/test_adapters.py
git commit -m "RED: detect_parser over a bounded window; unknown schemas raise"
```

- [ ] **Step 4: Write `toolbench/adapters.py` (detection half)**

```python
"""Schema dispatch (TB-13). Stdlib only.

`parse_session` used to be the unnamed default for every transcript that was not
hermes. A codex session matched nothing inside it and returned
`ParseResult(calls=[], malformed=0)` -- a healthy-looking zero (TB-12). A parser
that cannot recognize a schema must not be the fallback for schemas it has never
seen, so detection is explicit and failure is loud.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import chain

from toolbench.parsers import ClaudeParser, TranscriptParser

# Ordered by nothing in particular: detection asserts exactly one parser claims a
# line, so order cannot silently decide a tie. `CodexParser` joins here in TB-12.
PARSERS: tuple[type[TranscriptParser], ...] = (ClaudeParser,)

# Transcripts open with control/metadata preamble, so the discriminating record is
# not always line 0. Measured max depth across 40 sessions (10 per agent) is 0; the
# window is insurance against unseen preamble, and it bounds the read on a blob that
# no parser will ever claim.
DETECT_WINDOW = 100


class UnknownSchema(RuntimeError):
    """No registered parser claimed any line in the detection window.

    Subclasses RuntimeError so `passive.main` demotes the session to
    `skipped_roots` via its existing per-session guard. The agent is then named
    in the Summary rather than reported as an agent that did no tool work.
    """


class AmbiguousSchema(RuntimeError):
    """Two parsers claimed the same line. A programming error, not a data error."""


def detect_parser(
    lines: Iterator[str], *, window: int = DETECT_WINDOW
) -> tuple[TranscriptParser, Iterator[str]]:
    """Sniff up to `window` non-empty lines; return (parser, all lines replayed).

    Consumed lines are chained back onto the iterator, so the transcript is read
    exactly once even though detection looks at its head. Undecodable lines inside
    the window are skipped and NOT counted -- malformed accounting is the parser's
    job (S5), and counting here would charge a session twice.
    """
    buffered: list[str] = []
    seen = 0
    for raw_line in lines:
        buffered.append(raw_line)
        line = raw_line.strip()
        if not line:
            continue
        if seen >= window:
            break
        seen += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        claimed = [p for p in PARSERS if p.claims_line(entry)]
        if len(claimed) > 1:
            tags = ", ".join(p.schema_tag for p in claimed)
            raise AmbiguousSchema(f"line claimed by multiple parsers: {tags}")
        if claimed:
            return claimed[0](), chain(buffered, lines)

    raise UnknownSchema(
        f"no registered parser claimed any of the first {seen} decodable lines"
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_adapters.py -v`
Expected: PASS — 8 tests.

- [ ] **Step 6: Lint and typecheck, then commit**

```bash
uv run ruff check . && uv run mypy --strict toolbench
git add toolbench/adapters.py
git commit -m "GREEN: detect_parser sniffs a bounded window; unknown schemas raise"
```

---

### Task 3: `SessionLoader`, `RawFileLoader`, `AgentsViewLoader`

**Files:**
- Modify: `toolbench/sources.py:112-134` (`open_session_jsonl` becomes a wrapper)
- Test: `tests/test_sources.py` (append)

**Interfaces:**
- Consumes: `SessionRef`, `Runner`, `NonTranscriptExport`, `SNIFF_LEN`, `path_looks_binary`, `_looks_binary`, `_run_agentsview` — all already in `sources.py`.
- Produces:
  - `SessionLoader` ABC with `lines(self, ref: SessionRef) -> Iterator[str]`
  - `RawFileLoader(SessionLoader)`
  - `AgentsViewLoader(SessionLoader)` — `__init__(self, runner: Runner = _run_agentsview)`
  - `open_session_jsonl(ref, runner=_run_agentsview) -> Iterator[str]` (unchanged signature; now delegates)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sources.py  (append)
import subprocess
import pytest
from toolbench.sources import (
    AgentsViewLoader,
    NonTranscriptExport,
    RawFileLoader,
    SessionLoader,
    SessionRef,
)


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_raw_file_loader_yields_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="s", path=str(p))
    assert list(RawFileLoader().lines(ref)) == ['{"a":1}\n', '{"b":2}\n']


def test_raw_file_loader_rejects_binary_before_any_parse(tmp_path):
    p = tmp_path / "s.db"
    p.write_bytes(b"SQLite format 3\x00rest")
    ref = SessionRef(agent="hermes", source="raw", project="p", session_id="s", path=str(p))
    with pytest.raises(NonTranscriptExport):
        list(RawFileLoader().lines(ref))


def test_raw_file_loader_decodes_leniently(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b'{"a":"\xa0"}\n')          # stray non-UTF-8 byte
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="s", path=str(p))
    assert list(RawFileLoader().lines(ref)) == ['{"a":"�"}\n']


def test_agentsview_loader_yields_lines():
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="c:1", path=None)
    loader = AgentsViewLoader(runner=lambda argv: _ok('{"a":1}\n{"b":2}\n'))
    assert list(loader.lines(ref)) == ['{"a":1}\n', '{"b":2}\n']


def test_agentsview_loader_rejects_binary_payload():
    ref = SessionRef(agent="hermes", source="agentsview", project="p", session_id="h:1", path=None)
    loader = AgentsViewLoader(runner=lambda argv: _ok("SQLite format 3\x00junk"))
    with pytest.raises(NonTranscriptExport):
        list(loader.lines(ref))


def test_agentsview_loader_raises_on_nonzero_returncode():
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="c:1", path=None)
    bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    loader = AgentsViewLoader(runner=lambda argv: bad)
    with pytest.raises(RuntimeError, match="boom"):
        list(loader.lines(ref))


def test_loaders_are_session_loaders():
    assert issubclass(RawFileLoader, SessionLoader)
    assert issubclass(AgentsViewLoader, SessionLoader)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sources.py -v -k loader`
Expected: FAIL — `ImportError: cannot import name 'RawFileLoader' from 'toolbench.sources'`

- [ ] **Step 3: Commit the RED phase**

```bash
git add tests/test_sources.py
git commit -m "RED: SessionLoader splits acquisition out of open_session_jsonl"
```

- [ ] **Step 4: Add the loaders to `sources.py`**

Insert after `path_looks_binary`. Add `from abc import ABC, abstractmethod` to the imports.

```python
class SessionLoader(ABC):
    """Acquisition. Knows nothing about schemas.

    The NUL sniff lives here, and therefore runs before schema detection -- a
    SQLite dump has no first JSON line to detect (TB-11).
    """

    @abstractmethod
    def lines(self, ref: SessionRef) -> Iterator[str]: ...


class RawFileLoader(SessionLoader):
    """A session already on disk."""

    def lines(self, ref: SessionRef) -> Iterator[str]:
        assert ref.path is not None, "RawFileLoader requires ref.path"
        # Sniff on a separate binary handle so the text handle can still stream
        # line-by-line; slurping a head as text would force us to stitch a
        # mid-line cut back together.
        if path_looks_binary(ref.path):
            raise NonTranscriptExport(f"non-transcript payload (binary content): {ref.path}")
        with open(ref.path, encoding="utf-8", errors="replace") as f:
            yield from f


class AgentsViewLoader(SessionLoader):
    """A session fetched through `agentsview session export`."""

    def __init__(self, runner: Runner = _run_agentsview) -> None:
        self._runner = runner

    def lines(self, ref: SessionRef) -> Iterator[str]:
        result = self._runner(["agentsview", "session", "export", ref.session_id])
        if result.returncode != 0:
            raise RuntimeError(
                f"agentsview session export failed ({result.returncode}): {result.stderr.strip()}"
            )
        if _looks_binary(result.stdout[:SNIFF_LEN]):
            # No session id here: callers that record this already prefix it.
            raise NonTranscriptExport("non-transcript payload (binary content) from session export")
        yield from result.stdout.splitlines(keepends=True)
```

Then replace `open_session_jsonl`'s body:

```python
def open_session_jsonl(
    ref: SessionRef,
    runner: Runner = _run_agentsview,
) -> Iterator[str]:
    """Stream JSONL lines for `ref` from disk or via `agentsview session export` (S9).

    Retained as the documented entry point; the branch it used to own is now
    `RawFileLoader` / `AgentsViewLoader`.
    """
    loader: SessionLoader = RawFileLoader() if ref.path is not None else AgentsViewLoader(runner)
    yield from loader.lines(ref)
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS. Existing `test_sources.py` tests for `open_session_jsonl` must pass untouched — proof the wrapper preserved behavior.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check . && uv run mypy --strict toolbench
git add toolbench/sources.py
git commit -m "GREEN: SessionLoader ABC; open_session_jsonl delegates to it"
```

---

### Task 4: `SessionAdapter`, `ComposedAdapter`, `HermesAdapter`, `pick_adapter`

**Files:**
- Modify: `toolbench/adapters.py` (append the adapter half)
- Modify: `toolbench/hermes.py` (append `HermesAdapter`)
- Create: `toolbench/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `detect_parser` (Task 2); `RawFileLoader`, `AgentsViewLoader` (Task 3); `parse_hermes_session` (existing).
- Produces:
  - `adapters.SessionAdapter` ABC — `claims(self, ref) -> bool`, `parse(self, ref) -> ParseResult`
  - `adapters.ComposedAdapter(SessionAdapter)` — `__init__(self, runner: Runner | None = None)`
  - `hermes.HermesAdapter(SessionAdapter)`
  - `registry.ADAPTERS: tuple[SessionAdapter, ...]`
  - `registry.pick_adapter(ref: SessionRef, runner: Runner | None = None) -> SessionAdapter`

**Why `registry.py` exists:** `hermes.py` imports `SessionAdapter` from `adapters.py`; a registry naming `HermesAdapter` would make `adapters.py` import `hermes.py` — a cycle. `registry.py` imports both and is imported by neither.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry.py
import subprocess
import pytest
from toolbench.adapters import ComposedAdapter, UnknownSchema
from toolbench.hermes import HermesAdapter
from toolbench.registry import pick_adapter
from toolbench.sources import SessionRef


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_hermes_ref_picks_the_hermes_adapter():
    ref = SessionRef(agent="hermes", source="agentsview", project="h", session_id="hermes:1", path=None)
    assert isinstance(pick_adapter(ref), HermesAdapter)


def test_hermes_with_a_path_is_not_claimed_by_hermes_adapter(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"sessionId":"s1"}\n', encoding="utf-8")
    ref = SessionRef(agent="hermes", source="raw", project="h", session_id="s1", path=str(p))
    assert isinstance(pick_adapter(ref), ComposedAdapter)


def test_claude_ref_picks_the_composed_adapter():
    ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="c:1", path=None)
    assert isinstance(pick_adapter(ref), ComposedAdapter)


def test_composed_adapter_parses_a_raw_claude_session(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Bash","input":{}}]}}\n'
        '{"sessionId":"s1","timestamp":"t1","message":{"content":'
        '[{"type":"tool_result","tool_use_id":"u1","content":"ok"}]}}\n',
        encoding="utf-8",
    )
    ref = SessionRef(agent="claude", source="raw", project="p", session_id="s1", path=str(p))
    result = pick_adapter(ref).parse(ref)
    assert len(result.calls) == 1
    assert result.calls[0].name == "Bash"
    assert result.calls[0].agent == "claude"      # ref fields flow through
    assert result.calls[0].project == "p"


def test_composed_adapter_raises_unknown_schema_for_codex():
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="codex:1", path=None)
    adapter = ComposedAdapter(runner=lambda argv: _ok('{"type":"session_meta","payload":{}}\n'))
    with pytest.raises(UnknownSchema):
        adapter.parse(ref)


def test_unknown_schema_is_a_runtime_error_so_passive_demotes_it():
    assert issubclass(UnknownSchema, RuntimeError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'toolbench.registry'`

- [ ] **Step 3: Commit the RED phase**

```bash
git add tests/test_registry.py
git commit -m "RED: pick_adapter routes hermes by source, everything else by content"
```

- [ ] **Step 4: Append the adapter half to `toolbench/adapters.py`**

Add these imports at the top: `from abc import ABC, abstractmethod`, `from toolbench.sources import AgentsViewLoader, RawFileLoader, Runner, SessionLoader, SessionRef`, `from toolbench.transcript import ParseResult`.

```python
class SessionAdapter(ABC):
    """The single seam `passive.py` sees: a SessionRef becomes a ParseResult."""

    @abstractmethod
    def claims(self, ref: SessionRef) -> bool:
        """True if this adapter is responsible for `ref`."""

    @abstractmethod
    def parse(self, ref: SessionRef) -> ParseResult: ...


class ComposedAdapter(SessionAdapter):
    """A loader and a content-detected parser, composed. The terminal fallback.

    `claims` is unconditionally True: this adapter is last in the registry, and a
    ref it cannot handle surfaces as `UnknownSchema` from `detect_parser` rather
    than as a silent zero.
    """

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def claims(self, ref: SessionRef) -> bool:
        return True

    def _loader(self, ref: SessionRef) -> SessionLoader:
        if ref.path is not None:
            return RawFileLoader()
        return AgentsViewLoader(self._runner) if self._runner else AgentsViewLoader()

    def parse(self, ref: SessionRef) -> ParseResult:
        lines = self._loader(ref).lines(ref)
        parser, replayed = detect_parser(lines)
        return parser.parse(replayed, agent=ref.agent, source=ref.source, project=ref.project)
```

- [ ] **Step 5: Append `HermesAdapter` to `toolbench/hermes.py`**

```python
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
```

Add to `hermes.py` imports: `from toolbench.adapters import SessionAdapter` and `from toolbench.sources import SessionRef`.

- [ ] **Step 6: Create `toolbench/registry.py`**

```python
"""Adapter registry (TB-13). Stdlib only.

Exists to break an import cycle: `hermes.py` imports `SessionAdapter` from
`adapters.py`, so `adapters.py` cannot import `HermesAdapter`. This module imports
both and is imported by neither.

Order is significant. Source-keyed adapters get first refusal; `ComposedAdapter`
is the terminal fallback and claims everything. Adding an agent means adding an
entry here, never editing a dispatcher.
"""

from __future__ import annotations

from toolbench.adapters import ComposedAdapter, SessionAdapter
from toolbench.hermes import HermesAdapter
from toolbench.sources import Runner, SessionRef


def pick_adapter(ref: SessionRef, runner: Runner | None = None) -> SessionAdapter:
    """Return the first adapter that claims `ref`. Never returns None."""
    adapters: tuple[SessionAdapter, ...] = (HermesAdapter(), ComposedAdapter(runner))
    for adapter in adapters:
        if adapter.claims(ref):
            return adapter
    raise AssertionError("ComposedAdapter claims everything; this is unreachable")
```

- [ ] **Step 7: Run tests, lint, typecheck, commit**

```bash
uv run pytest tests/ -v
uv run ruff check . && uv run mypy --strict toolbench
git add toolbench/adapters.py toolbench/hermes.py toolbench/registry.py
git commit -m "GREEN: SessionAdapter seam; hermes claims by source, rest by content"
```

Expected: PASS, 6 new tests in `test_registry.py`.

---

### Task 5: Rewire `passive.py`, delete the temp file

**Files:**
- Modify: `toolbench/passive.py:18-29` (imports), `toolbench/passive.py:309-336` (`_parse_ref`)
- Test: `tests/test_passive.py` (append)

**Interfaces:**
- Consumes: `registry.pick_adapter`.
- Produces: `_parse_ref(ref: SessionRef, runner: Runner | None) -> ParseResult` — same signature, new body.

**Context:** `_parse_ref` currently writes the AgentsView generator to a `NamedTemporaryFile` and reopens it, because `parse_session` demanded a path. It needed a *rewind*, not buffering — and `_run_agentsview` already buffers the whole export into memory via `subprocess.run(capture_output=True)`. `detect_parser`'s `itertools.chain` supplies the one line of lookahead that was actually required.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_passive.py  (append)
import subprocess
import pytest
from toolbench.adapters import UnknownSchema
from toolbench.passive import Reducer, _parse_ref
from toolbench.sources import SessionRef


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_parse_ref_parses_an_agentsview_claude_session_without_a_temp_file():
    ref = SessionRef(agent="claude", source="agentsview", project="p", session_id="c:1", path=None)
    body = (
        '{"sessionId":"s1","timestamp":"t0","message":{"content":'
        '[{"type":"tool_use","id":"u1","name":"Grep","input":{}}]}}\n'
        '{"sessionId":"s1","timestamp":"t1","message":{"content":'
        '[{"type":"tool_result","tool_use_id":"u1","content":"hit"}]}}\n'
    )
    result = _parse_ref(ref, runner=lambda argv: _ok(body))
    assert len(result.calls) == 1
    assert result.calls[0].name == "Grep"


def test_parse_ref_raises_unknown_schema_for_codex_instead_of_returning_zero():
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="codex:1", path=None)
    body = '{"type":"session_meta","payload":{},"timestamp":"t"}\n'
    with pytest.raises(UnknownSchema):
        _parse_ref(ref, runner=lambda argv: _ok(body))


def test_passive_no_longer_imports_tempfile():
    import toolbench.passive as p
    assert not hasattr(p, "tempfile"), "the NamedTemporaryFile round-trip must be gone"


def test_unknown_schema_lands_in_skipped_roots_not_as_a_zero_row():
    # UnknownSchema is a RuntimeError, so main()'s existing guard demotes it.
    ref = SessionRef(agent="codex", source="agentsview", project="p", session_id="codex:1", path=None)
    reducer = Reducer()
    skipped: list[str] = []
    try:
        _parse_ref(ref, runner=lambda argv: _ok('{"role":"user","message":{}}\n'))
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        skipped.append(str(exc))
    assert skipped, "an unparseable session must be skipped, never counted as 0 calls"
    assert reducer.calls_joined == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_passive.py -v -k "unknown_schema or temp_file"`
Expected: FAIL — `test_passive_no_longer_imports_tempfile` fails (module still imports `tempfile`); the codex test fails because `_parse_ref` returns an empty `ParseResult` instead of raising.

> This failure **is** TB-12 reproduced in a unit test. Confirm you see the empty-result behavior before fixing it.

- [ ] **Step 3: Commit the RED phase**

```bash
git add tests/test_passive.py
git commit -m "RED: _parse_ref must raise UnknownSchema, not return a healthy zero"
```

- [ ] **Step 4: Replace `_parse_ref` and prune imports**

Delete `import os`, `import tempfile` (verify `os` is unused elsewhere in the module first with `grep -n 'os\.' toolbench/passive.py`), and drop `path_looks_binary`, `open_session_jsonl`, `parse_session`, `parse_hermes_session`, `NonTranscriptExport` from the import block if now unused. Add `from toolbench.registry import pick_adapter`.

```python
def _parse_ref(ref: SessionRef, runner: Runner | None) -> ParseResult:
    """Uniformly parse any session (S11 wiring).

    Every branch this function used to own now lives in the registry: hermes
    claims by source, everything else is content-detected. An unrecognized
    schema raises `UnknownSchema` (a RuntimeError), which `main`'s per-session
    guard demotes to `skipped_roots` -- so an unparseable agent is named in the
    Summary instead of reported as an agent that did no tool work (TB-12).
    """
    return pick_adapter(ref, runner).parse(ref)
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS. Every pre-existing `test_passive.py` test must still pass.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check . && uv run mypy --strict toolbench
git add toolbench/passive.py
git commit -m "GREEN: _parse_ref delegates to pick_adapter; temp-file round-trip deleted"
```

---

### Task 6: Golden fixtures and the line-0 shape pin

**Files:**
- Create: `tests/fixtures/schema_claude.jsonl`, `tests/fixtures/schema_cowork.jsonl`, `tests/fixtures/schema_codex.jsonl`, `tests/fixtures/schema_cursor.jsonl`
- Test: `tests/test_adapters.py` (append)

**Interfaces:**
- Consumes: `detect_parser`, `UnknownSchema`, `ClaudeParser`.
- Produces: nothing importable — regression coverage only.

**Why fixtures and not the live corpus:** the spec's "byte-identical rows vs the 2026-07-09 baseline" cannot be a test. `reports/` is gitignored, and the rows derive from a live archive that grows every session (claude was 1338 sessions on 2026-07-09 and climbs daily). A golden test must own its inputs. The live comparison stays a **manual** pre/post check, recorded in the PR body (Task 7).

- [ ] **Step 1: Write the fixtures**

These are the real line-0 shapes, observed on 2026-07-09 against the live archive.

```bash
cat > tests/fixtures/schema_claude.jsonl <<'EOF'
{"type":"last-prompt","sessionId":"s1","leafUuid":"u0"}
{"type":"mode","sessionId":"s1","mode":"default"}
{"sessionId":"s1","timestamp":"t0","message":{"model":"m","content":[{"type":"tool_use","id":"u1","name":"Bash","input":{"command":"ls"}}]}}
{"sessionId":"s1","timestamp":"t1","message":{"content":[{"type":"tool_result","tool_use_id":"u1","content":"a.txt"}]}}
EOF

cat > tests/fixtures/schema_cowork.jsonl <<'EOF'
{"type":"mode","sessionId":"s2","mode":"default"}
{"type":"queue-operation","sessionId":"s2","timestamp":"t0","operation":"enqueue","content":"x"}
{"sessionId":"s2","timestamp":"t1","message":{"content":[{"type":"tool_use","id":"u9","name":"Read","input":{}}]}}
EOF

cat > tests/fixtures/schema_codex.jsonl <<'EOF'
{"type":"session_meta","timestamp":"t0","payload":{"id":"c1"}}
{"type":"event_msg","timestamp":"t1","payload":{"type":"agent_message"}}
{"type":"response_item","timestamp":"t2","payload":{"type":"function_call","name":"exec_command","arguments":"{}","call_id":"call_1"}}
EOF

cat > tests/fixtures/schema_cursor.jsonl <<'EOF'
{"role":"user","message":{"text":"hi"}}
{"role":"assistant","message":{"text":"hello"}}
EOF
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_adapters.py  (append)
from pathlib import Path
from toolbench.adapters import UnknownSchema, detect_parser
from toolbench.parsers import ClaudeParser

FIXTURES = Path(__file__).parent / "fixtures"


def _lines(name: str):
    return iter((FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True))


def test_claude_fixture_detects_as_claude_despite_control_preamble():
    parser, _ = detect_parser(_lines("schema_claude.jsonl"))
    assert isinstance(parser, ClaudeParser)


def test_cowork_fixture_detects_as_claude_with_no_registry_entry_of_its_own():
    parser, _ = detect_parser(_lines("schema_cowork.jsonl"))
    assert isinstance(parser, ClaudeParser)


def test_codex_fixture_raises_unknown_schema_until_tb_12():
    with pytest.raises(UnknownSchema):
        detect_parser(_lines("schema_codex.jsonl"))


def test_cursor_fixture_raises_unknown_schema():
    with pytest.raises(UnknownSchema):
        detect_parser(_lines("schema_cursor.jsonl"))


def test_golden_claude_fixture_parses_to_exactly_one_joined_call():
    parser, replayed = detect_parser(_lines("schema_claude.jsonl"))
    result = parser.parse(replayed, agent="claude", source="raw", project="p")
    assert result.malformed == 0
    assert len(result.calls) == 1
    call = result.calls[0]
    assert (call.name, call.output_chars, call.no_result) == ("Bash", 5, False)
    assert call.result_source == "block_local"


def test_golden_cowork_fixture_drains_its_unmatched_call():
    parser, replayed = detect_parser(_lines("schema_cowork.jsonl"))
    result = parser.parse(replayed, agent="cowork", source="agentsview", project="p")
    assert len(result.calls) == 1
    assert result.calls[0].no_result is True      # S6
```

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `uv run pytest tests/test_adapters.py -v -k fixture`
Expected: FAIL first if fixtures are absent (`FileNotFoundError`); PASS once Step 1 has been run.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/schema_*.jsonl tests/test_adapters.py
git commit -m "GREEN: pin the four observed line-0 shapes as golden fixtures"
```

---

### Task 7: Manual corpus verification and documentation

**Files:**
- Modify: `README.md`, `SPEC.md`, `.lattice/orchestration/CLOSEOUT.md`
- Modify: `docs/superpowers/specs/2026-07-09-transcript-schema-dispatch-design.md` (record the two corrections)

- [ ] **Step 1: Capture the pre-change baseline**

Do this on the branch point, **before** any code lands, so the corpus is the same size for both runs.

```bash
git stash list   # ensure clean
uv run python -m toolbench.passive --agent all --all > /tmp/tb13-before.md
grep -A8 "## Agent Breakdown" /tmp/tb13-before.md
```

- [ ] **Step 2: Capture the post-change run**

```bash
uv run python -m toolbench.passive --agent all --all > /tmp/tb13-after.md
diff <(grep -A8 "## Agent Breakdown" /tmp/tb13-before.md) \
     <(grep -A8 "## Agent Breakdown" /tmp/tb13-after.md)
```

Expected diff: `claude`, `cowork`, `hermes` rows **identical**. `codex` and `cursor` rows **disappear** from the breakdown and appear in `skipped_roots`. Paste this diff into the PR body — it is the evidence, and it cannot live in a test.

- [ ] **Step 3: Update `SPEC.md`**

Add acceptance criteria:

```markdown
- **S24** — Schema dispatch. `detect_parser` sniffs up to 100 non-empty lines and
  returns the single parser whose `claims_line` matches. Two matches raise
  `AmbiguousSchema`; zero matches raise `UnknownSchema`. Both subclass
  `RuntimeError`, so `passive.main` demotes the session to `skipped_roots`.
- **S25** — No parser is the default. An unrecognized transcript is never parsed
  by `ClaudeParser`, and never reported as an agent with zero tool calls.
```

- [ ] **Step 4: Update `README.md`**

Document the three-layer model (loader / parser / adapter), that hermes is source-keyed and the rest content-keyed, and that codex + cursor currently land in `skipped_roots` pending TB-12. State the test count (145 + 27 new = 172; verify with `uv run pytest tests/ -q | tail -1`).

- [ ] **Step 5: Record the spec corrections**

Append to the design doc's "Corrections to the ticket" section:

```markdown
4. **Guard tuple.** The spec quoted `except (OSError, RuntimeError)`. The real
   guard at `passive.py:456` is `(OSError, RuntimeError, UnicodeDecodeError)`.
   The conclusion holds: `UnknownSchema` is a `RuntimeError` and is caught.
5. **Regression pin.** "Byte-identical rows vs the 2026-07-09 baseline" is not a
   test: `reports/` is gitignored and the corpus grows daily. Replaced by golden
   fixtures (`tests/fixtures/schema_*.jsonl`) plus a manual pre/post diff in the
   PR body.
6. **`registry.py`.** Not in the original module list; required to break the
   `hermes.py` <-> `adapters.py` import cycle.
```

- [ ] **Step 6: Update CLOSEOUT.md and close the ticket**

```bash
uv run pytest tests/ -q | tail -1
uv run ruff check . && uv run mypy --strict toolbench
git add README.md SPEC.md .lattice/orchestration/CLOSEOUT.md docs/
git commit -m "DOCS: TB-13 schema-dispatch seam; codex/cursor now skip loudly"
lattice status TB-13 review --actor agent:claude --reason "seam landed; awaiting review"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: interfaces → Tasks 1–4; detection → Task 2; errors → Tasks 2, 5; loaders + NUL ordering → Task 3; hermes as peer adapter → Task 4; `_parse_ref` reduction and temp-file deletion → Task 5; preserved S1/S2/S5/S6 → Tasks 1, 3, 6; acceptance → Tasks 6, 7. The one spec requirement with no task is the live-corpus byte-identical pin, deliberately downgraded to a manual check with the reason recorded in three places.

**Placeholders.** None. Every code step carries runnable code; every test step names the exact command and expected outcome.

**Type consistency.** `claims_line` (classmethod, on `TranscriptParser`) and `claims` (instance method, on `SessionAdapter`) are distinct by design — one interrogates a decoded line, the other a `SessionRef`. `detect_parser` returns `tuple[TranscriptParser, Iterator[str]]` in Task 2 and is destructured as `parser, replayed` in Tasks 4 and 6. `SessionLoader.lines(ref)` takes the ref in Task 3 and is called as `self._loader(ref).lines(ref)` in Task 4.

**Sequencing.** TB-12 and TB-13 both touch `transcript.py`. Whichever lands second rebases onto the first; they are not developed in parallel. After TB-13, TB-12 shrinks to: add `CodexParser` to `parsers.py`, add it to `PARSERS`, delete `tests/test_adapters.py::test_codex_fixture_raises_unknown_schema_until_tb_12`.
