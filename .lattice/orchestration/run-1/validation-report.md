# Validation Report — tool-benchmarks

**Date:** 2026-07-08 · **Auditor:** Orchestrator (degraded mode) · **Plan:** [validation-plan.md](validation-plan.md)

> ⚠️ **Independence caveat (honest disclosure).** The Phase-2 fresh-session Result
> Validator could not be spawned — two new c11 terminal surfaces failed to
> initialize their PTY, compounded by a blocking 1Password prompt and the weekly
> usage ceiling. Per operator direction, this audit was run by the **Orchestrator**
> instead. Mitigating the loss of "cold, no-build-context" independence: the
> Orchestrator did **not** author `SPEC.md` and did **not** implement any ticket
> (the delegators did); it walked the plan mechanically against shipped source it
> did not write, on the assembled `integration/full` tree; and it re-ran the strict
> gate itself rather than trusting delegator claims. What is genuinely reduced is
> the freshness of perspective — a follow-up cold audit is cheap insurance if
> desired (see Recommendations).

## Summary

**24 / 24 `pre-merge-static` rows PASS.** Zero FAIL, zero PARTIAL. The build
faithfully implements SPEC S1–S24. The flagged primary risk (S11 no-whole-corpus
list) is **correctly implemented and independently confirmed**. Three
`post-merge-smoke` rows (S10-live, S11-scale, S25) are for the operator; the
strict gate (ruff + mypy --strict + 93 unittest) is green on the assembled tree.

Evidence base: assembled tree `integration/full` (= merge of all six ticket
branches), re-run gate, and per-module source + test reads. PR→branch map:
#1 tb-2-scaffold, #2 tb-4-sources, #3 tb-3-parse, #4 tb-6-probe, #5 tb-5-passive,
#6 tb-7-readme.

