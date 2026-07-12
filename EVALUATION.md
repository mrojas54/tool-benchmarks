# tool-benchmarks — EVALUATION

One row per SPEC criterion, each tagged by how it is verified:

- `autonomous` — a hermetic test proves it; the Result Validator can read it
  from the PR diff + source.
- `operator-assisted` — needs a human to drive a real flow (real transcripts,
  the live AgentsView daemon).
- `external-oracle` — depends on an external system (AgentsView CLI /
  `~/.claude/projects` shape) that the harness cannot fully fake.
- `felt` — a judgment call about readability / usefulness.

## Harness commands

- **`test` (fast, hermetic, parallel, ≤60s)** — `uv run pytest -q`. The
  delegators' inner-loop clock. No daemon, no `~/.claude` access; fixtures +
  fake `agentsview` runner only. Collects `unittest.TestCase` methods and
  module-level `test_*` functions uniformly (S31) — `unittest discover`
  silently missed the latter (37 of 220 tests, TB-19) and is no longer the
  documented command.
- **`test:full` (slow, real corpus / daemon)** — the S25 smoke commands run
  against a real `~/.claude/projects` and, where healthy, the live
  AgentsView daemon. Not hermetic; operator-run. Tests that read a live
  archive are gated on `TOOLBENCH_LIVE=1` and skip out of the fast suite;
  run them with `TOOLBENCH_LIVE=1 uv run pytest -q`.
- **lint / types** — `uv run ruff check .`; `uv run mypy --strict toolbench
  tests`.

## Criteria map

