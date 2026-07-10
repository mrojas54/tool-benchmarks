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
| S13 | subagent include/exclude path filter | `autonomous` | `test` |
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
| S28 | no default parser; unrecognized schemas skip loudly | `autonomous` | `test` (codex/cursor → skipped_roots) |
| S29 | producer split on `version`; `UsageProvenance` stamped; four-case cache render | `autonomous` | `test` (`schema_hermes_trace.jsonl` fixture; `detect_parser` → `HermesTraceParser`; `n/a` / `n/a*` / `no` render) |
| S30 | probe refuses trace at dispatch; `_turn_key` raises `NonIsolableTurns`; no timestamp fallback | `autonomous` | `test` (probe fixtures carry `requestId`; refusal on a stripped fixture) |
| S31 | gate command collects every test (`TestCase` methods + module-level fns) | `autonomous` | `test` (`test_gate_completeness.py`) |
| S32 | session-grain `cache_read_tokens` surfaced as an agent-level caveat, never fabricated per call, never mixed with the per-call `cache_assisted` column | `autonomous` / `operator-assisted` (live archive ratio) | `test` (`test_hermes.py` present/null/zero session-grain fixtures; `test_passive.py` Reducer counters + Agent Breakdown caveat render + Tool Leaderboard non-leakage) |

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