## Per-criterion results (24 static rows)

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | S1 id-join | ✅ PASS | `transcript._result_id` tries block-local `tool_use_id` then top-level `toolUseID`; joined via `pending[tool_use_block["id"]]`. Tests: `test_id_block_local_only`, `test_string_result_top_level_join`. |
| 2 | S2 payload precedence | ✅ PASS | `_result_payload` returns block-local `content` ("block_local") before top-level `toolUseResult`; source stored in `ToolCall.result_source`. Test: `test_payload_block_local_wins_over_top_level`. |
| 3 | S3 result_len (4 shapes) | ✅ PASS | `result_len` handles str / dict(json) / MCP block-list / block-local `{content:[...]}`. Tests assert all four lengths (`transcript.py:11-39`). |
| 4 | S4 ToolCall fields | ✅ PASS | Dataclass has full S4 field set; `tokens=output_chars//4`, `input_tokens=input_chars//4`. Test `test_derived_tokens_floor_division` (101//4=25, 41//4=10). |
| 5 | S5 malformed non-fatal | ✅ PASS | `json.JSONDecodeError` / non-dict → `malformed += 1`, `continue`; surfaced on `ParseResult.malformed`. Test `test_malformed_line_counted_and_skipped`. |
| 6 | S6 interrupted kept | ✅ PASS | Leftover `pending` appended with `output_chars=0, no_result=True`. Test `test_interrupted_call_kept_with_no_result`. |
| 7 | S7 raw discovery | ✅ PASS | `iter_session_files` filters by parent-dir substring + mtime (`datetime.fromisoformat`); raises `FileNotFoundError` on missing root (`sources.py:32-47`). |
| 8 | S8 AgentsView pagination | ✅ PASS | `iter_agentsview_sessions` builds `session list --json --limit 500`, cursor-pages via `next_cursor`/`--cursor`, yields `SessionRef`. Injectable `Runner`; `FakeRunner` test. |
| 9 | S9 uniform open | ✅ PASS | `open_session_jsonl`: file path → read; else `agentsview session export <id>`. |
| 10 | S10 index-source (logic) | ✅ PASS | `iter_sessions`: raw=fs-only; agentsview=strict (RuntimeError on nonzero); auto=probe→fallback-to-raw recording reason. Returns `(refs, reason)`. |
| 12 | S11 incremental reducer | ✅ **PASS (flagged risk)** | `Reducer.absorb` folds each session's `ParseResult.calls` into per-agent (`agents`) + per-tool (`tools`) dict counters, then discards; main loop retains no corpus-wide `list[ToolCall]` (only a bounded `refs: list[SessionRef]`). Docstring + code trace confirm the invariant. |
| 14 | S12 CLI | ✅ PASS | `parse_args` defines all S12 flags; `--all`/`--project` mutually exclusive; default `--agent all`, `all_projects` default true. |
| 15 | S13 subagents | ✅ PASS | `filter_subagents` drops refs whose `path` contains `/subagents/`; applied only under `--exclude-subagents` (included by default). |
| 16 | S14 report sections | ✅ PASS | `render_report` emits Agent Breakdown → Tool Leaderboard → Inefficiency Callouts → Summary, in order; callouts cover ToolSearch/failures/oversized/subagent-fanout/churn. |
| 17 | S15 provenance | ✅ PASS | Summary lists index source, sessions scanned, calls joined, malformed count, subagents-included, fallback reason, skipped roots, and the `--since` mtime note. |
| 18 | S16 vendored corpus | ✅ PASS | `protocols/active-probes.md` lists exactly 5 relative `tools/` paths (all present); output defaults to `reports/active-probe-comparison.md`; explicit input/output separation note. |
| 19 | S17 sentinels | ✅ PASS | `TB_PROBE_0N_{TOOL,BASH}_V2` — 10 distinct, none a substring of another; `find_probe_calls` requires sentinel-in-input **and** matching tool name. |
| 20 | S18 comparison table | ✅ PASS | Per-arm context tokens (`chars//4`) + real `usage.output_tokens` only when turn isolable; seeds `SEED_BASELINES` search 723/794, find 68/89 when arm absent; `*` marks seeded. |
| 21 | S19 metric roles | ✅ PASS | Leaderboard ranked by `output_tokens` (context cost); `_is_cache_hit` is caveat column only ("never used for ranking"); failures/oversized/churn feed callouts only. |
| 22 | S20 stdlib + uv | ✅ PASS | `pyproject.toml` `dependencies = []`, dev group ruff/mypy/pytest; `uv.lock` present; import-scan of `toolbench/` shows only stdlib + internal imports. |
| 23 | S21 entry points | ✅ PASS | `passive.py`/`probe.py` have `if __name__ == "__main__"` guards + `main()`; `python -m unittest discover tests` green (93 tests). |
| 24 | S22 strict gate | ✅ PASS | Re-run on `integration/full`: `ruff check .` clean; `mypy --strict toolbench tests` clean (10 files); `unittest discover` 93 OK, exit 0. |
| 25 | S23 exit contract | ✅ PASS | Empty selection → message + `return 0`; strict missing root → caught → `return 1`; `--agent all --index-source auto` swallows `FileNotFoundError` into `skipped_roots` and continues. |
| 26 | S24 fixtures + fake runner | ✅ PASS | Parser fixtures: string, MCP block-list, block-local content, interrupted, malformed. `test_sources.FakeRunner` fakes the `agentsview` CLI (no daemon). |

## Drift from BUILDPLAN

Minor, all acceptable — none contradict SPEC:
1. **`--agent` is a no-op under `--index-source raw`** (TB-5-flagged). Consistent with S7, which scopes raw discovery to Claude Code only (parent-dir = *project*, not agent); the `--agent` filter applies to the AgentsView path (S8). Recommend a one-line README/`--help` note.
2. **`ToolCall.duration_ms` is always `None`.** `parse_session` documents that raw Claude Code JSONL carries no per-tool-call duration field. S4 lists the field (present in the type) but does not require it be populated from raw; acceptable.
3. **`SUBAGENT_TOOL_NAMES = {Agent, Task}`** is a heuristic set (TB-5-flagged) for the subagent-fanout callout. Callout-only (never ranks), so no S19 conflict; reasonable default.

## Gaps & recommendations

- **NIT (test hygiene):** a probe test prints the comparison table to stdout during
  `unittest discover` (visible in the run). Not a failure; consider capturing
  stdout or asserting on the returned string to keep the test run quiet.
- **Optional cold re-audit:** since this report was produced in degraded mode, a
  fresh-session validator pass (after the usage reset / once the 1Password/PTY
  issue clears) would restore full independence. Given 24/24 PASS with concrete
  evidence, this is insurance, not a blocker.
- **Merge order** for the operator: #1 (scaffold) → #2, #3 (sources, parse) →
  #4 (probe), #5 (passive) → #6 (README+gate); each child PR rebases onto `main`
  after its parents merge (the integration branches were the build-time base).

## Operator post-merge smoke checklist (verbatim — human-driven, NOT run here)

Rows 11, 13, 27 from the validation plan + the four EVALUATION checkpoints:

1. **Join-key on real data (S1/S2)** — the flagged primary risk. Run
   `passive --agent claude --project <one real project> --limit 5` against a real
   `~/.claude/projects` file and confirm tool-output tokens are non-zero (the
   block-local `content` branch actually fires).
2. **AgentsView live path (S10/S25)** — with the daemon healthy, run
   `--index-source auto --limit 20`; then stop the daemon and confirm
   fallback-to-raw and that the report names the reason.
3. **Scale (S11)** — `--all --limit 200 --verbose` completes with flat memory.
4. **Report reads well (`felt`)** — the four-section report is scannable and the
   inefficiency callouts are actionable, not noise.
