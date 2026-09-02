# Design: usage provenance and probe turn-key refusal (TB-18)

**Ticket:** TB-18 — *hermes `--format trace` parses as claude but carries no usage or requestId; cache-hit signal silently fabricated*
**Status:** shipped (PR #20, merged 2026-07-10). Body is the 2026-07-09 design
snapshot — Gate counts and `python -m unittest` below are contemporaneous at ship.
Live Quality gate: [`README.md`](../../../README.md#quality-gate)
(`pytest -q`, src-layout mypy, complexity_gate).
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
relationship to usage. And a guard inside `detect_parser` does not reach `probe.py` at all today:
`probe.py`'s only toolbench import is `from toolbench.transcript import ToolCall, parse_session`
(`probe.py:13`, used at `:272`), and it never calls `detect_parser`. It bypasses the
schema-dispatch seam entirely. §4.4 changes that, but the seam alone was never sufficient.

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

### 2.2 An independent trace export reproduces the hazard

A single-session trace export from an unrelated project (`aphrodite-oracle`, 87 records,
44 user / 43 assistant):

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

### 2.3 The producer self-declares: `version: "hermes-agent"`

The decisive fact. Trace exports do not merely *lack* fields — they carry a positive producer tag.

```
                          version values
hermes --format trace     {"hermes-agent": 618/618}   (43-file export)
real claude transcripts   {None, "2.1.170", "2.1.201", "2.1.205", ...}  — semver or absent
```

This is what makes a parser split possible, and it is stronger evidence than any inference from a
missing field. Verified as a **total partition** over the full local archive:

```
NEW ClaudeParser predicate   ("sessionId" in e and e["version"] != "hermes-agent")
  claimed                       : 4036/4036 real transcripts, all at decodable line 1
  never claimed within window   : 0
  real transcripts tagged hermes-agent (would misroute) : 0

NEW HermesTraceParser predicate ("sessionId" in e and e["version"] == "hermes-agent")
  claimed                       : 705/705 trace records (44 files)
  also claimed by ClaudeParser  : 0      ← AmbiguousSchema can never fire
  trace files never claimed     : 0/44
```

TB-13's original detection spec was defective because its proposed discriminator was absent from
real data. This one is present on every record of both classes, and the two predicates are
mutually exclusive by construction.

### 2.4 Redaction does not poison the token leaderboard (correcting the ticket)

`--format trace` applies forced secret redaction by default (`--no-redact` disables it). Exporting
the same 43 sessions both ways and parsing both trees through the real parser:

```
             calls   output_chars  w/ usage
redacted       270         298462         0
unredacted     270         298474         0

output_chars delta: 12  =>  tokens delta: 3   (~0.004%)
```

The usage hazard is confirmed at n=270: **zero** calls carry usage in either tree. But all 43 files
differ at the byte level and the parsed delta is small yet *not zero*. TB-18 says "`tokens` is NOT
poisoned." The measurement supports "**perturbed by 3 tokens across 270 calls; negligible, not
zero.**" The absolute claim must not reach `SPEC.md`.

---

## 3. Non-goals

Both carried forward from TB-18, which recorded them as explicit non-findings.

- **The dispatch was not broken.** Classifying a trace export as *Claude-shaped* is correct: the
  export **is** Claude-shaped, and `ClaudeParser`'s docstring already says "one parser, two
  agents." What was missing is that schema and **producer** are separate axes, and only the schema
  axis was modelled. §4.1 adds the producer axis. This is not a repudiation of the non-finding —
  it is the non-finding, taken seriously.
- **Do not migrate `hermes.py` to trace.** The SQLite adapter has strictly *more* information:
  session-level usage exists in the DB (§2.1) and trace drops it. Migrating would lose data.

Out of scope: the hermes CLI spec-sheet corrections recorded in TB-18 (five formats not six;
`--only {user-prompts}`; 23 filter flags). Those belong to the export-plan document.

---

## 4. Design

Model the **producer** as a parser, the **guarantee** as a type, the **aggregate** as a counter,
and refuse where absence corrupts rather than merely omits.

### 4.1 Split the producer out of the schema

```python
HERMES_TRACE_VERSION = "hermes-agent"


class UsageProvenance(Enum):
    PRESENT           # message.usage was read off the entry
    ABSENT_BY_SCHEMA  # producer has no per-call usage to give (hermes SQLite, §2.1)
    ABSENT_BY_EXPORT  # producer had usage; the export format dropped it (trace)
    ABSENT_UNEXPECTED # claude schema and claude producer, yet no usage — an anomaly
```

```python
class ClaudeParser(TranscriptParser):
    schema_tag: ClassVar[str] = "claude"

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        return "sessionId" in entry and entry.get("version") != HERMES_TRACE_VERSION

    @classmethod
    def _provenance(cls, usage: object) -> UsageProvenance:
        return (UsageProvenance.PRESENT if isinstance(usage, dict)
                else UsageProvenance.ABSENT_UNEXPECTED)


class HermesTraceParser(ClaudeParser):
    """Same schema, different producer, different guarantees."""

    schema_tag: ClassVar[str] = "hermes-trace"

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        return "sessionId" in entry and entry.get("version") == HERMES_TRACE_VERSION

    @classmethod
    def _provenance(cls, usage: object) -> UsageProvenance:
        return UsageProvenance.ABSENT_BY_EXPORT   # unconditional; trace never carries usage


PARSERS = (ClaudeParser, HermesTraceParser)
```

`HermesTraceParser` inherits the entire parse path. It overrides exactly two classmethods. That is
the whole of it, and it is the right shape: the schema really is Claude's, so the parsing really is
`ClaudeParser`'s.

**Why `ABSENT_BY_EXPORT` is now honest.** An earlier draft of this design kept one parser and
inferred provenance per row from a *missing* field. That draft could not name this arm
`ABSENT_BY_EXPORT`, because a `ClaudeParser` looking at a usage-less entry cannot know a trace
export caused it — it can only observe the absence. Splitting the parser dissolves the problem:
`HermesTraceParser` is selected by the producer's own `version` tag (§2.3), so it *knows* what it
is parsing. Inference from an absence is replaced by a positive producer declaration.

**Why `_provenance` is a classmethod hook and not a `usage_provenance` ClassVar.** A ClassVar
would need a sentinel on `ClaudeParser` meaning "infer per row" — almost certainly `None`. That
would reintroduce a null carrying two meanings, in the very design whose purpose is to eliminate
one. A polymorphic method has no sentinel and no default.

**`ABSENT_UNEXPECTED` remains**, now meaning something sharp: claude schema, claude producer, and
still no usage. Across 4,036 real transcripts every assistant record carries `message.usage`
(131/131 on the sampled session), so this arm should never fire in practice. It covers truncation
and future upstream drift, and it fires loudly rather than silently.

**Edge case, decided:** `usage={}` is `PRESENT`. The channel existed and reported nothing. That is
a measured zero, and `_is_cache_hit({})` correctly returns `False`. Only a *missing or non-dict*
`usage` is `ABSENT_UNEXPECTED`.

`hermes.py` (the SQLite adapter) stamps `ABSENT_BY_SCHEMA` on every row. It constructs its own
rows and §2.1 establishes the fact as a property of storage, not a guess about intent.

### 4.2 The aggregate needs a counter, not an enum

The parser split does **not** remove this. The `agent` label comes from the ref, not the parser, so
`tool_stats` — keyed `(agent, call.name)` at `passive.py:106` — can still absorb rows from a real
Claude transcript (`PRESENT`) and from a trace export (`ABSENT_BY_EXPORT`) under one key. A scalar
`stats.usage_provenance` has no correct value in that bucket, and `Reducer` is a streaming
aggregator that "never stores a corpus-wide call list" (S11), so there is nothing to re-scan.

Provenance must travel *with the row* into the fold. The win from §4.1 is that it is now **stamped
by the parser class** rather than sniffed per row.

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

**All three absent arms increment `usage_missing`.** The distinction between them drives
diagnostics; it does not drive the cache flag, for which every flavour of absence means the same
thing: not measurable.

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

Four cases, exhaustive over `(cache_hits, usage_missing, calls)`. `"yes"` stays conditioned only on
`cache_hits > 0`: a single observed hit is a positive existence proof, and no amount of surrounding
blindness weakens it.

### 4.4 probe.py refuses at the door *and* at the invariant

`probe.py` measures per-turn token cost. TB-16 established that `output_tokens` is billed against
the API response, keyed by `requestId` (S26). A corpus that cannot be grouped by `requestId` does
not yield a degraded probe result — it yields a **wrong** one. There is no useful partial answer,
so there is no partial mode.

Two guards, deliberately redundant, because they defend different things.

**At the door**, `probe.py` starts routing through dispatch, which it does not do today:

```python
parser, stream = detect_parser(lines)
if parser.schema_tag == HermesTraceParser.schema_tag:
    raise NonIsolableTurns(
        "hermes --format trace carries no requestId; not valid probe input (S30). "
        "Use --format jsonl, or run probe against a native Claude transcript."
    )
```

**At the invariant**, `_turn_key` stops falling back:

```python
class NonIsolableTurns(RuntimeError):
    """Raised when turns cannot be keyed to the billing unit (S26)."""


def _turn_key(entry: dict[str, object]) -> str:
    """The unit `output_tokens` is billed against: the API response (S26)."""
    request_id = entry.get("requestId")
    if not (isinstance(request_id, str) and request_id):
        raise NonIsolableTurns("probe requires requestId to group turns (S26)")
    return f"req:{request_id}"
```

The door check buys a good diagnostic — it can name the format and suggest a fix. The invariant
check is the load-bearing one: it defends S26 for **any** corpus lacking `requestId`, whatever
parser claimed it and whatever producer wrote it. Gating the fallback behind a schema check alone
would make the TB-16 regression conditional rather than gone. The `ts` parameter is removed from
`_turn_key`'s signature.

This mirrors TB-15's lesson: a contaminated arm that still produces plausible numbers is more
dangerous than one that refuses.

`NonIsolableTurns` subclasses `RuntimeError`, matching `SeededReportError` (`probe.py:18`),
`UnknownSchema`/`AmbiguousSchema` (`adapters.py:39,48`), `NonTranscriptExport` (`sources.py:22`).

**Asymmetry, stated plainly:** `passive.py` degrades and annotates (`n/a`); `probe.py` refuses.
Same input, opposite policy, because omitting a caveat-only flag is survivable (S19 forbids that
flag from affecting ranking) while mis-grouping the billing unit corrupts the measurement itself.

---

## 5. New acceptance criteria

Next free IDs are S29 and S30 (`SPEC.md` currently ends at S28, line 178).

> **S29 — producer provenance for usage.** Schema and producer are separate axes. A transcript
> claimed by the claude schema is routed by producer: `version == "hermes-agent"` selects
> `HermesTraceParser`, otherwise `ClaudeParser`. The two claim predicates partition, so
> `AmbiguousSchema` never fires between them. Every `ToolCall` carries a `UsageProvenance` of
> `PRESENT`, `ABSENT_BY_SCHEMA`, `ABSENT_BY_EXPORT`, or `ABSENT_UNEXPECTED`, stamped by its
> producer. The passive cache-hit flag renders `n/a` when no call in a bucket could be measured,
> `n/a*` when only some could, and `no` only when usage was available and zero hits were observed.
> Per S19 the flag remains caveat-only and never affects ranking.

> **S30 — probe requires the billing unit.** `probe.py` groups turns solely by `requestId` (S26).
> It rejects `hermes-trace` input at dispatch, and `_turn_key` raises `NonIsolableTurns` on any
> entry lacking `requestId`. There is no timestamp fallback and no partial-corpus mode.
> `hermes sessions export --format trace` output is therefore valid input to `passive.py` and
> invalid input to `probe.py`.

---

## 6. Test plan

RED → GREEN → DOCS, per the project's TDD workflow.

**Fixtures.** A trace-shaped fixture derived from a real export (§2.2's 87-record file is the
natural source): `sessionId` present, `version: "hermes-agent"`, no `message.usage`, no `requestId`.

**Dispatch partition (S29)** — the core of the change:
- `HermesTraceParser` claims a trace line; `ClaudeParser` does not.
- `ClaudeParser` claims a real claude line; `HermesTraceParser` does not.
- `detect_parser` over the trace fixture returns `HermesTraceParser`, **not** `ClaudeParser`.
- A line satisfying both predicates is unconstructible; assert `AmbiguousSchema` still fires for a
  genuinely doubly-claimed line, so the guard is proven live rather than merely unfired.
- Regression: the existing "every real transcript claims at line 1" property still holds.

**Provenance (S29)**
- `ClaudeParser` on a real transcript → every call `PRESENT`.
- `HermesTraceParser` on the trace fixture → every call `ABSENT_BY_EXPORT`, and `malformed == 0`
  (the point is that it parses cleanly).
- `usage={}` → `PRESENT`, and `_is_cache_hit` returns `False`.
- Claude schema with `usage` stripped → `ABSENT_UNEXPECTED`. This arm must be tested even though it
  should never fire in the wild.
- `hermes.py` adapter → every call `ABSENT_BY_SCHEMA`.

**Render (S29)** — one test per arm of the four-case rule, driven through `Reducer`:
- hits > 0 → `"yes"`; usage present, no hits → `"no"`; all rows absent → `"n/a"`.
- **mixed bucket → `"n/a*"`.** Absorb a real-transcript session and a trace session under the same
  `(agent, tool)` key. This is the test that fails against a scalar-enum implementation, and it is
  why the counter exists. It must be **synthetic**: §2.2's export carries exactly one `tool_use`
  block, so no natural trace corpus can form a mixed bucket.

**probe refusal (S30)**
- `_turn_key` with `requestId` → `req:<id>`; without → raises `NonIsolableTurns`.
- `probe.py` against the trace fixture refuses at dispatch, names the format, exits non-zero, and
  emits **no** comparison table. Extends the `SeededReportError` guard: no table without real
  measurement.
- A synthetic *claude*-tagged corpus with `requestId` stripped still raises — proving the invariant
  guard is independent of the schema check, not shadowed by it.
- Regression: the existing 245-batch `requestId` grouping test still passes.

**Gate (contemporaneous at ship, 2026-07-09).** `uv run ruff check .`,
`uv run mypy --strict`, `uv run python -m unittest` — all green, counts reported.
Baseline then was 213 passing / 1 skipped. **Do not run that Gate today** —
`unittest discover` under-collects (TB-19); live command is `uv run pytest -q`
(~753 / 4 on the default install). See [`README.md` Quality gate](../../../README.md#quality-gate).

---

## 7. Risks

**Subclassing costs an `isinstance` tax.** `HermesTraceParser` **is-a** `ClaudeParser`, so
`tests/test_adapters.py:24,31,78,83` — four `assert isinstance(parser, ClaudeParser)` — keep passing
on trace input. They are *silently weakened*, not broken, which is the worst failure mode for a
test. Tighten them to `type(parser) is ClaudeParser`, or assert on `schema_tag`. Grep for
`isinstance(.*Parser` before starting; there are no such checks in `toolbench/` itself.

**A new enum field on `ToolCall` is a breaking change.** Every construction site must set it:
`parsers.py:137,170,190`, `hermes.py:176`, and every test fixture building a `ToolCall` directly.
Mitigation: **no default value.** A default would silently mark unconverted sites `PRESENT` —
reintroducing exactly the fabricated-certainty bug this ticket exists to kill. `mypy --strict` then
enumerates every site. The absence of a default is load-bearing, not an oversight.

Two mechanical consequences follow, and the implementer hits both on the first run:

1. **Field ordering is forced.** `ToolCall` ends with `no_result: bool = False` and
   `result_source: str | None = None`. A dataclass cannot place a non-defaulted field after a
   defaulted one, so `usage_provenance` must be *inserted* into the non-default block — naturally
   right after `usage`, which it annotates — not appended.
2. **Positional construction breaks.** Inserting mid-struct shifts every positional argument after
   `usage`. `rg 'ToolCall\('` before starting and convert those sites to keywords.

**`probe.py` gains a dependency on the dispatch layer.** It imports `parse_session` today and
nothing else. Routing through `detect_parser` couples it to `adapters.py`. This is acceptable —
`probe.py` bypassing dispatch is precisely why it was unprotected — but it is a new edge in the
module graph and `transcript.py:98` already notes an import-cycle hazard in this neighbourhood.
Check for a cycle before wiring it.

**`n/a*` is an unusual token in a markdown table column.** It needs a legend line wherever the
table is rendered, or it reads as a footnote marker pointing nowhere.

**Open — should `ABSENT_UNEXPECTED` also emit an operator warning?** The typed field makes the
*data* honest; a warning would make the *operator* aware. Deferring: additive, no type change, and
better decided once we see whether the arm ever fires.

---

## 8. Out of scope, discovered during design — file separately

**`hermes.py:63` cannot read one of the four archive databases.** `_connect` opens
`file:{db}?mode=ro`. `~/.hermes/profiles/aphrodite-mood/state.db` has no `-wal`/`-shm` sidecar, and
SQLite cannot open a WAL-mode database read-only without creating the shared-memory file, so the
connection raises `OperationalError: unable to open database file`. That profile — 2,006 messages —
is invisible to the hermes adapter today.

A live bug, unrelated to TB-18; it should get its own ticket. Two cautions:

- `immutable=1` opens the file but **ignores the WAL and returns stale data.** On
  `tech-interviewing/state.db` it reports 639 messages where `mode=ro` reports 644. Not a drop-in
  fix.
- The comment at `hermes.py:62` ("a running hermes owns this file. Never open it writable") is
  correct and must be preserved by any fix.

This deepens the under-sampling already noted in `hermes.py`'s module docstring (89 sessions via
`session list` vs 789 via `agentsview stats`).
