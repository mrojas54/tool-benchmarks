# tool-benchmarks

A re-runnable harness that analyzes **tooling inefficiencies across agent
systems** — Claude Code, Codex, Hermes, and other
[AgentsView](https://github.com/)-supported runtimes — from their on-disk
session transcripts. The goal is evidence for where agent work wastes
context, time, retries, or tool calls, emitted as a single markdown report.

Builds on the native-tool-vs-Bash benchmarking methodology from claude-mem
observation #8376.

## What it measures

1. **Cross-agent tool cost** — which tools, agents, projects, and workflows
   dump the most context back into sessions.
2. **Tooling inefficiency patterns** — repeated failed calls, slow tools,
   edit churn, retry loops, context pressure, subagent fan-out.
3. **Deferral / discovery tax** — what deferred-tool loading and searching
   (e.g. `ToolSearch`) costs across Claude Code, Codex, and Hermes.
4. **Controlled tool-vs-shell probes** — for comparable local tasks, when
   native tools (Grep/Glob/Read) are cheaper or more reliable than shell
   commands.

The primary metric is **context cost** = joined tool-result payload tokens
(`chars / 4`). Cache flags are caveat-only and never rank tools; failure /
slow / retry-churn feed the inefficiency callouts only.

## Scope guards (non-goals)

- **Markdown only** — no HTML report (the `session-report` skill owns that).
- **No live token-API calls** — all numbers derive from on-disk transcripts.
- **Read-only** — never mutates transcripts or the probe corpus's source
  projects.
- **Python standard library only** — no third-party runtime dependencies, so
  the harness runs anywhere `python3` exists.
- **No web-chat benchmarking** — local/agentic surfaces with inspectable
  sessions only.

## Architecture

```
raw roots + AgentsView exports
          │
   loaders (sources.py)  ── acquisition: bytes → lines
          │
   parsers (parsers.py)  ── interpretation: lines → ToolCall
          │
   adapters (adapters.py + registry.py)  ── SessionRef → ParseResult
          │
     ┌────┴─────┐
 passive.py   probe.py
     └────┬─────┘
   reports/YYYY-MM-DD-tool-usage.md
```

### Three layers, one seam (TB-13)

Acquisition and interpretation are orthogonal, so they are separate ABCs:

- A **`SessionLoader`** turns a `SessionRef` into lines. It owns the NUL sniff,
  which therefore runs *before* schema detection — a SQLite dump has no first
  JSON line to detect.
- A **`TranscriptParser`** interprets already-acquired lines. It never opens a
  file and never decides which schema it is looking at.
- A **`SessionAdapter`** composes the two. `registry.pick_adapter` walks an
  ordered list of `claims(ref)` predicates and returns the first match.

**Hermes SQLite is keyed on source; everything else is keyed on content.**
Hermes archive sessions claim by `agent == "hermes" and path is None` — a
SQLite read with no lines. Every other session is content-sniffed over a
bounded 100-line window, because schema is a property of the payload, not of
the producer: `cowork` emits Claude's exact schema and parses with zero
registry entries of its own. A `hermes sessions export --format trace` file
is also content-sniffed: it speaks Claude's shape but declares
`version == "hermes-agent"`, so `HermesTraceParser` claims it (S29) rather
than letting `ClaudeParser` swallow a usage-less export as a measured zero.

**No parser is the default.** An unrecognized transcript raises `UnknownSchema`
(a `RuntimeError`, so `passive.main`'s existing per-session guard demotes it to
`skipped_roots`). Previously such a session fell through to the Claude parser,
matched nothing, and reported a healthy zero. `codex` is now claimed by
`CodexParser`, which joins its three paired `response_item` shapes —
`function_call`, `custom_tool_call`, and `tool_search_call` — on `payload.call_id`
(S33 / TB-12). `cursor` still lands in `skipped_roots`, pending a parser of its own.
codex's `web_search_call` has no `call_id` and no output record, so it is not
joined as a call; instead it is counted in `ParseResult.unjoinable` and named in the
Summary (`Unjoinable tool records`), so codex's ~4% web-search undercount is surfaced
rather than silently absent (S38 / TB-24).

- **`transcript.py`** — the schema-neutral records: `ToolCall` (with
  `UsageProvenance`), `ParseResult` (optional `session_cache_read_tokens`),
  and `result_len`. `parse_session` remains as a compat shim over
  `ClaudeParser`. Stray non-UTF-8 bytes decode with `errors="replace"` so one
  bad byte never aborts the session (TB-10).
- **`parsers.py`** — one class per schema. `ClaudeParser` joins each assistant
  `tool_use` block to its result by id. `HermesTraceParser` subclasses it for
  the claude-shaped hermes trace export and stamps every call
  `ABSENT_BY_EXPORT` (S29). Malformed lines are counted and skipped, never
  fatal.
- **`adapters.py`** — `detect_parser`, `UnknownSchema`, `AmbiguousSchema`, and
  `ComposedAdapter` (the terminal fallback). `PARSERS` currently holds
  `ClaudeParser` and `HermesTraceParser`; their `claims_line` predicates
  partition on `version`.
- **`registry.py`** — the ordered adapter list and `pick_adapter`. Exists to
  break the `hermes.py` ↔ `adapters.py` import cycle. Adding an agent means
  adding an entry here, never editing a dispatcher.
- **`sources.py`** — multi-agent discovery plus the loaders. Either scans raw
  local transcript roots or pages the AgentsView CLI (`--index-source auto |
  agentsview | raw`). `auto` tries AgentsView first and falls back to raw
  scanning, recording the reason. Exports that are not JSONL (e.g. a SQLite
  dump with a NUL in the header) raise `NonTranscriptExport` and are skipped
  by name (TB-10).
- **`hermes.py`** — direct read-only SQLite adapter for Hermes sessions
  (TB-11). Discovery still comes from AgentsView; only the read is redirected
  because `session export` returns the whole default-profile database.
  Per-call usage is `ABSENT_BY_SCHEMA`; session-row `cache_read_tokens` is
  surfaced on `ParseResult` for the Agent Breakdown caveat (S32 / TB-20),
  never attributed per call.
- **`passive.py`** — streams and aggregates **incrementally** (per-agent /
  per-tool reducers only, never a whole-corpus `list[ToolCall]`), then emits
  a five-section report: agent breakdown (plus optional session-grain cache
  caveats), tool leaderboard (`cache_assisted` as `yes` / `no` / `n/a` /
  `n/a*`), model breakdown, inefficiency callouts, summary.
- **`probe.py`** — scores matched tool-vs-Bash probe pairs from a dedicated
  session JSONL and emits a context-token + usage comparison table under
  `reports/`. Tool arms match structurally (name + corpus target); bash arms
  match by sentinel. Usage is attributed only when the API response is
  isolable (one `tool_use`, no prose/reasoning — S26). Turns are keyed solely
  by `requestId` (S30); hermes-trace input is refused with `NonIsolableTurns`.

## Probe corpus

Five files are vendored under [`tools/`](tools/) — a log-spaced size spread
(~121 → ~2,242 lines) so the tool-vs-Bash comparison shows how context cost
scales with target size:

| File | Lines |
|------|-------|
| `regex_check.py` | 121 |
| `mcp.py` | 352 |
| `monitor.py` | 768 |
| `llm_extraction.py` | 1,332 |
| `code_analysis.py` | 2,242 |

They are committed so probes re-run from a clean checkout with no external
absolute paths. Probe *output* lands in `reports/`, kept separate from these
inputs.

## Status

**Implemented.** `toolbench/` ships all of tickets **T1–T6** in
[`BUILDPLAN.md`](BUILDPLAN.md): the scaffold, the transcript parser, the
multi-agent source layer, the passive analyzer, and the active probes.
Post-merge hardening covers **TB-8** (subagent `--project` filter), **TB-9**
(callout denominators), **TB-10** (non-UTF-8 / non-transcript exports),
**TB-11** (Hermes SQLite direct read — discovery still via AgentsView),
probe isolability (**S26** / TB-14–16), schema dispatch (**S27–S28** /
TB-13), usage provenance (**S29–S30** / TB-18: producer-aware
`UsageProvenance` on every `ToolCall`, and `probe.py` refusing corpora it
cannot key to the billing unit), the gate itself running every test
(**S31** / TB-19: the documented command is `pytest`, not `unittest
discover`, which silently missed 37 module-level tests), and session-grain
Hermes cache surfacing (**S32** / TB-20: Agent Breakdown caveat, never
folded into the per-call `cache_assisted` column), and the codex schema
(**S33** / TB-12: `CodexParser` joins three paired `response_item` shapes on
`payload.call_id`, recovering ~3,100 calls across 93 sessions that the
Claude-only parser had reported as a healthy zero). The strict gate
(`ruff check .`, `mypy --strict toolbench tests`, `pytest`) is green —
**283** tests passing (1 skipped when the live hermes archive is absent).
`mypy --strict` covers `tests` as well as `toolbench`, and passes on both:
before TB-12 the `tests` tree carried 38 `no-untyped-def` errors, so the
documented gate had never actually been green.

Source-of-truth documents:

- [`SPEC.md`](SPEC.md) — 33 numbered acceptance criteria (S1–S33).
- [`EVALUATION.md`](EVALUATION.md) — verification map for every criterion.
- [`BUILDPLAN.md`](BUILDPLAN.md) — decided architecture and the T1–T6 tickets.
- [`docs/2026-07-07-tool-benchmarks-design.md`](docs/2026-07-07-tool-benchmarks-design.md)
  — full v2 design spec.
- [`protocols/active-probes.md`](protocols/active-probes.md) — probe corpus,
  arm matching (S17), isolability (S26), and the seeded `#8376` baseline table.
- [`protocols/probe-run-sheet.md`](protocols/probe-run-sheet.md) — executable
  ten-turn operator run sheet for scoring a fresh probe session.

## Agents / targets

Three source adapters. The first two are selected per-session by
`--index-source`; the third is selected by agent.

- **Claude Code raw transcripts** — scans on-disk JSONL session files
  directly under a root (default `~/.claude/projects`).
- **AgentsView** — pages the `agentsview` CLI for any AgentsView-registered
  runtime (Claude Code, Codex, Hermes, …), yielding one `SessionRef` per
  session with cursor-based pagination.
- **Hermes SQLite** — reads hermes sessions straight from `~/.hermes`
  (`$HERMES_HOME` overrides). `agentsview session export` returns `rc=0` and
  streams the whole 37 MB default-profile database for every hermes session
  instead of that session's transcript, so hermes contributed zero tool calls
  until this adapter landed (TB-11). Per-call usage is absent by schema; when
  the session row carries `cache_read_tokens`, that appears as an Agent
  Breakdown caveat only (S32).

A fourth path is content-detected, not selected by `--index-source`:
`hermes sessions export --format trace` JSONL is claimed by
`HermesTraceParser` (S29). Valid for `passive` (`cache_assisted` → `n/a`);
refused by `probe` (`NonIsolableTurns`, S30).

Hermes **discovery** still comes from AgentsView; only the read is redirected.
The corpus is *defined* as what `agentsview session list` returns, and every
agent is sampled through that one path. Enumerating the hermes archive here
would redefine the corpus for a single agent and skew every cross-agent rate.

**Known limitation.** Hermes is under-sampled. `agentsview session list --agent
hermes` reports 89 sessions while `agentsview stats --agent hermes` reports 789
from the same archive — one binary, two subsystems, an 8.9× disagreement. That
is an upstream defect
([kenn-io/agentsview#1048](https://github.com/kenn-io/agentsview/issues/1048)),
not a curation to work around by forking discovery into one adapter. The export
bug this adapter exists for is
[#1047](https://github.com/kenn-io/agentsview/issues/1047).

## Usage

The project is [uv](https://docs.astral.sh/uv/)-managed (`pyproject.toml` +
`uv.lock`, empty runtime deps, `dev` group `ruff`/`mypy`/`pytest`).

```sh
# Passive analyzer — default scope is every agent, every project
uv run python -m toolbench.passive --agent all --all

# Scope by project / time / index source
uv run python -m toolbench.passive --project my-repo --since 2026-06-01
uv run python -m toolbench.passive --all --index-source agentsview
uv run python -m toolbench.passive --all --date-from 2026-06-01 --date-to 2026-06-30
uv run python -m toolbench.passive --all --exclude-subagents --out reports/2026-07-08-tool-usage.md

# Reproducible before/after: freeze the corpus once, then replay it to compare.
# First run writes the manifest; every later run scans the frozen set and names
# what has vanished since (TB-22).
uv run python -m toolbench.passive --all --freeze reports/corpus.manifest   # writes
uv run python -m toolbench.passive --all --freeze reports/corpus.manifest   # replays

# Active tool-vs-Bash probes. Score a dedicated probe session; without
# --session every arm is seeded and the report is refused (SeededReportError).
# Operator run sheet: protocols/probe-run-sheet.md (ten arms, ten turns).
uv run python -m toolbench.probe --session /path/to/probe-session.jsonl --out reports/active-probe-comparison.md
uv run python -m toolbench.probe --allow-seeded   # baseline table only; measures nothing

# Tests
uv run pytest -q
```

### Probe scoring pitfalls

- **Fresh session only.** Mentions of sentinels, the run sheet, or
  `toolbench/probe.py` are discarded as contamination (`MENTION_MARKERS`).
- **One tool call per API response.** Usage is keyed by `requestId` (S26 /
  S30). Batching, prose, or reasoning in an arm turn blanks the usage column
  (`—`) while keeping real context tokens — it does **not** re-seed the cell.
  There is no timestamp fallback; a corpus without `requestId` (including
  `hermes sessions export --format trace`) raises `NonIsolableTurns`.
- **Usage columns are not yet comparable (TB-17).** `output_tokens` bills
  the whole emitted `tool_use` block. The bash arm must carry a sentinel
  comment (and often a `description`) that the tool arm cannot carry, so
  bash usage is inflated by ~15–20 tokens of instrumentation — enough to
  swamp the measured gap. Trust the context-token columns; do not conclude
  "MCP is cheaper on output tokens" from the usage pair until TB-17 lands
  a stated correction (or drops those columns).
- **Turn 0 before arms.** Confirm serena has an active project with a
  non-corpus target (`pyproject.toml`) so a failed arm call is not scored as
  a successful match.
- **Native Claude transcript only.** Score a Claude Code probe session, not
  a hermes trace export — `passive` accepts both; `probe` does not (S30).

### `--index-source` policy

- `auto` (default) — tries AgentsView first; on failure, falls back to
  scanning the raw root directly and records the fallback reason in the
  report's Summary section.
- `agentsview` — AgentsView only; a source error is fatal.
- `raw` — raw local transcript roots only; a source error is fatal.

`--agent` filters AgentsView listing only. Under `--index-source raw` the
discovery root is Claude Code sessions, so `--agent` is a no-op there.

`--project` matches the **owning project directory** under the raw root
(first path segment after the root), not `path.parent.name`. Nested
subagent transcripts at `<project>/subagents/*.jsonl` therefore survive
`--project` and are only dropped when you pass `--exclude-subagents`.

### Corpus reproducibility (`Corpus fingerprint` + `--freeze`)

The corpus is a moving target: claude-mem observer transcripts age out of a
~30-day sliding window *mid-scan*, so its tail deletes itself at roughly re-run
cadence, and the live session appends calls while it is read. Two reports are
therefore not automatically diffable — a delta between them may be the corpus
moving, not your code (TB-22).

- **`Corpus fingerprint: <digest> (<N> sessions scanned)`** (always emitted in the
  Summary, S36) is a hash over the *scanned* set — the sessions that produced the
  numbers. It folds each session's identity **and** every content count the Summary
  renders (calls, malformed lines, unjoinable records), so a vanished tail (an id
  leaves the set) and any kind of append (a new call, a malformed line, or a
  `web_search_call`) all move it. **Two reports whose fingerprints match are
  diffable; if they differ, do not attribute the delta to code** until you know why
  the input set moved.
- **`--freeze <manifest>`** (S37) makes a before/after actually reproducible. The
  first run writes the discovered ref list to the manifest; every later run
  *replays* it — scanning exactly the frozen set instead of re-discovering — and
  reports `(<V> vanished since freeze)` for refs whose transcripts have since been
  deleted (`--verbose` names them). Over an unchanged corpus a replay is
  byte-identical; when the tail has moved, the vanished count names the mechanism
  instead of letting it masquerade as a code effect.

The fast test suite is hermetic — it fakes the `agentsview` CLI, points
`$HERMES_HOME` at a fixture database, and never touches `~/.claude` or
`~/.hermes`, so the inner loop never depends on a live daemon. One test in
`tests/test_hermes.py` reads the real hermes archive to pin the schema
compatibility envelope, and skips when that archive is absent.

Hermes databases are always opened `file:…?mode=ro`. A running hermes owns
those files; the adapter never writes to them.

### Reading the report

Inefficiency callouts are written as `N of M calls (P%)` and name the
worst tool when the count is non-zero, for example:

```text
- Failures: 147 of 997 calls (14.7%); top: Bash (109)
- ToolSearch/deferral tax: 12 of 997 calls (1.2%), 3400 tokens
```

Ties for "top" break alphabetically so the report stays deterministic. A
zero count omits the top-offender clause.

The Tool Leaderboard's `cache_assisted` column is caveat-only (S19) and
uses four values (S29): `yes` (a hit was observed), `no` (usage was
measurable and zero hits), `n/a` (usage unavailable for every call in the
bucket — e.g. hermes SQLite or hermes-trace), `n/a*` (mixed). Neither
`n/a` form is a measured zero.

When Hermes sessions carry session-row `cache_read_tokens`, the Agent
Breakdown adds a caveat line such as `M of N sessions carry session-grain
cache_read_tokens > 0` (S32). That signal is never divided into a per-call
rate and never mixed into `cache_assisted`.

### Troubleshooting / common pitfalls

| Symptom | Likely cause | What to do |
|---|---|---|
| Summary `scanned` is far below the corpus size | Not a bug: many discovered sessions skip (dead index entries, parser gaps) | Read the `Sessions discovered: D / scanned: M / skipped: K` line and the `Skipped by reason` histogram — they reconcile the gap by typed reason (S34/S35). `scanned` was never the corpus size. |
| Run dies with `UnicodeDecodeError` | Pre-TB-10 behavior, or a custom strict-decode runner | In-tree readers use `errors="replace"`. One bad session should be counted under **Skipped by reason** as `decode_error`, not abort the corpus. |
| Summary counts a `non_transcript` skip for a non-Hermes session | AgentsView `session export` returned binary with returncode 0 | Expected for off-contract exports; the NUL sniff rejects them before parse (TB-10). Run with `--verbose` to name the session. Hermes sessions should not hit this path — they route through `hermes.py`. |
| `--project X` silently omits every subagent | Pre-TB-8 filter matched `path.parent.name` (`subagents`) | Current code matches the owning project dir. Re-run on current `main`. |
| Callouts are bare integers (`Failures: 865`) | Pre-TB-9 report formatting | Current callouts include denominators and a top offender. |
| `--agent hermes` yields far fewer sessions than expected | AgentsView `session list` under-counts Hermes vs `stats` (~89 vs ~789) | Known upstream limit ([#1048](https://github.com/kenn-io/agentsview/issues/1048)); discovery is intentionally not forked into the Hermes adapter (S9b). |
| Hermes session skipped / archive not found | `$HERMES_HOME` / `~/.hermes` missing, or session only in an unread profile DB | Confirm `HERMES_HOME`; it counts under the `non_transcript` reason and `--verbose` names the session. Profile DBs under `profiles/*/state.db` are searched. |
| `Malformed lines` explodes into the hundreds of thousands | Binary export absorbed as text (would happen without the NUL sniff) | Should not occur on current code — binary payloads are rejected before parse. |
| Empty selection message | No sessions matched filters, or all matched sessions were skipped | Check `--project` / `--since` / `--date-*`, and the `(skipped K: reason=count)` suffix on the message; `--verbose` names each session. |
| `toolbench.probe` without `--session` refuses to write | Seeded-only report is blocked (`SeededReportError`) | Pass `--session PATH`, or `--allow-seeded` for the baseline table only. |
| Probe usage column shows `—` but context tokens are real | Arm matched, but the API response was not isolable (prose, thinking, or batched `tool_use` — S26) | Re-run from [`protocols/probe-run-sheet.md`](protocols/probe-run-sheet.md); one tool call per turn, no surrounding prose. |
| Bash usage looks ~15–20 tokens higher than the tool arm | Sentinel + optional Bash `description` are billed into bash `output_tokens` only (TB-17) | Expected until TB-17. Compare context-token columns; treat usage as non-comparable. |
| `cache_assisted` shows `n/a` / `n/a*` for hermes (or hermes-trace) | Per-call usage is absent by schema or dropped by the trace export (S29) | Expected. Do not read `n/a` as "no cache hits". Session-grain cache, when present, appears as an Agent Breakdown caveat (S32), not in this column. |
| `toolbench.probe` raises `NonIsolableTurns` on a hermes trace file | Trace export has no `requestId`; probe keys turns only by that field (S30) | Score a native Claude Code probe session instead. Trace remains valid input to `passive`. |
| `cursor` sessions appear only under the `unknown_schema` skip reason | No parser claims cursor's schema yet (`UnknownSchema`, S28) | Expected until a `CursorParser` lands. It must not appear as a healthy zero-call agent; `tally_skips`/`--verbose` surface the count and ids (S34). |
| `cache_assisted` shows `n/a` for every `codex` tool | codex has no per-call usage channel; it bills per turn via `token_count` events (`ABSENT_BY_SCHEMA`, S33) | Expected. Do not read `n/a` as "no cache hits". |
| `codex` reports 0 errors no matter what failed | codex encodes exit status in the output text and sets `status: completed` even for failed tools (S33) | Expected. `error` is never inferred from output prose. Use `output_chars` / the raw transcript to inspect failures. |
| `codex` web searches never appear in the leaderboard | `web_search_call` carries no `call_id` and emits no output record, so it cannot be joined (S33 / TB-24) | Expected. They are not joinable calls, so leaderboard/ratio counts exclude them. The count is not lost: the Summary's `Unjoinable tool records (seen, not joined)` line names it as `codex/web_search_call` (S38). |

## Quality gate

Before any PR: `uv run ruff check .`, `uv run mypy --strict toolbench tests`,
and the full `pytest -q` suite must be green (S31 — the documented command
must collect every test, including module-level `test_*` functions that
`unittest discover` silently misses).
