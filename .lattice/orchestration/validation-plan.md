# Validation Plan
Source spec: [SPEC.md](../../SPEC.md) · Source evaluation: [EVALUATION.md](../../EVALUATION.md) · Date: 2026-07-08

The Result Validator (Phase 2) runs every `pre-merge-static` row exactly as
written, from `gh pr diff` + PR-branch source + the hermetic `test`/lint/type
commands (single-branch, no merged tree, no human, no real `~/.claude`).
`post-merge-smoke` rows are the operator checklist, run after merge.

| # | Criterion (ID) | Verification method | Artifact to inspect | Pass condition | runnable_at |
|---|----------------|---------------------|---------------------|----------------|-------------|
| 1 | S1 id-join | Read parser tests; both key locations (top-level `toolUseID` + block-local `tool_use_id`) exercised over fixtures | TB-3 PR: `toolbench/transcript.py`, `tests/` | Fixtures join a call via each key location; `output_chars` non-zero on matched result | pre-merge-static |
| 2 | S2 payload precedence | Read block-local-vs-top-level fixture; assert block-local `content` wins and source recorded | TB-3 PR: `transcript.py`, block-local fixture | Both-present fixture resolves to block-local payload; source field reflects the choice | pre-merge-static |
| 3 | S3 `result_len` (4 shapes) | Read `result_len` unit test over dict / string / MCP block-list / block-local content | TB-2 PR: `transcript.py`, `tests/` | All four shapes normalize to correct char length | pre-merge-static |
| 4 | S4 `ToolCall` fields | Read record definition + derived-prop test | TB-2 PR: `transcript.py` | Field set matches S4 exactly; `tokens==output_chars//4`, `input_tokens==input_chars//4` | pre-merge-static |
| 5 | S5 malformed non-fatal | Read malformed-line fixture; assert counted, skipped, surfaced in `ParseResult.malformed` | TB-3 PR: parser + fixture | Malformed line increments count, never raises; count reaches `ParseResult` | pre-merge-static |
| 6 | S6 interrupted kept | Read no-result fixture; assert kept with `output_chars=0, no_result=True` | TB-3 PR: parser + fixture | Interrupted `tool_use` retained, not dropped; flags set | pre-merge-static |
| 7 | S7 raw discovery | Read `iter_session_files` test over tmp tree; project substring + mtime filter; missing root raises | TB-4 PR: `sources.py`, tmp-tree test | Filters apply; `FileNotFoundError` on missing `root` | pre-merge-static |
| 8 | S8 AgentsView pagination | Read fake-runner test; cursor pagination + `SessionRef` shape | TB-4 PR: `sources.py`, `test_sources.py` fake runner | Multi-page cursor walk yields correct `SessionRef`s; `--limit 500` arg constructed | pre-merge-static |
| 9 | S9 uniform open | Read `open_session_jsonl` test over path vs export (fake runner) | TB-4 PR: `sources.py` | Both a filesystem path and an `export <id>` stream JSONL lines identically | pre-merge-static |
| 10 | S10 index-source (logic) | Read auto/strict/raw policy test; fallback records reason | TB-4 PR: `sources.py` | `auto` falls back on fake-nonzero-exit and records reason; `agentsview` strict errors; `raw` fs-only | pre-merge-static |
| 11 | S10 index-source (live) | Live AgentsView daemon healthy → auto uses it; daemon down → falls back, report names reason | merged tree + live daemon | `--index-source auto --limit 20` uses AgentsView, then falls back with a stated reason | post-merge-smoke |
| 12 | S11 incremental (reducer) | Read reducer unit + source scan: no whole-corpus `list[ToolCall]`; only per-agent/per-tool counters global | TB-5 PR: `passive.py` | No corpus-wide list accumulation; reducers are per-agent/per-tool | pre-merge-static |
| 13 | S11 incremental (scale) | `--all --limit 200 --verbose` completes with flat memory on real corpus | merged tree + real `~/.claude/projects` | Completes without unbounded memory growth | post-merge-smoke |
| 14 | S12 CLI | Read arg-parse test; flags + default scope `--agent all --all` | TB-5 PR: `passive.py` | All S12 flags parse; default scope correct | pre-merge-static |
| 15 | S13 subagents | Read include/exclude test; `--exclude-subagents` drops `/subagents/` paths | TB-5 PR: `passive.py` | Included by default; excluded on flag | pre-merge-static |
| 16 | S14 report sections | Read report-string test; four sections in order | TB-5 PR: `passive.py` | Agent breakdown → Tool leaderboard → Inefficiency callouts → Summary, in order | pre-merge-static |
| 17 | S15 provenance | Read report-string test for provenance fields | TB-5 PR: `passive.py` | Report states index source, sessions scanned, calls joined, malformed count, subagent inclusion, fallback reason, `--since` mtime note | pre-merge-static |
| 18 | S16 vendored corpus | Diff `protocols/active-probes.md` five relative paths against files under `tools/`; probe output goes to `reports/` | TB-6 PR: `protocols/active-probes.md`, `tools/`, `probe.py` | Exactly the five vendored files listed by relative path; all exist under `tools/`; no external absolute paths; outputs land in `reports/` | pre-merge-static |
| 19 | S17 sentinels | Read `find_probe_calls` test; `TB_PROBE_*_V2` globally unique, none a substring of another; verifies sentinel + tool name | TB-6 PR: `probe.py` | Sentinels unique + non-substring; probe-call match requires sentinel AND expected tool name | pre-merge-static |
| 20 | S18 comparison table | Read table test; context-tokens per arm + real `usage` when isolable; #8376 seeds (`search` 723/794, `find` 68/89) when arm absent | TB-6 PR: `probe.py` | Table emits per-arm context tokens; falls back to seeded baselines on absent arm | pre-merge-static |
| 21 | S19 metric roles | Read ranking test/source; context-cost (`chars/4`) ranks; cache flag caveat-only; failure/slow/churn callouts-only | TB-5 PR: `passive.py` | Ranking keyed on context-cost tokens; cache never ranks; churn/failure feed callouts only | pre-merge-static |
| 22 | S20 stdlib + uv shape | Import-scan `toolbench/` for third-party imports; read `pyproject.toml` (empty runtime deps, dev group ruff/mypy/pytest) + `uv.lock` present | TB-2 PR: `toolbench/`, `pyproject.toml`, `uv.lock` | No third-party imports in shipped package; uv project shape correct | pre-merge-static |
| 23 | S21 entry points | Read module guards + `unittest discover tests` green on PR branch | TB-2 PR: `toolbench/`, `tests/` | `python -m toolbench.passive` / `.probe` importable + guarded; `unittest discover` green | pre-merge-static |
| 24 | S22 strict gate | Run on PR branch: `uv run ruff check .`; `uv run mypy --strict toolbench tests`; `uv run python -m unittest discover tests` | TB-7 PR (final tree) | All three green | pre-merge-static |
| 25 | S23 exit contract | Read argv + tmp-root tests | TB-5 PR: `passive.py` | Empty selection → message + exit 0; missing strict root → exit 1; `--agent all --index-source auto` continues + reports skipped roots | pre-merge-static |
| 26 | S24 fixtures + fake runner | Confirm the five parser fixtures + fake `agentsview` runner present | TB-3/TB-4 PRs: `tests/` | String, MCP block-list, block-local content, interrupted, malformed fixtures + fake runner all present | pre-merge-static |
| 27 | S25 acceptance smoke | Run S25 commands against real corpus / live daemon | merged tree + real corpus + daemon | `--project` slice reports counts; `--all --limit 200 --verbose` bounded memory; `--index-source auto --limit 20` via AgentsView or stated fallback | post-merge-smoke |

## Operator post-merge smoke checklist (verbatim, human-driven)

1. **Join-key on real data (S1/S2)** — the flagged primary risk. Run
   `passive --agent claude --project <one real project> --limit 5` against a
   real `~/.claude/projects` file and confirm tool-output tokens are non-zero
   (the block-local `content` branch actually fires).
2. **AgentsView live path (S10/S25)** — with the daemon healthy, run
   `--index-source auto --limit 20`; then stop the daemon and confirm
   fallback-to-raw and that the report names the reason.
3. **Scale (S11)** — `--all --limit 200 --verbose` completes with flat memory.
4. **Report reads well (`felt`)** — the four-section report is scannable and
   the inefficiency callouts are actionable, not noise.
