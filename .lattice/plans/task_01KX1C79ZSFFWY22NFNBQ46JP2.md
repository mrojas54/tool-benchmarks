# TB-3: parse_session id-join

ParseResult(calls, malformed), _result_id/_result_payload, block-local content precedence, malformed + interrupted handling; parser fixtures. Carries the flagged join-key risk (de-risked by block-local fixture).

SPEC: S1, S2, S5, S6, S24
BUILDPLAN anchor: T2
Depends on: T1

## Plan (filled in by delegator)

Working against `origin/tb-2-scaffold` @ dd96d91 (TB-2's `toolbench/transcript.py` with
`ToolCall` + `result_len` already merged there). This ticket extends that same file
additively; it does not touch `sources.py`, `passive.py`, or `probe.py`.

### Data model additions (additive, backward compatible with TB-2's ToolCall)

- `ParseResult` dataclass: `calls: list[ToolCall]`, `malformed: int`.
- `ToolCall` gains two new fields, both with defaults so TB-2's existing
  `ToolCallTests._make` fixture (which constructs `ToolCall(**fields)` without
  these) keeps passing unmodified:
  - `no_result: bool = False` (S6 — interrupted call flag)
  - `result_source: str | None = None` — records whether the joined payload
    came from `"top_level"` or `"block_local"` (S2).
- Private helper `_PendingCall` dataclass to hold an in-flight `tool_use`
  (name, input_chars, session_id, ts, usage) until its result arrives or the
  session ends (interrupted).

### `parse_session(path, *, agent="claude-code", source="raw", project=None) -> ParseResult`

- Opens `path`, reads line by line (streams, no full-file materialization
  beyond the pending-calls dict, matching S11's "no full in-memory list"
  spirit even though S11 itself is T4 scope).
- `project` defaults to `Path(path).parent.name` when not supplied — the
  caller (future T4 `passive.py`) may override; not exercised by SPEC IDs in
  this ticket but keeps the signature usable without T3/T4 existing yet.
- Per line: strip; skip blank lines (not malformed); `json.loads`; on
  `JSONDecodeError` (or non-dict top-level value) increment `malformed` and
  continue — never fatal (S5).
- Assistant-side: for each `message.content[]` block with
  `type == "tool_use"`, register a `_PendingCall` keyed by the block's `id`.
  `input_chars` computed via the existing `result_len(block["input"])`
  (reused, not reimplemented — the dict-normalization branch already handles
  arbitrary JSON-able input).
- Result-side, per line: gather `tool_result` blocks from
  `message.content[]` (type-filtered) when content is a list; if none found
  but a top-level `toolUseID` exists, treat the whole entry as one implicit
  "block" (`block=None`) so the top-level-only shape still joins.
- `_result_id(entry, block) -> str | None` — block-local `tool_use_id` first,
  falls back to top-level `toolUseID` (S1: EITHER location joins).
- `_result_payload(entry, block) -> tuple[object, str | None]` — block-local
  `content` WINS when present (`"block_local"`); else top-level
  `toolUseResult` (`"top_level"`); else `(None, None)` (S2).
- On a successful id match: pop the pending call, compute
  `output_chars = result_len(payload)`, stamp `result_source`, append a
  `ToolCall`. `error` is set from the block's `is_error` flag when present
  (best-effort; not covered by S24 fixtures — real shape support only).
  `duration_ms` is always `None` — raw Claude Code JSONL carries no
  per-tool-call duration field; nothing to compute it from at this layer.
- After the file is exhausted, every `_PendingCall` still in `pending` had no
  matching result — interrupted (S6). Emit a `ToolCall` for each with
  `output_chars=0, no_result=True, result_source=None`. Kept, not dropped.

### Fixtures (S24) — `tests/fixtures/sample.jsonl`

One synthetic session, 8 lines, matching real Claude Code JSONL shape
(`sessionId`, `timestamp`, `message.role/content`, top-level
`toolUseID`/`toolUseResult` sibling to `message`):

1. `Bash` tool_use → result matched via **top-level-only** `toolUseID` +
   string `toolUseResult`. Exercises the top-level join-key location and the
   plain-string payload shape.
2. `mcp__search__query` tool_use → result matched via **block-local-only**
   `tool_use_id` + block-local `content` as an MCP block-list
   (`[{"type":"text","text":...}, ...]`). Exercises the block-local join-key
   location and the MCP block-list payload shape.
3. `Read` tool_use → result entry carries **BOTH** top-level `toolUseID` +
   `toolUseResult` (a decoy/stale payload) **AND** a block-local
   `tool_use_id` + `content` (the real payload) for the *same* id — this is
   the flagged de-risking fixture: asserts block-local wins and
   `result_source == "block_local"`, matching the real-world shape where
   Claude Code entries commonly carry both.
4. `Write` tool_use with **no** matching result line anywhere in the file —
   interrupted call (S6): `output_chars=0`, `no_result=True`.
5. One line of genuinely truncated/invalid JSON — malformed (S5):
   `malformed == 1`, no `ToolCall` produced, parse does not raise.

This covers all five S24 categories (string, MCP block-list, block-local
content, interrupted, malformed) and exercises both S1 join-key locations
(fixture 1 top-level, fixtures 2 & 3 block-local).

### Tests (`tests/test_transcript.py`, extended)

- `ResultIdPayloadTests` — direct unit tests of `_result_id` / `_result_payload`
  covering: top-level-only id, block-local-only id, top-level-only payload,
  block-local-wins-over-top-level payload, neither-present → `(None, None)`.
- `ParseSessionTests` — loads `tests/fixtures/sample.jsonl`, asserts:
  - `result.malformed == 1`
  - `len(result.calls) == 4`
  - Per call (keyed by `name`): `output_chars`, `result_source`, `no_result`
    match the fixture design above (Read's `output_chars` specifically must
    equal the block-local payload length, not the stale top-level one —
    this is the precedence assertion).

### Self-review checklist (before moving to `planned`)

- [ ] Join-key correctness: both top-level and block-local locations
  actually exercised by distinct fixtures, not just theoretically supported.
- [ ] Block-local payload precedence: fixture 3 has genuinely different
  top-level vs. block-local payloads so the test can't pass by accident.
- [ ] Fixture fidelity: shapes mirror real Claude Code JSONL (sibling
  `toolUseID`/`toolUseResult` next to `message`, not nested inside it).
- [ ] `ToolCall` changes are additive only — TB-2's existing
  `ToolCallTests` in `tests/test_transcript.py` must keep passing unmodified.
- [ ] No edits to `sources.py`, `passive.py`, `probe.py`, or
  `toolbench/__init__.py`.

### Workflow

1. Tests first (RED): write fixture + test additions, confirm they fail
   (missing `ParseResult`/`parse_session`).
2. Implement (GREEN): extend `toolbench/transcript.py`, confirm full suite
   green including TB-2's pre-existing tests.
3. Own-reviewer pass over `git diff origin/tb-2-scaffold..HEAD`.
4. Validate: `ruff check .`, `mypy --strict toolbench tests`,
   `python -m unittest discover tests`.
5. Push `tb-3-parse`, open PR against `main` (base declares dependency on
   PR #1 / `tb-2-scaffold`), attach PR + validation output to TB-3, land at
   `review`.
