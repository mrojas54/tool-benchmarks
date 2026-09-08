# Transcript schema dispatch (TB-13)

Date: 2026-07-09
Ticket: TB-13
**Status:** shipped (PR #19, merged 2026-07-09). Body below is the 2026-07-09
design snapshot — flat-layout paths (`toolbench/…`) and the path-based
`parse_session` API named in the Problem section are historical. Live modules
live under `src/toolbench/`; schema classes own `parsers.py`, sniff/dispatch is
`adapters.detect_parser` over `DETECT_WINDOW` (not `JsonLines`), and path-based
`parse_session` is retired (CQ 1.3). Operator truth:
[`README.md`](../../../README.md) (Module ownership) + SPEC S27.
Supersedes: the design sketch in TB-13's own description (see "Corrections to the ticket")

## Problem

`toolbench/transcript.py:parse_session` recognizes exactly one transcript schema:
Claude's assistant-message `tool_use` blocks joined to `tool_result` blocks by
`tool_use_id`. It is also the unnamed default: `passive.py:_parse_ref` routes every
non-hermes session into it, whatever schema that session actually speaks.

A transcript in an unrecognized schema therefore matches nothing, returns
`ParseResult(calls=[], malformed=0)`, and reports as a healthy zero. That is TB-12:
all 60 codex sessions parse without error and contribute 0 of their 2,089 tool calls.
Cursor's 47 sessions do the same.

A parser that cannot recognize a schema must not be the fallback for schemas it has
never seen. This ticket lands the seam that makes that structurally impossible.

## Findings (2026-07-09, live archive, read-only)

### The ticket's detection spec does not work

TB-13's description proposes sniffing the **first non-empty line** and matching
Claude on `message.content` containing `type == "tool_use"`, codex on
`entry["type"] == "response_item"`.

Neither marker appears on line 0 of any real session:

| agent | line 0 `type` | line 0 keys |
| --- | --- | --- |
| claude | `last-prompt` | `leafUuid`, `sessionId`, `type` |
| cowork | `mode` | `mode`, `sessionId`, `type` |
| codex | `session_meta` | `payload`, `timestamp`, `type` |
| cursor | *(absent)* | `message`, `role` |

Transcripts open with control/metadata preamble. Codex's first `response_item` is on
line 2. Claude's first `tool_use` may be hundreds of lines in, or absent entirely from
a session that used no tools. **Implemented as written, `detect_schema` would raise
`UnknownSchema` on every session in the corpus** — converting a silent-zero bug into a
total blackout.

### Discriminators that do work

| schema | predicate on a decoded line | agents |
| --- | --- | --- |
| `claude` | `"sessionId" in entry` | claude, cowork |
| `codex` | `"payload" in entry` and `type` ∈ {`session_meta`, `event_msg`, `response_item`, `turn_context`} | codex |
| `cursor` | `"role" in entry` and `"message" in entry` and `"type" not in entry` | cursor (not parsed; TB-12 defers) |

Measured across 40 sessions (10 per agent):

| agent | n | max depth to first discriminating line | outcome |
| --- | --- | --- | --- |
| claude | 10 | 0 | `claude` |
| cowork | 10 | 0 | `claude` |
| codex | 10 | 0 | `codex` |
| cursor | 10 | 0 | `cursor` |

Zero ambiguity: no line was claimed by two parsers. Every session discriminated at
depth 0. `cowork` — a distinct agent — resolves to the `claude` schema with no
registry entry of its own, which is the measured justification for dispatching on
payload rather than on producer.

### `_parse_ref` duplicates a loader that already exists

`sources.py:open_session_jsonl` already takes a `SessionRef`, branches raw-path vs.
AgentsView export, performs the NUL sniff on both branches, and yields lines. It is a
loader in all but name.

`passive.py:_parse_ref` ignores it for the raw-path case (re-implementing the binary
sniff inline) and, for the AgentsView case, writes its generator to a
`NamedTemporaryFile` and reopens it.

The temp file buys a *rewind*, not buffering — and nothing needs a rewind.
`_run_agentsview` uses `subprocess.run(capture_output=True)`, so the entire export is
already a `str` in memory before the first line is yielded. For hermes that was 37 MB
buffered in RAM, spooled to disk, and read back. Bounded lookahead plus
`itertools.chain` replaces it.

## Decision

An ABC-based, two-axis design: **loaders** (acquisition) and **parsers** (schema),
composed behind one adapter interface.

Acquisition and schema are orthogonal — three acquisition mechanisms, four schemas,
and they do not line up:

| agent | acquisition | schema |
| --- | --- | --- |
| claude (raw) | local file path | claude |
| claude (agentsview) | export subprocess | claude |
| cowork | export subprocess | claude |
| codex | export subprocess | codex |
| cursor | export subprocess | cursor |
| hermes | SQLite direct read | hermes |

### Interfaces

```python
class SessionAdapter(ABC):
    """The single seam passive.py sees: a SessionRef becomes a ParseResult."""
    @abstractmethod
    def claims(self, ref: SessionRef) -> bool: ...
    @abstractmethod
    def parse(self, ref: SessionRef) -> ParseResult: ...


class SessionLoader(ABC):
    """Acquisition. Knows nothing about schemas."""
    @abstractmethod
    def lines(self, ref: SessionRef) -> Iterator[str]: ...


class TranscriptParser(ABC):
    """Interpretation. Knows nothing about acquisition."""
    schema_tag: ClassVar[str]

    @classmethod
    @abstractmethod
    def claims_line(cls, entry: dict[str, object]) -> bool: ...

    @abstractmethod
    def parse(
        self, lines: Iterable[str], *, agent: str, source: str, project: str
    ) -> ParseResult: ...
```

Concrete types: `RawFileLoader`, `AgentsViewLoader`; `ClaudeParser` (the body of
today's `parse_session`); `HermesAdapter`; `ComposedAdapter(loader, parser)`.
`CodexParser` is TB-12's, not this ticket's.

### Dispatch

`pick_adapter(ref)` walks an ordered registry and returns the first adapter whose
`claims(ref)` is true. Source-keyed adapters get first refusal; `ComposedAdapter` is
the terminal fallback and content-sniffs to choose its parser.

`HermesAdapter.claims(ref)` is `ref.agent == "hermes" and ref.path is None`.

This does not *delete* the hermes branch — it relocates it from control flow in
`_parse_ref` into a declared property of the adapter. The gain is that adding an agent
never edits the dispatcher, and no parser is the anonymous default.

Hermes stays keyed on source, not content, and stays outside the loader/parser pipe.
It has no lines: it runs two SQL queries and joins them in memory. Forcing it to yield
synthetic JSON lines so a `HermesParser` could re-decode them was considered and
rejected — a serialize/deserialize round-trip bought only symmetry.

### Detection

`detect_parser(lines)` decodes up to **100** non-empty lines, returning the first
parser whose `claims_line` is true.

- Measured max depth is 0; the bound is insurance against unseen preamble, not a
  working requirement.
- Undecodable lines inside the window are skipped, not counted — malformed-line
  accounting stays the parser's job (S5), so a session is never charged twice.
- Exhausting the window raises `UnknownSchema`.
- Two parsers claiming the same line is a programming error, not a data error: it
  raises `AmbiguousSchema` (a `RuntimeError`) rather than silently picking one.

Lookahead is fed back to the parser via `itertools.chain(buffered, rest)`. The
transcript is read once.

### Errors

`UnknownSchema(RuntimeError)`, alongside the existing `NonTranscriptExport`. Because
`passive.main` already guards each session with
`except (OSError, RuntimeError, UnicodeDecodeError)` (`passive.py:456`), an unknown
schema demotes to `skipped_roots` with **no change to that guard**. The agent is named
in the Summary instead of reported as a healthy zero. This single change would have
surfaced TB-12 on the run that created it.

Ordering is load-bearing: the NUL sniff runs in the **loader**, before detection. A
SQLite dump has no first JSON line to detect.

## Preserved exactly

These are existing acceptance criteria, not refactor latitude.

- **S5** — malformed lines counted and skipped, never fatal. The per-line
  `json.JSONDecodeError` guard and the `errors="replace"` open both stay.
- **S6** — the end-of-file `pending` drain emitting `no_result=True` calls with
  `output_chars=0`. A seam that drops the drain silently loses unmatched calls,
  reintroducing TB-12's bug class inside the fix for it.
- **S1/S2** — Claude join and payload precedence: `tool_use_id` over `toolUseID`;
  block-local `content` over `toolUseResult`.
- `result_len`, `ToolCall`, `ParseResult` are schema-neutral. They stay in
  `transcript.py`, unchanged, reused by every parser. Do not fork them.

### One API break, handled

`parse_session(path, project=None)` derives `project` from `path.parent.name`. A
parser fed `Iterable[str]` has no path to derive from. `parse_session` therefore
survives as a compatibility shim: it resolves the project from the path, opens the
file, and delegates to `ClaudeParser`. The deprecated alias keeps its documented
default rather than silently changing it.

## Scope

**In:** `adapters.py` (new), `parsers.py` (new), loaders extracted into `sources.py`,
`HermesAdapter` in `hermes.py`, `_parse_ref` reduced to `pick_adapter(ref).parse(ref)`,
`NamedTemporaryFile` deleted, `UnknownSchema` + `AmbiguousSchema`.

**Out:** `CodexParser` (TB-12). The cursor parser (needs its own repro). Any change to
`passive.py` reducers, report rendering, or the CLI surface. Performance work.

TB-12 and TB-13 both touch `transcript.py`. Whichever lands second rebases onto the
first; they are not developed in parallel.

## Acceptance

- Golden fixtures (`tests/fixtures/schema_*.jsonl`) pin the four observed line-0
  shapes and the joined output of a claude and a cowork session. These are the
  regression test: they are committed, deterministic, and reproducible in CI.
- A **manual** pre/post run of `uv run python -m toolbench.passive --agent all --all`
  shows identical `claude`, `cowork`, and `hermes` rows, with `codex` and `cursor`
  moving into `skipped_roots`. The diff goes in the PR body. This cannot be a test:
  `reports/` is gitignored (`.gitignore:31`) and the corpus grows every session, so
  pinning live rows would fail tomorrow for reasons unrelated to TB-13.
- A fixture session in an invented schema raises `UnknownSchema` and appears in
  `skipped_roots`. It does **not** appear as a 0-call agent row.
- A fixture whose first 100 lines are valid JSON but match no parser raises
  `UnknownSchema` rather than reading to EOF.
- The four observed line-0 shapes above are pinned in a test, so an upstream format
  change fails loudly here instead of silently zeroing an agent.
- Existing 145 tests green. `ruff` clean. `mypy --strict` clean.

## Expected report change

Codex (60 sessions) and cursor (47 sessions) move from a `0 calls` agent row into
`skipped_roots` the moment this lands, and stay there until TB-12 adds `CodexParser`.
This is the ticket working as intended: an unparseable agent must be visible as
unparsed. It is a visible change to the report and should not surprise a reviewer.

## Corrections to the ticket

TB-13's description, as filed and pushed, is wrong or superseded in three places. It
should be amended to match this spec.

1. **Detection markers.** `tool_use` / `response_item` on the first line do not exist.
   Replaced by the `sessionId` / `payload` discriminators, over a 100-line window.
2. **"Reduce `_parse_ref` to: hermes source-check, then `parse_any`."** There is no
   hermes source-check in the dispatcher. `HermesAdapter.claims` owns it.
3. **Function protocol + registry.** Superseded by the ABC hierarchy above, on the
   operator's direction. The protocol shape survives inside `TranscriptParser.parse`.

## Corrections to this spec, found while planning

4. **Guard tuple.** An earlier draft quoted `except (OSError, RuntimeError)`. The real
   guard at `passive.py:456` is `(OSError, RuntimeError, UnicodeDecodeError)`. The
   conclusion holds — `UnknownSchema` is a `RuntimeError` and is caught — but the
   quoted code was wrong. Corrected above.
5. **Regression pin.** "Byte-identical rows vs the 2026-07-09 baseline" is not
   implementable as a test. `reports/` is gitignored and the rows derive from a live
   archive that grows daily (claude: 1,338 sessions on 2026-07-09, climbing). Replaced
   by committed golden fixtures plus a manual pre/post diff. Corrected above.
6. **`registry.py`.** Absent from the module list above, and required. `hermes.py`
   must import `SessionAdapter` from `adapters.py`, so `adapters.py` cannot import
   `HermesAdapter` to register it — a cycle. `registry.py` imports both and is
   imported by neither.

## Corrections found while executing

7. **`test_transcript.py` could not stay untouched.** The plan said to delete
   `_result_id` / `_result_payload` from `transcript.py` *and* that
   `test_transcript.py` would pass unmodified. It imports both names directly, so
   both could not hold. The import site was repointed at `toolbench.parsers`; every
   assertion is unchanged, so the S1/S2 behavioural pin survives intact.

8. **S24/S25 were already taken — and then S26 was too.** The plan directed new
   acceptance criteria to be filed as S24 (schema dispatch) and S25 (no default
   parser). `SPEC.md` already defines S24 (fixtures) and S25 (acceptance smoke), so
   they were first written as S26/S27. While this branch was in flight, PR #17
   merged TB-16's **S26** (response-pooled isolability) to `main`, colliding again.
   Final numbers are **S27/S28**, resolved in the merge of `origin/main`. Acceptance
   IDs are append-only and shared across concurrent branches; check `main` at merge
   time, not just at plan time.

9. **Claim depth is 1, not 0.** The plan's `DETECT_WINDOW` comment asserted a
   measured max depth of 0 across 40 sessions. Measured across all 2,142 loadable
   claude/cowork sessions in the archive, every session claims at decodable line
   **1** and none is ever unclaimed. Depth is counted in *decodable* lines, so blank
   and malformed lines do not advance it — which is why "line 0 is a `last-prompt`
   record" and "claims at depth 1" are both true. Comment corrected in `adapters.py`.

10. **The acceptance diff needs a frozen corpus, not just a pre/post pair.** Running
    the pre- and post-change binaries thirty minutes apart showed claude +71 calls and
    cowork +2 sessions — pure archive growth, including the very session doing the
    refactor. Re-running both binaries with `--date-to 2026-07-08` closes the window
    and yields byte-identical claude/cowork/hermes rows. A manual pre/post check on a
    live archive is not evidence unless the window is closed.
