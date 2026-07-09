# TB-5: passive.py reducer + report + CLI — Plan

SPEC: S11, S12, S13, S14, S15, S19, S23 · BUILDPLAN anchor T4
Depends on: T2 (transcript.py, merged), T3 (sources.py, merged) — both present on
`origin/integration/substrate`, the base of this branch.

## Base state

Working against `origin/integration/substrate` (40 tests green, contains
`toolbench/transcript.py` from TB-3 and `toolbench/sources.py` from TB-4).
`toolbench/passive.py` currently a stub (`main() -> None: print(...)`) left by
TB-2. This ticket replaces the stub. Do not touch `transcript.py`, `sources.py`,
or `probe.py`.

## Wiring `passive.py` onto the existing substrate

- `transcript.parse_session(path, *, agent, source, project) -> ParseResult`
  takes a filesystem path only — it has no "parse from lines" entry point.
- `sources.SessionRef` from AgentsView has `path=None`; only raw-discovered
  refs carry a real path.
- **Decision:** a `_parse_ref(ref, runner)` adapter in passive.py branches:
  raw refs (`path` set) call `parse_session(ref.path, ...)` directly;
  AgentsView refs (`path=None`) stream `open_session_jsonl(ref, runner=...)`
  into a `tempfile.NamedTemporaryFile`, call `parse_session(tmp_path, ...)`,
  then unlink the temp file. This keeps `transcript.py`/`sources.py`
  untouched while giving both source kinds a uniform parse path. Hermetic in
  tests (fake runner writes into the temp file same as it would in `sources.py`'s
  own tests).
- `sources.py`'s private default runner (`_run_agentsview`) is not imported.
  passive.py's `runner` param defaults to `None`; when `None`, calls into
  `iter_sessions`/`open_session_jsonl` omit the `runner=` kwarg entirely so
  those modules fall back to their own defaults. Avoids reaching into a
  leading-underscore symbol across module boundaries.

## S11 — incremental reducer (the audited invariant)

`Reducer` is a dataclass holding only aggregates:
- `sessions_scanned: int`, `calls_joined: int`, `malformed_total: int`
- `agents: dict[str, AgentStats]` (per-agent totals)
- `tools: dict[tuple[str, str], ToolStats]` (per agent+tool totals — S14 §2)
- `inefficiency: InefficiencyCounters` (S14 §3 counters)

`Reducer.absorb(agent, parse_result)` is called once per parsed session. It
iterates `parse_result.calls` (a **per-session** list — already returned by
`parse_session`, unavoidable and fine) purely to fold each `ToolCall` into the
counters above, then returns; the list goes out of scope with the caller's
stack frame. `Reducer` itself never stores a list of `ToolCall`, so no
corpus-wide list ever accumulates regardless of corpus size.

**Self-review check:** no field on `Reducer`/`AgentStats`/`ToolStats` is typed
`list[ToolCall]` — a unit test introspects `dataclasses.fields()` to assert
this structurally (not just "seems fine by inspection"), so a future
regression (e.g. someone appending calls to a list "for the report") fails
the suite immediately.

## S12 — CLI

`argparse` with: `--agent` (default `"all"`), mutually-exclusive `--all` /
`--project` (default scope resolves to all-projects when `--project` is
omitted — satisfies "default scope `--agent all --all`"), `--since`,
`--date-from`, `--date-to`, `--out`, `--limit`, `--exclude-subagents`,
`--index-source {auto,agentsview,raw}` (default `auto`), `--verbose`.

`--since` is passed straight through to `sources.iter_sessions` (file-mtime
filter, exactly as documented in `sources.py`/S15). `--date-from`/`--date-to`
are a **separate** filter applied after parsing, per `ToolCall.ts` (the
session-recorded timestamp), date-string range `[from, to]` inclusive,
tested independently of `--since`. This matches SPEC listing them as
distinct flags alongside `--since`.

`--limit` bounds the number of *sessions* discovered (break out of the
discovery loop once reached), not tool calls — keeping with S11's
memory-bounded, streaming design and S25's `--all --limit 200` smoke.

**Testing hook (documented deviation):** `main()`/`_discover_refs()` accept a
`root: str = "~/.claude/projects"` keyword not exposed as a CLI flag (SPEC's
S12 flag list has no `--root`). Tests call `main(argv, runner=fake, root=tmp)`
directly to point at a temp raw root hermetically — this is the "argv + tmp
roots" testing shape called out in the ticket body. Real CLI usage always
gets the SPEC-default root.

## S13 — subagents

Included by default. `--exclude-subagents` filters discovered `SessionRef`s
whose `path` contains `/subagents/` (raw-sourced refs only, matching SPEC's
"paths containing `/subagents/`" wording — AgentsView refs have no path and
are not filtered by this flag, a known limitation of the current
`SessionRef` shape, not something to invent new filtering for here).