| ID | Verification | Tag | How |
|----|--------------|-----|-----|
| S1 | id-join over fixtures (both key locations) | `autonomous` | `test` |
| S2 | block-local vs top-level payload precedence | `autonomous` | `test` (block-local fixture) |
| S3 | `result_len` over 4 shapes | `autonomous` | `test` |
| S4 | `ToolCall` field set (incl. `model`) + derived props | `autonomous` | `test` |
| S5 | malformed counted + skipped | `autonomous` | `test` |
| S6 | interrupted kept, `output_chars=0` | `autonomous` | `test` |
| S7 | raw discovery filters (owning project dir, nested subagents) + FileNotFoundError | `autonomous` | `test` (tmp tree) |
| S8 | AgentsView cursor pagination + `SessionRef` | `autonomous` | `test` (fake runner) |
| S9 | uniform open; lenient decode; reject binary / non-transcript exports | `autonomous` | `test` (fake runner + real bytes) |
| S9a | hermes direct SQLite read (`parse_hermes_session`, mode=ro) | `autonomous` / `operator-assisted` (live archive) | `test` (`test_hermes.py`) |
| S9b | hermes discovery stays on AgentsView `session list` | `autonomous` (routing) / `external-oracle` (list vs stats) | `test` + live AgentsView |
| S10 | auto/strict/raw index-source behavior + fallback reason | `autonomous` (logic) / `external-oracle` (live) | `test` + `test:full` |
| S11 | incremental — no whole-corpus list | `autonomous` (reducer unit) / `operator-assisted` (mem at scale) | `test` + `--all --limit 200 --verbose` |
| S12 | CLI arg parsing / defaults | `autonomous` | `test` |
| S13 | subagent include/exclude path filter | `autonomous` | `test` (`test_sources.py` real nested layout `<project>/<session-uuid>/subagents/` sets `is_subagent`, and `--exclude-subagents` is asserted on the FILTERED refs, not on the flag — TB-29: the old fixture built a flat `<project>/subagents/` that exists nowhere on disk, so the suite ratified a no-op) |
| S14 | five report sections; callouts carry denominators + top offender | `autonomous` | `test` (report string) |
| S15 | report provenance fields present (incl. skipped roots) | `autonomous` | `test` (report string) |
| S16 | exact 5 corpus paths listed | `operator-assisted` | inspect `active-probes.md` vs real dir |
| S17 | structural tool-arm match + bash sentinel; contamination guards | `autonomous` | `test` (probe fixtures) |
| S18 | comparison table + seeded fallback + SeededReportError | `autonomous` | `test` |
| S19 | context-cost ranking; cache caveat-only | `autonomous` | `test` |
| S20 | stdlib runtime; uv project shape | `autonomous` | `test` + import-scan + `pyproject.toml` |
| S21 | entry points run | `autonomous` | smoke via `uv run python -m …` |
| S22 | strict gate green | `autonomous` | ruff + mypy + `test` |
| S23 | exit-code contract; per-session skip continues the run | `autonomous` | `test` (argv, tmp roots, binary/non-UTF-8) |
| S24 | fixtures + fake runner present | `autonomous` | `test` |
| S25 | acceptance smoke completes | `operator-assisted` / `external-oracle` | `test:full` |
| S26 | requestId-keyed isolability; prose/thinking/batch blank usage | `autonomous` | `test` (prose + pooled fixtures) |
| S27 | schema dispatch (`detect_parser`); UnknownSchema / AmbiguousSchema | `autonomous` | `test` (`test_adapters` / `test_registry`) |
| S28 | no default parser; unrecognized schemas skip loudly | `autonomous` | `test` (cursor → skipped_roots; codex now parsed per S33) |
| S29 | producer split on `version`; `UsageProvenance` stamped; four-case cache render | `autonomous` | `test` (`schema_hermes_trace.jsonl` fixture; `detect_parser` → `HermesTraceParser`; `n/a` / `n/a*` / `no` render) |
| S30 | probe refuses trace at dispatch; `_turn_key` raises `NonIsolableTurns`; no timestamp fallback | `autonomous` | `test` (probe fixtures carry `requestId`; refusal on a stripped fixture) |
| S31 | gate command collects every test (`TestCase` methods + module-level fns) | `autonomous` | `test` (`test_gate_completeness.py`) |
| S32 | session-grain `cache_read_tokens` surfaced as an agent-level caveat, never fabricated per call, never mixed with the per-call `cache_assisted` column | `autonomous` / `operator-assisted` (live archive ratio) | `test` (`test_hermes.py` present/null/zero session-grain fixtures; `test_passive.py` Reducer counters + Agent Breakdown caveat render + Tool Leaderboard non-leakage) |
| S33 | codex's three paired call shapes join on `payload.call_id`, each with its own input/output field and name source; `tool_search_call` named `ToolSearch` so the deferral tax sees it; session identified by `session_meta.id` (not `session_id`); `model` from `turn_context`; usage `ABSENT_BY_SCHEMA`; `error` never inferred; `spawn_agent` counted as fan-out; `web_search_call` unclaimed (TB-24); claim predicate disjoint from claude/hermes | `autonomous` / `operator-assisted` (live archive count) | `test` (`test_parsers.py` codex fixture: three joins, `input`-vs-`arguments`, dict-vs-string `arguments`, `tools`-vs-`output`, rollout-id identity incl. a subagent rollout and an older no-`session_id` rollout, EOF drain, provenance, no-error; `test_passive.py` `spawn_agent` fan-out; `test_adapters.py` detection + claude non-theft; `test_registry.py` / `test_passive.py` end-to-end, with cursor still raising `UnknownSchema`) |
| S34 | skips are typed `SkipRecord(session_id, agent, reason: SkipReason, detail)`; `MissingSourceExport` raised at the loader on `source file not found` stderr (a flat `RuntimeError` sibling of `NonTranscriptExport`, not a subclass); `classify_skip` maps each caught type to `MISSING_SOURCE`/`UNKNOWN_SCHEMA`/`NON_TRANSCRIPT`/`DECODE_ERROR`/`EXPORT_FAILED`; `tally_skips` answers "how many have no parser?" without prose parsing; render byte-identical (TB-21 owns the histogram) | `autonomous` | `test` (`test_sources.py` MissingSourceExport on source-not-found, other failure not mis-typed, sibling-not-subclass; `test_passive.py` classify_skip per type, skip_record_for identity, tally_skips counts, `_discover_refs` typed skip) |
| S35 | Summary opens `Sessions discovered: D / scanned: M / skipped: K` with `D = M + K` derived; a `Skipped by reason` histogram keyed on `SkipReason` (S34), count-desc, ties on value, zero omitted; the one-line 1639-id blob removed; ids behind `--verbose` (`### Skipped sessions (detail)`); empty-selection message reports a typed `(skipped K: reason=count)` tally | `autonomous` / `operator-assisted` (live archive `discovered/scanned/skipped` ratio) | `test` (`test_passive.py` `DiscoveryReconciliationRenderTests` reconcile line, histogram order, no-histogram-when-empty, blob-gone, ids-only-under-verbose; `DiscoveryReconciliationMainTests` mixed-discovery end-to-end; updated binary/decode/empty-selection integration tests) |
| S36 | Summary emits `Corpus fingerprint: <digest> (<N> sessions scanned)`, a sha256/16-hex over the sorted set of `session_signature(id, call_count, malformed, unjoinable)` for the *scanned* set; order-independent; a vanished tail (id leaves) **and** an append (call, malformed, or unjoinable count grows) all move it, while an id-only digest — or one folding only some counts — would falsely match across an append | `autonomous` / `operator-assisted` (two live runs; matching digest ⇒ diffable) | `test` (`test_passive.py` `CorpusFingerprintTests` order-independence, count, membership-move, grown-session-moves-digest, malformed-line-moves-digest, appended-web_search_call-moves-digest; `CorpusFingerprintRenderTests` line present/absent; `CorpusFingerprintMainTests` identical-runs same line, vanished session moves it, grown session moves it with same id set) |
| S37 | `--freeze <manifest>` write-once (`toolbench/freeze.py`, `SessionRef` round-trip + stored identity fingerprint) then replay bypassing live discovery; refs that no longer load counted as `(<V> vanished since freeze)` with ids under `--verbose`; a missing raw transcript raises the typed `MissingSourceExport`; byte-identical replay over an unchanged corpus | `autonomous` / `operator-assisted` (freeze once, replay to compare) | `test` (`test_freeze.py` manifest round-trip, fingerprint+count, path preservation; `test_passive.py` `CorpusFreezeMainTests` write-on-first-run, replay-uses-frozen-refs-not-discovery, vanished-reported, two-replays-byte-identical; `test_sources.py` raw-missing → MissingSourceExport) |
| S38 | a tool record a parser recognizes but structurally cannot join (no join key, no output) is counted in `ParseResult.unjoinable` (kind → count), never as a joined call or `no_result` orphan; `CodexParser` counts `web_search_call` there; `Reducer` folds by `(agent, kind)`; the Summary renders `Unjoinable tool records (seen, not joined): <T>` + one sorted `<agent>/<kind>: <count>` line when non-zero, absent otherwise; the count folds into `session_signature` (S36) | `autonomous` / `operator-assisted` (live codex archive `web_search_call` count) | `test` (`test_parsers.py` counts-by-kind, not-emitted-as-call, empty-when-absent; `test_passive.py` `ReducerAbsorbTests` fold-by-agent-and-kind, `DateRangeFilterTests` survives-date-filtering, `CorpusFingerprintTests` appended-web_search_call-moves-digest, `UnjoinableReconciliationRenderTests` line present-with-attribution / absent-when-empty) |
| S39 | Claude sessions sum `session_cache_read_tokens` / `session_cache_creation_tokens` from per-message `usage` (`None` when unmeasured, `0` when measured-zero); hermes path unchanged (sessions row, no double-count); TB-25 date-range survival via `replace()`; Summary caveat prints read + creation together, never ranks | `autonomous` / `operator-assisted` (live Claude run diff) | `test` (`test_parsers.py` Claude cache sums; `test_report.py` S39 Summary caveat; `test_passive_cli.py` date-range survival) |
| S40 | Claude buckets per-entry `usage` by `gitBranch` into `usage_by_branch`, additive beside the S39 session totals (`session total == sum of buckets`); a straddling session splits across buckets and does **not** donate its session total to a run it merely touched (counter-trap); `unattributed` is scoped to candidate sessions; a manifest branch matching zero entries is named; a markdown file passed to `--run-manifest` fails with a clear message; read + creation rendered together, per-ticket normalized, never ranked; `usage_by_branch` survives `--date-from`/`--date-to` via `replace()`; detached-checkout usage (`gitBranch="HEAD"`) is booked to a `detached_*` bucket and named in the run section — never folded into the run total, never silently dropped (TB-28) | `autonomous` / `operator-assisted` (live lattice run diff) | `test` (`test_parsers.py` branch bucketing + additivity invariant; `test_run_manifest.py` JSON reader + markdown refusal + empty-branch-set refusal; `test_reducer.py` in-set fold, straddle counter-trap, candidate scoping, zero-match branches, per-ticket, prefix-sharing trap, detached-HEAD named-not-dropped + not-mislabelled-as-unattributed + zero-usage raises no false alarm; `test_report.py` run section + detached blind-spot line + no line when clean; `test_passive_cli.py` flags + exit 1 on a bad manifest + date-range survival) |

