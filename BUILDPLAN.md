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
   parsers (parsers.py)  ── interpretation: lines → ToolCall / ParseResult
          │
   adapters (adapters.py + registry.py)  ── SessionRef → ParseResult
          │
     ┌────┴──────────────────┐
 passive.py (CLI / scan)   probe.py
     │                         │
 reducer.py → report.py        │  (ClaudeParser keep_raw + track_turns)
 freeze.py (opt-in pin)        │
     └──────────┬──────────────┘
          reports/*.md

 cache_tokens.py  ── run-aggregation façade over ClaudeParser (S39;
                      bridge until T17 / S40 `--run-manifest` on passive)
```

- **Runtime:** Python stdlib only (`subprocess` shells to the AgentsView CLI).
- **Project:** uv-managed; `pyproject.toml` + `uv.lock`; dev group
  `ruff`/`mypy`/`pytest`; runtime deps empty.
- **Parser seam (decided):** callers open lines and use `ClaudeParser.parse`
  or `registry.pick_adapter` → `SessionAdapter.parse`. Path-based
  `parse_session` was retired (CQ 1.3). Probe reuses the same pass via
  `keep_raw_input` / `track_turns` (CQ 7.1).
- **Reducer (decided):** `reducer.py` keeps only per-agent/per-tool counters
  globally; it never accumulates a whole-corpus `list[ToolCall]`. Report
  rendering and fingerprinting live in `report.py`.
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
| **T12 — the codex parser** (lattice `TB-12`) | `CodexParser` joins three paired shapes (`function_call`, `custom_tool_call`, `tool_search_call`) to their outputs on `payload.call_id`; registered in `PARSERS`; session identified by `session_meta.id`, `model` from `turn_context`; usage `ABSENT_BY_SCHEMA` (codex bills per turn); no inferred `error`; `spawn_agent` counted as subagent fan-out; `web_search_call` unjoinable (TB-24) | S33 | T7, T9 |
| **T13 — corpus reproducibility** (lattice `TB-22`) | `corpus_fingerprint` + `session_signature` (identity + call count) over the scanned set, emitted in the Summary; `--freeze <manifest>` (`toolbench/freeze.py`) write-once then replay bypassing live discovery, naming refs vanished since freeze; a missing raw transcript raises the typed `MissingSourceExport` so both loader paths bucket a gone source as `missing_source` | S36, S37 | T4, T7 |
| **T14 — unjoinable tool records** (lattice `TB-24`) | `ParseResult.unjoinable` (kind → count) for records a parser recognizes but cannot join; `CodexParser` counts `web_search_call` there instead of dropping it; `Reducer` folds by `(agent, kind)`; the Summary names the gap (`Unjoinable tool records (seen, not joined): <T>` + attribution) rather than leaving codex's ~4% web-search undercount a silent zero; the count folds into `session_signature` so an appended `web_search_call` moves the fingerprint | S38 | T12, T13 |
| **T15 — date-range cache-stat drop fixed** (lattice `TB-25`) | `_apply_date_range` reconstructs the `ParseResult` with `dataclasses.replace(result, calls=kept)` instead of hand-listing fields, so `session_cache_read_tokens` (S32) survives `--date-from`/`--date-to` instead of silently resetting to `None`; a session whose calls all fall outside the range still contributes its cache stat, since it was still measured | S32 | T11 |
| **T16 — session-grain cache-token sums for Claude** (lattice `TB-26`) | Populate `session_cache_read_tokens` for Claude by summing per-message `usage.cache_read_input_tokens`; add `session_cache_creation_tokens` from `cache_creation_input_tokens`; both promote the `_is_cache_hit` boolean (S19) to a session total; NULL-vs-measured per S32; hermes path unchanged (no double-count); TB-25 date-range survival extends to the new field; Summary renders a read + creation caveat line, never a ranking column | S39 | T11, T15 |
| **T17 — per-run cache grouping via `--run-manifest`** (lattice `TB-27`) | `--run-manifest <run.json>` (orchestrator-emitted `{run, tickets, branches, worktrees?}` — **not** `agents.md`, which discards branch data on completion) folds entry-grain usage whose `gitBranch` is in the run's branch set into one reducer; out-of-set usage within candidate sessions lands in `unattributed`; emits cache read + creation per run, normalized per ticket; Claude-only (`gitBranch`); retires the standalone `cache_tokens` façade once landed. Design: `docs/superpowers/specs/2026-07-12-tb-27-per-run-cache-grouping-design.md` | S40 | T16 |

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