## S14 — four report sections, in order

1. **Agent breakdown** — table of per-agent sessions/calls/output_tokens/
   input_tokens/errors/no_result.
2. **Tool leaderboard** — per (agent, tool), **sorted descending by
   `output_tokens`** (S19 primary ranking = context cost = joined
   result-payload tokens). Includes a `cache_assisted` yes/no column that is
   caveat display only — it is never part of the sort key.
3. **Inefficiency callouts** — ToolSearch/deferral tax (call count + token
   cost), failures (count), oversized outputs (count, threshold constant
   `OVERSIZED_OUTPUT_TOKENS`), subagent fan-out (count of calls to
   agent-spawning tool names), churn (consecutive same-tool retries where the
   prior call also errored/had no result — computed streaming, per session,
   inside `absorb`, never needing cross-session state).
4. **Summary** — totals + the S15 provenance block (index source, sessions
   scanned, calls joined, malformed count, subagents-included flag,
   AgentsView fallback reason or "none", skipped roots or "none", and the
   fixed note that `--since` is file-mtime based).

Rendered as literal markdown headers (`## Agent Breakdown`, `## Tool
Leaderboard`, `## Inefficiency Callouts`, `## Summary`) so tests can assert
both presence and order via substring index comparison, and provenance lines
use stable, greppable prefixes (`Index source:`, `Sessions scanned:`, etc.)
for direct string assertions.

## S19 — metric roles

Tool leaderboard sort key is `ToolStats.output_tokens` only. `cache_hits` is
tracked per tool (derived from `usage.cache_read_input_tokens` /
`cache_creation_input_tokens` when present and truthy) but only ever
displayed, never used in `sorted(..., key=...)`. Failures/no-result/churn
are tallied into `InefficiencyCounters` and the per-tool `errors` column
only — neither feeds the leaderboard order.

## S23 — error/exit contract

- Any selection yielding zero joined tool calls → print a clear one-line
  message to stdout, return exit code `0`.
- `--index-source raw` (or `agentsview`) with a missing/erroring source →
  the underlying `FileNotFoundError`/`RuntimeError` from `sources.py`
  propagates out of discovery, `main()` prints a fatal message to stderr and
  returns `1`.
- `--index-source auto` (default, paired with default `--agent all`): if
  AgentsView is unavailable *and* the raw fallback's root is also missing,
  the `FileNotFoundError` from the raw fallback is caught (not fatal),
  recorded as a skipped root, and execution continues — which, with zero
  refs discovered, lands on the exit-0 "no sessions matched" branch, whose
  message also lists the skipped root(s). This is the "continues with other
  sources and reports skipped roots" behavior for `auto`, scoped to the
  current single-root raw scanner (BUILDPLAN G3 notes multi-root raw
  adapters are deferred — nothing here invents new per-agent raw roots).

## Tests (`tests/test_passive.py`, new file)

- Reducer: absorb accumulates per-agent/per-tool counters across multiple
  `ParseResult`s; error/no_result/cache/ToolSearch/oversized/subagent/churn
  counters each individually; structural no-corpus-list assertion via
  `dataclasses.fields(Reducer)`.
- CLI: default scope, `--agent`, mutually exclusive `--all`/`--project`
  (`SystemExit`), `--since` vs `--date-from`/`--date-to` distinctness,
  `--out`, `--limit`, `--exclude-subagents`, `--index-source` choices,
  `--verbose`.
- Subagent path filter on discovered refs.
- Report: four section headers present in order; provenance lines present;
  leaderboard ranked by `output_tokens` regardless of call count / cache
  flag / errors.
- Exit contract: `main()` with a fake runner + tmp roots covering (a) strict
  raw missing root → 1, (b) empty selection → 0 with message, (c) auto with
  agentsview unavailable + raw root missing → 0 with skipped-root message,
  (d) an end-to-end raw-mode run over `tests/fixtures/sample.jsonl` copied
  into a tmp project dir → 0 with a full four-section report.

All new tests are hermetic: fake `agentsview` runner (matching
`test_sources.py`'s `FakeRunner` pattern), `tempfile`/`TemporaryDirectory`
roots, no real `~/.claude`, no daemon.

## Deviations flagged for the reviewer

1. `_parse_ref`'s temp-file bridge for AgentsView-sourced sessions (no
   existing "parse from lines" API on `transcript.py`).
2. `root` kwarg on `main()` for hermetic testing, not a CLI flag.
3. `SUBAGENT_TOOL_NAMES` heuristic (`{"Agent", "Task"}`) for fan-out
   detection — no SPEC-given canonical tool name for subagent spawns.
4. `--agent` filtering is a no-op under `--index-source raw` because
   `sources._raw_session_refs` doesn't accept/filter by `agent` at all
   (pre-existing, out of scope to change here per guardrails).
