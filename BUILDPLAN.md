# tool-benchmarks — BUILDPLAN

Decided architecture and the ticket breakdown lattice will mint into a board.
Source of truth for behavior is `SPEC.md`; source of the step-by-step is
`docs/superpowers/plans/2026-07-07-tool-benchmarks.md` (v2).

## Architecture (decided)

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

- **Runtime:** Python stdlib only (`subprocess` shells to the AgentsView CLI).
- **Project:** uv-managed; `pyproject.toml` + `uv.lock`; dev group
  `ruff`/`mypy`/`pytest`; runtime deps empty.
- **Parser deviation (decided):** `parse_session` returns
  `ParseResult(calls, malformed)` — additive, so the malformed count reaches
  the report footer. After TB-13 it is a compat shim over `ClaudeParser`;
  dispatch is `registry.pick_adapter` → `SessionAdapter.parse`.
- **Reducer (decided):** passive keeps only per-agent/per-tool counters
  globally; it never accumulates a whole-corpus `list[ToolCall]`.
- **Schema dispatch (TB-13, shipped):** acquisition (`SessionLoader`) and
  interpretation (`TranscriptParser`) are orthogonal ABCs. Hermes SQLite
  claims by source; every other session is content-sniffed (including
  hermes-trace via `HermesTraceParser`, S29). Unrecognized schemas raise
  `UnknownSchema` and land in `skipped_roots` (S27–S28).
- **Usage provenance (TB-18 / TB-20, shipped):** every `ToolCall` carries
  `UsageProvenance`; cache flags render `n/a` / `n/a*` when unmeasurable
  (S29). `probe.py` keys turns only by `requestId` and refuses hermes-trace
  (S30). Hermes session-row `cache_read_tokens` surfaces as an Agent
  Breakdown caveat, never a per-call rate (S32).

## Test split

- **`test`** — `uv run pytest -q` (hermetic, fake `agentsview` runner, no
  `~/.claude` access; ≤60s). Collects `unittest.TestCase` methods and
  module-level `test_*` functions uniformly — `unittest discover` silently
  missed the latter and is no longer the documented command (S31 / TB-19).
- **`test:full`** — the S25 acceptance-smoke commands against real corpus /
  live daemon; operator-run post-merge.

## Tickets

Walking skeleton first (T1), then the parser/discovery substrate in
parallel (T2, T3), then the two consumers (T4, T5), then docs + gate (T6).

| Ticket | Scope | SPEC IDs | Depends on |
|--------|-------|----------|------------|
| **T1 — scaffold + `ToolCall` + `result_len`** | uv init, `pyproject.toml`, empty `toolbench/`, the record type + normalizer, first tests | S3, S4, S20, S21 | — |
| **T2 — `parse_session` id-join** | `ParseResult`, `_result_id`/`_result_payload`, block-local `content`, malformed + interrupted handling; fixtures | S1, S2, S5, S6, S24 | T1 |
| **T3 — `sources.py` multi-agent discovery** | `SessionRef`, `iter_session_files`, `iter_agentsview_sessions` (cursor pagination), `open_session_jsonl`, index-source policy; fake-runner tests | S7, S8, S9, S10, S24 | T1 |
| **T4 — `passive.py` reducer + report + CLI** | incremental reducer, five report sections + provenance, full CLI, error/exit contract | S11, S12, S13, S14, S15, S19, S23 | T2, T3 |
| **T5 — `probe.py` + `active-probes.md`** | 5-file corpus under `tools/` (done), `_V2` sentinels + tool-name verify, comparison table + #8376 seeds → `reports/` | S16, S17, S18 | T2 |
| **T6 — README + strict gate** | README (agents/targets/run/index/metrics), then ruff + mypy --strict + full suite green; PR | S22 | T4, T5 |
| **T7 — schema dispatch seam** (lattice `TB-13`) | `detect_parser`, `PARSERS` registry, `UnknownSchema` / `AmbiguousSchema`; no default parser | S27, S28 | T2 |
| **T8 — requestId-keyed arm isolability** (lattice `TB-16`) | turn key is the `requestId`, not the timestamp; response-pooled usage attribution | S26 | T5 |
| **T9 — usage provenance + probe refusal** (lattice `TB-18`) | `UsageProvenance` enum, `HermesTraceParser` split on `version`, four-case cache render, `NonIsolableTurns` refusal, WAL read-only repair | S29, S30 | T4, T5, T7, T8 |
| **T10 — pytest as the documented gate** (lattice `TB-19`) | Replace `unittest discover` (silently missed 37/220 module-level tests) with `uv run pytest -q` as the gate command in README/EVALUATION/BUILDPLAN/AGENTS; `testpaths` pytest config; regression test pinning the collection defect | S31 | T6 |
| **T11 — session-grain cache surfaced as a caveat** (lattice `TB-20`) | `parse_hermes_session` reads `cache_read_tokens` off the session row; `ParseResult.session_cache_read_tokens`; `AgentStats` session-grain counters; Agent Breakdown caveat line, orthogonal to the per-call `cache_assisted` column | S32 | T4, T9 |
| **T12 — the codex parser** (lattice `TB-12`) | `CodexParser` joins `function_call`/`custom_tool_call` to their outputs on `payload.call_id`; registered in `PARSERS`; `session_id` lifted from `session_meta` and `model` from `turn_context`; usage `ABSENT_BY_SCHEMA` (codex bills per turn); no inferred `error` | S33 | T7, T9 |

