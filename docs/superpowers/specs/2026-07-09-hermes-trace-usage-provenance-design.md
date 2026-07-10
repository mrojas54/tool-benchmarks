# Design: usage provenance and probe turn-key refusal (TB-18)

**Ticket:** TB-18 — *hermes `--format trace` parses as claude but carries no usage or requestId; cache-hit signal silently fabricated*
**Status:** design, awaiting approval
**Date:** 2026-07-09
**Depends on:** TB-13 (schema dispatch, PR #19), TB-16 (response-pooled isolability)

---

## 1. Problem

`hermes sessions export --format trace` (v0.18.2, upstream `daedf4f6`) emits Claude Code-shaped
JSONL carrying `sessionId`. TB-13's dispatch detects claude by `"sessionId" in entry`
(`parsers.py:83`), so a trace export parses **cleanly** as `ClaudeParser`: it yields real
`ToolCall` rows, raises nothing, and reports zero malformed lines.

It also carries no `message.usage` and no `requestId` on any line.

This is the inverse of TB-12's silent zero. There, calls were *dropped*. Here, calls are *kept*
and two distinct channels are silently null. Nothing in the pipeline notices, because nothing in
the pipeline can currently distinguish **"measured, and the answer was zero"** from
**"never measurable."**

The hazard is not one bug. It is **two independent nulls with two different consumers and two
different blast radii.**

| null | consumer | failure |
|---|---|---|
| `usage is None` | `passive.py:346` | `cache_note = "no"` — reports a measured zero that was never measured. **Under-reports.** |
| `requestId` absent | `probe.py:_turn_key` | silently falls back to `ts:` grouping — the exact defect TB-16 proved wrong. **Reports wrong numbers.** |

They cannot share a fix. A `usage` flag does nothing for `probe.py`, whose bug has no
relationship to usage. A guard inside `detect_parser` never reaches `probe.py` at all: `probe.py`'s
only toolbench import is `from toolbench.transcript import ToolCall, parse_session`
(`probe.py:13`) and it never calls `registry.pick_adapter`. It bypasses the schema-dispatch seam
entirely.

### 1.1 The root cause is a conflated null

`usage=None` already does triple duty *today*, before trace exports enter the picture:

1. **Absent by schema design** — `hermes.py:176`. The hermes SQLite DB carries usage on the
   *session* row, not per tool call, so there is no honest per-call usage record to report.
   (`hermes.py`'s docstring justifies this by saying hermes "records `token_count` per message."
   That premise is false — see §2.1 — but the conclusion holds, and holds harder.)
2. **Absent by export truncation** — hermes `--format trace`. The producer *had* usage and the
   serializer dropped it.
3. **Present but empty** — a `usage` dict that carries no cache keys.

`passive.py` collapses all three into the string `"no"`. That is the defect.

---

## 2. Evidence

From the ticket, plus one measurement run during design.

**Detection.** Running the real detector on `20260709_122716_09e9bb` selects `ClaudeParser`,
yields 13 tool calls, 0 malformed, `usage=None` on every call, `model='zai/GLM-5.1:US'`,
`result_source='block_local'`.

**Corpus.** 47 sessions / 650 records: **0** lines carry `message.usage`; **0** carry `requestId`;
280 `tool_use` blocks are present and parse.

**Redaction.** `--format trace` applies forced secret redaction by default (`--no-redact` disables
it). If redaction rewrote tool-result bodies, `output_chars` would shift and the token leaderboard
would be poisoned too. Exporting the same 43 sessions both ways and parsing both trees through the
real `ClaudeParser`:

```
             calls   output_chars  w/ usage
redacted       270         298462         0
unredacted     270         298474         0

output_chars delta: 12  =>  tokens delta: 3   (~0.004%)
```

Two readings. First, the usage hazard is confirmed at n=270: **zero** calls carry usage in either
tree. Second — and this **corrects the ticket** — all 43 files differ at the byte level, and the
parsed delta is small but *not zero*. TB-18 says "`tokens` is NOT poisoned." The measurement
supports "**perturbed by 3 tokens across 270 calls; negligible, not zero.**" The absolute claim
should not survive into SPEC.md. Redaction evidently touches mostly fields that do not feed
`output_chars`.

### 2.1 Where hermes usage actually lives (and why `ABSENT_BY_SCHEMA` is right)

`hermes.py`'s docstring justifies `usage=None` by asserting that hermes "records `token_count` per
message, not per tool call." Read-only inspection of all four archive databases
(`~/.hermes/state.db` plus three profiles) shows the premise is false:

```
db                                    rows   non-null   >0
state.db                              6737          0    0
profiles/aphrodite-mood/state.db      2006          0    0
profiles/light-mood/state.db           746          0    0
profiles/tech-interviewing/state.db    644          0    0
-------------------------------------------------------------
TOTAL                                10133          0    0
```

`messages.token_count` exists in the schema and is **populated zero times out of 10,133 rows.**
Usage lives one level up, on the session row:

```
sessions: 788   with input_tokens > 0: 743   with cache_read_tokens > 0: 714
columns: input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
         reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, cost_source
```

The conclusion is unchanged and better supported: hermes has **no per-call usage to give**, so
`ABSENT_BY_SCHEMA` is correct. The granularity gap is *session → call*, not *message → call*. The
docstring at `hermes.py:116` should be corrected as part of this work; it is a comment fix, not a
behaviour change.

This also settles the §7 open question. `ABSENT_BY_SCHEMA` is not a guess about a producer's
intent — it is a checkable fact about its storage.

### 2.2 Second corpus: an independent trace export reproduces the hazard

A single-session trace export taken from an unrelated project
(`aphrodite-oracle`, 87 records, 44 user / 43 assistant) reproduces TB-18 exactly:

```
with sessionId    : 87/87      → ClaudeParser claims it
with requestId    :  0/87      → probe.py's null
with message.usage:  0/87      → passive.py's null

detect_parser -> ClaudeParser ; calls=1 ; malformed=0
```

Driving both consumers over it, verbatim from the current code:

```
passive.py:346   tool='read_file' calls=1 cache_hits=0  ->  cache_note='no'
probe.py:_turn_key                                      ->  'ts:2026-07-10T00:50:31.403Z'
```

`passive.py` reports "no cache hits" for a session whose `--format jsonl` sibling records
`cache_read_tokens: 1792854` and `cache_write_tokens: 624458`. Roughly 2.4M cache tokens rendered
as `"no"`. `probe.py` groups by timestamp — the pre-TB-16 defect — in silence.

Note the tool name: `read_file`, a hermes-native tool, not Claude Code's `Read`. This confirms the
§3 non-finding that schema and producer are separate axes, and it is the mechanism by which a
mixed-provenance `(agent, tool)` bucket forms.

---

## 3. Non-goals

Both carried forward from TB-18, which recorded them as explicit non-findings. Neither is a bug.

- **Do not "fix" the dispatch.** Classifying a hermes trace export as `claude` is **correct by
  design.** Schema and producer are separate axes; `ClaudeParser`'s docstring already says "one
  parser, two agents." The export *is* Claude-shaped. Detection is working.
- **Do not migrate `hermes.py` to trace.** The SQLite adapter has strictly *more* information:
  message-level `token_count` exists in the DB, and trace drops it. Migrating would lose data.

Also out of scope: the hermes CLI spec-sheet corrections recorded in TB-18 (five formats not six;
`--only {user-prompts}`; 23 filter flags). Those belong to the export-plan document, not to this
design.

---

## 4. Design

Make absence **explicit and typed** at the row, **countable** at the aggregate, and **fatal** where
it corrupts rather than merely omits.

### 4.1 The enum lives on the row

```python
class UsageProvenance(Enum):
    PRESENT           # message.usage was read off the entry
    ABSENT_BY_SCHEMA  # producer has no per-call usage to give (hermes.py:176)
    ABSENT_UNEXPECTED # schema implies usage; the entry lacked it
```

The third arm is named for **what was observed, not what caused it.** `ClaudeParser` can see that
a claude-shaped assistant entry has no `message.usage`. It cannot see that a `--format trace`
export is why. Naming the arm `ABSENT_BY_EXPORT` would bake an unverifiable causal inference into a
type — and re-conflating observation with cause is the precise mistake this ticket exists to undo.
A truncated file, or a future upstream schema change, lands in `ABSENT_UNEXPECTED` correctly and
without the type lying about the reason.

`ABSENT_BY_SCHEMA` is different: it is **self-declared ground truth.** `hermes.py` constructs its
own rows and knows the DB has no per-call usage. Only a producer may assert it.

Assignment, one rule per producer:

| producer | provenance |
|---|---|
| `hermes.py` (SQLite adapter) | always `ABSENT_BY_SCHEMA` |
| `ClaudeParser`, `usage` key present and is a `dict` | `PRESENT` |
| `ClaudeParser`, `usage` key missing or not a `dict` | `ABSENT_UNEXPECTED` |
| tool-result rows (`parsers.py:170,190`) | inherit from their pending call |

**Edge case, decided:** `usage={}` is `PRESENT`. The channel existed and reported nothing. That is
a measured zero, and `_is_cache_hit({})` correctly returns `False`. Only a *missing or non-dict*
`usage` is `ABSENT_UNEXPECTED`.

`ToolCall` gains one field. `ParseResult` needs nothing: `Reducer.absorb` iterates
`result.calls`, so per-row provenance folds without a session-level summary.

### 4.2 The aggregate needs a counter, not an enum

This is the part the obvious design gets wrong.

`tool_stats` is keyed `(agent, call.name)` (`passive.py:106`). Because a trace export *detects as
claude*, a single `ToolStats` bucket can absorb rows from a genuine Claude transcript (usage
`PRESENT`) and from a trace export (`ABSENT_UNEXPECTED`) in the same run. A scalar
`stats.usage_provenance` has no correct value in that bucket. And `Reducer` is documented as a
streaming aggregator that "never stores a corpus-wide call list" (S11), so there is nothing to
re-scan after the fact.

A counter composes under streaming addition and preserves the mixed case that an enum would
flatten:

```python
@dataclass
class ToolStats:
    ...
    cache_hits: int = 0
    usage_missing: int = 0   # calls whose usage channel was unavailable
```

In `absorb`, alongside the existing `_is_cache_hit` bump:

```python
if call.usage_provenance is not UsageProvenance.PRESENT:
    tool_stats.usage_missing += 1
    model_stats.usage_missing += 1
```

Note that **both** absent arms increment `usage_missing`. The `PRESENT`/`ABSENT_BY_SCHEMA`/
`ABSENT_UNEXPECTED` distinction drives diagnostics and future warnings; it does not drive the
cache flag, for which every flavour of absence means the same thing: not measurable.

### 4.3 The render rule becomes total

Replacing `passive.py:346`:

```python
if stats.cache_hits > 0:
    cache_note = "yes"                       # at least one real hit observed
elif stats.usage_missing == 0:
    cache_note = "no"                        # measured, and it was zero
elif stats.usage_missing == stats.calls:
    cache_note = "n/a"                       # never measurable
else:
    cache_note = "n/a*"                      # partially measurable; some rows blind
```

Four cases, exhaustive over `(cache_hits, usage_missing, calls)`. The `n/a*` case is the one a
scalar enum could not express, and it is reachable the moment a corpus mixes real transcripts with
trace exports under one agent label.

`"yes"` is deliberately still `cache_hits > 0` and not conditioned on `usage_missing`: a single
observed hit is a positive existence proof, and no amount of surrounding blindness weakens it.

### 4.4 probe.py refuses rather than degrades

`probe.py` measures per-turn token cost. TB-16 established that `output_tokens` is billed against
the API response, keyed by `requestId` (S26). A corpus that cannot be grouped by `requestId` does
not yield a degraded probe result — it yields a **wrong** one, silently, with the pre-TB-16
grouping that TB-16 was filed to eliminate. There is no useful partial answer, so there is no
partial mode.

```python
class NonIsolableTurns(RuntimeError):
    """Raised when turns cannot be keyed to the billing unit (S26)."""


def _turn_key(entry: dict[str, object], ts: str) -> str:
    """The unit `output_tokens` is billed against: the API response (S26)."""
    request_id = entry.get("requestId")
    if not (isinstance(request_id, str) and request_id):
        raise NonIsolableTurns(
            "probe requires requestId to group turns (S26); "
            "this corpus lacks it — trace-format export?"
        )
    return f"req:{request_id}"
```

`NonIsolableTurns` subclasses `RuntimeError`, matching `SeededReportError` (`probe.py:18`),
`UnknownSchema`/`AmbiguousSchema` (`adapters.py:39,48`), and `NonTranscriptExport`
(`sources.py:22`).

This deliberately mirrors TB-15's lesson: a contaminated arm that still produces plausible numbers
is more dangerous than one that refuses. The `ts:` fallback is removed, not guarded. The `ts`
parameter becomes unused and should be dropped from the signature.

**Asymmetry, stated plainly:** `passive.py` degrades gracefully and annotates (`n/a`), because
omitting a caveat-only flag is survivable and S19 already forbids that flag from affecting ranking.
`probe.py` refuses, because its output is the measurement itself. Same input, opposite policy,
because the blast radii differ.

---

## 5. New acceptance criteria

Next free IDs are S29 and S30 (`SPEC.md` currently ends at S28, line 178).

> **S29 — usage provenance.** Every `ToolCall` carries a `UsageProvenance` of `PRESENT`,
> `ABSENT_BY_SCHEMA`, or `ABSENT_UNEXPECTED`. A producer that structurally cannot supply per-call
> usage declares `ABSENT_BY_SCHEMA`; a parser whose schema implies usage but whose entry lacks it
> records `ABSENT_UNEXPECTED`. The passive cache-hit flag renders `n/a` when no call in a bucket
> could be measured, `n/a*` when only some could, and `no` only when usage was available and zero
> hits were observed. Per S19 the flag remains caveat-only and never affects ranking.

> **S30 — probe requires the billing unit.** `probe.py` groups turns solely by `requestId` (S26).
> An entry lacking `requestId` raises `NonIsolableTurns`; there is no timestamp fallback and no
> partial-corpus mode. `hermes sessions export --format trace` output is therefore valid input to
> `passive.py` and invalid input to `probe.py`.

---

## 6. Test plan

RED → GREEN → DOCS, per the project's TDD workflow.

**Fixtures.** A trace-shaped fixture: claude schema (`sessionId` present, `tool_use` blocks
present), no `message.usage`, no `requestId`. This is the fixture the whole ticket turns on and it
must be derived from a real export, not hand-written.

**Provenance (S29)**
- `ClaudeParser` on a real transcript → every call `PRESENT`.
- `ClaudeParser` on the trace fixture → every call `ABSENT_UNEXPECTED`, and `malformed == 0`
  (the point is that it parses cleanly).
- `usage={}` → `PRESENT`, and `_is_cache_hit` returns `False`. Guards the edge case in §4.1.
- `hermes.py` adapter → every call `ABSENT_BY_SCHEMA`.

**Render (S29)** — one test per arm of the four-case rule, driven through `Reducer`:
- hits > 0 → `"yes"`; usage present, no hits → `"no"`; all rows absent → `"n/a"`.
- **mixed bucket → `"n/a*"`.** Absorb a real-transcript session and a trace session under the
  same `(agent, tool)` key. This is the test that fails against a scalar-enum implementation, and
  it is the reason the counter exists.

**probe refusal (S30)**
- `_turn_key` on an entry with `requestId` → `req:<id>`.
- `_turn_key` on an entry without → raises `NonIsolableTurns`.
- End-to-end: `probe.py` against the trace fixture exits non-zero with the message, and does **not**
  emit a comparison table. Extends the `SeededReportError` guard: no table without real measurement.
- Regression: the existing 245-batch `requestId` grouping test still passes.

**Gate.** `uv run ruff check .`, `uv run mypy --strict`, `uv run python -m unittest` — all green,
counts reported. Baseline is 213 passing / 1 skipped.

---

## 7. Risks and open questions

**A new enum field on `ToolCall` is a breaking change.** Every construction site must set it.
Sites: `parsers.py:137,170,190`, `hermes.py:176`, and every test fixture that builds a `ToolCall`
directly. Mitigation: **no default value.** A default would silently mark unconverted call sites
`PRESENT` — reintroducing exactly the fabricated-certainty bug this ticket exists to kill.
`mypy --strict` then enumerates every site for us. The absence of a default is a load-bearing
design decision, not an oversight.

Two mechanical consequences follow, and the implementer will hit both on the first run:

1. **Field ordering is forced.** `ToolCall` ends with `no_result: bool = False` and
   `result_source: str | None = None`. A dataclass cannot place a non-defaulted field after a
   defaulted one, so `usage_provenance` must be *inserted* into the non-default block — naturally
   right after `usage`, which it annotates — not appended.
2. **Positional construction breaks.** Inserting mid-struct shifts every positional argument after
   `usage`. Any site building a `ToolCall` positionally must be converted to keywords. Worth a
   quick `rg` for `ToolCall(` before starting, so this is a known quantity rather than a surprise
   in the middle of the RED phase.

**`n/a*` is an unusual token in a markdown table column.** It needs a legend line wherever the
table is rendered, or it reads as a footnote marker pointing nowhere.

**Settled — the `n/a*` mixed-bucket fixture must be synthetic.** The natural trace corpora
available (§2.2's 87-record export carries exactly one `tool_use` block) cannot produce a bucket
holding both `PRESENT` and absent rows under one `(agent, tool)` key. Construct it: absorb one real
Claude transcript session and one trace session into the same `Reducer` under the same agent label.
This is the test that fails against a scalar-enum implementation, so it is not optional.

**Open — should `ABSENT_UNEXPECTED` also emit a one-time warning from `passive.py`?** TB-18's
candidate fix (b) proposed a provenance warning at detect time. The typed field makes the *data*
honest; a warning would make the *operator* aware. Deferring: it is additive, it does not change
the type, and it can land as a follow-up once we see how often the arm fires in real runs.

---

## 8. Out of scope, discovered during design — file separately

**`hermes.py:63` cannot read one of the four archive databases.** `_connect` opens
`file:{db}?mode=ro`. `~/.hermes/profiles/aphrodite-mood/state.db` has no `-wal`/`-shm` sidecar, and
SQLite cannot open a WAL-mode database read-only without creating the shared-memory file, so the
connection raises `OperationalError: unable to open database file`. That profile — 2,006 messages —
is invisible to the hermes adapter today.

This is a live bug, unrelated to TB-18, and it should get its own ticket rather than being smuggled
into this one. Two cautions for whoever takes it:

- `immutable=1` opens the file but **ignores the WAL and returns stale data.** On
  `tech-interviewing/state.db` it reports 639 messages where `mode=ro` reports 644. It is not a
  drop-in fix.
- The `mode=ro` comment at `hermes.py:62` ("a running hermes owns this file. Never open it
  writable") is correct and must be preserved by any fix.

This deepens the under-sampling already noted in `hermes.py`'s own module docstring (89 sessions
via `session list` vs 789 via `agentsview stats`).