## Operator post-merge smoke checkpoints (human-driven)

1. **Join-key on real data (S1/S2).** Run `passive --agent claude --project
   <one real project> --limit 5` against a real `~/.claude/projects` file and
   confirm tool-output tokens are non-zero — i.e. the block-local `content`
   branch actually fires. This is the flagged primary risk.
2. **AgentsView live path (S10/S25).** With the daemon healthy, run
   `--index-source auto --limit 20`; then stop the daemon and confirm the
   fallback-to-raw path and that the report names the reason.
3. **Scale (S11).** `--all --limit 200 --verbose` completes with flat memory.
4. **Report reads well (`felt`).** The five-section report is scannable and
   the inefficiency callouts are actionable (`N of M (P%); top: <tool>`),
   not bare counts.
5. **Probe isolability (S26).** Score a dedicated probe session with
   `toolbench.probe --session …`. Expect ten unseeded context-token cells and
   real usage numbers (not `—`). A `—` in usage with unseeded context tokens
   means the arm matched but its response was not isolable. When both usage
   cells are filled, do **not** rank arms on them — bash usage includes a
   sentinel (+ optional `description`) tax the tool arm cannot carry (TB-17);
   compare context tokens instead.
6. **Live trace export (S29/S30, `external-oracle`).** Export a real session
   with `hermes sessions export --format trace <dir>` (positional dir, not
   `--output-dir`). Confirm `passive` reports its calls with a `n/a` cache
   flag rather than `no`, and that `probe` over the same file refuses with
   `NonIsolableTurns` instead of scoring it. The fixture proves the shape;
   only the real CLI proves the shape is still what hermes emits.
7. **Session-grain cache ratio (S32, `operator-assisted`).** Run `passive
   --agent hermes --all` against the live archive and confirm the Agent
   Breakdown gains a `hermes: M of N sessions carry session-grain
   cache_read_tokens > 0` caveat line whose ratio is in the same ballpark as
   a direct `SELECT` against the profile databases (~90% at ticket-filing
   time), while the Tool Leaderboard's hermes row is still `n/a` — proving
   the two signals stayed separate end to end, not just in fixtures.
8. **Claude cache read+creation (S39, `operator-assisted`).** Run `passive`
   over a Claude project slice and confirm the Summary prints
   `Session-grain cache tokens: read=… creation=…`. For a lattice run diff,
   build a JSON run-manifest and run `toolbench.passive --agent claude
   --run-manifest run.json --tickets N` (S40); treat a read drop that raises
   creation by ~the same amount as a fake win (prefix-sharing trap).
