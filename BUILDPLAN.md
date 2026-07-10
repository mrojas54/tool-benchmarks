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
  interpretation (`TranscriptParser`) are orthogonal ABCs. Hermes claims by
  source; every other session is content-sniffed. Unrecognized schemas raise
  `UnknownSchema` and land in `skipped_roots` (S27–S28).

## Test split

- **`test`** — `uv run python -m unittest discover tests` (hermetic,
  stdlib, fake `agentsview` runner, no `~/.claude` access; ≤60s).
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

## Checkpoint sequence

1. **Skeleton (T1)** — `uv run python -m unittest discover tests` green on
   the record + normalizer; `pyproject.toml` shows empty runtime deps.
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
  skip loudly instead of reporting healthy zeros. **Codex** still needs a
  `CodexParser` (TB-12); until then codex/cursor sessions land in
  `skipped_roots`. The raw scanner (S7) remains Claude-Code-only
  (`~/.claude/projects`). Note for any future raw adapter work: Codex and
  Hermes are **multi-root** (Codex 2 roots incl. archived; Hermes 3
  profile roots), unlike Claude Code's single root.