`T1`–`T6` are the original v2 build-contract tickets (board `TB-2`–`TB-7`) and
predate the lattice board's use as the source of truth. `T7`–`T9` are recorded
retroactively: the work landed as lattice tickets first, and the criteria they
deliver — S26, S27, S28 — were previously claimed by no row here at all.

Rows are listed in dependency order, not in the order they were minted. Every
future row carries both IDs; `T9`'s step-by-step lives in
`docs/superpowers/plans/2026-07-09-tb-18-usage-provenance.md`; `T11`'s lives
in the lattice plan for `TB-20`.

`T12` closes the gap S28 only deferred: `codex` is parsed rather than skipped.
`cursor` remains in `skipped_roots` — a third schema again, unclaimed by any row
and still needing its own repro before a parser can be written for it.

## Checkpoint sequence

1. **Skeleton (T1)** — `uv run pytest -q` green on the record + normalizer;
   `pyproject.toml` shows empty runtime deps.
2. **Substrate (T2 ∥ T3)** — parser joins both key/payload shapes; sources
   page AgentsView via the fake runner. The **join-key on real data**
   (operator checkpoint #1) is de-risked here by the block-local fixture.
3. **Consumers (T4 ∥ T5)** — passive emits the five-section report from a
   streamed reducer; probe scores the seeded table.
4. **Gate (T6)** — strict gate green, README written, PRs opened.

## Config (recorded at Phase 0)

- Autonomy: **Moderate**; max concurrent delegators **N=3**.
- PR merge policy: leave at terminal pre-merge status (to be confirmed).
- Result Validator: on (6 tickets). Master Validator: default on.

## Open contract gaps to confirm before Phase 0

- **G1 — probe corpus vendored (S16). [RESOLVED — vendored under `tools/`.]**
  Rather than name five external absolute paths (non-reproducible,
  machine-specific), the five probe target files are **committed under
  `tools/`** so `active-probes.md` uses relative paths and probes re-run from
  a clean checkout. The five files are already in place — a log-spaced size
  spread: `regex_check.py` (121), `mcp.py` (352), `monitor.py` (768),
  `llm_extraction.py` (1,332), `code_analysis.py` (2,242). Probe *output*
  lands in `reports/`, kept separate from these inputs.
- **G2 — AgentsView on the build host (S10/S25). [RESOLVED — fast suite fakes
  the CLI.]** The fast `test` suite **fully fakes the `agentsview` CLI** (via
  the fake runner in `test_sources.py`, S24), so the build/inner-loop never
  depends on a healthy daemon. The live daemon path stays `external-oracle`:
  only `test:full` / the S25 acceptance-smoke touches the real CLI, and that
  is operator-run post-merge, never in the delegators' loop.
- **G3 — Codex/Hermes adapters. [PARTIALLY RESOLVED.]** Hermes now has a
  direct SQLite reader (`hermes.py`, TB-11) because AgentsView `session
  export` returns the whole profile database rather than a transcript
  (#1047). Discovery for hermes still goes through AgentsView `session
  list` (S9b) — under-sampling vs `stats` is #1048, not forked here.
  Schema dispatch (TB-13 / S27–S28) landed so unrecognized transcripts
  skip loudly instead of reporting healthy zeros. **Codex** is now parsed by
  `CodexParser` (TB-12 / S33); **cursor** sessions still land in
  `skipped_roots`. The raw scanner (S7) remains Claude-Code-only
  (`~/.claude/projects`). Note for any future raw adapter work: Codex and
  Hermes are **multi-root** (Codex 2 roots incl. archived; Hermes 3
  profile roots), unlike Claude Code's single root.
