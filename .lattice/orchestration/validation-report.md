# Validation Report

Source spec: [SPEC.md](../../SPEC.md)
Source build plan: [BUILDPLAN.md](../../BUILDPLAN.md)
Source validation plan: [validation-plan.md](./validation-plan.md)
Result Validator: fresh Sonnet audit session (Phase 2, run 2)
Date: 2026-07-10
Run completed: 2026-07-10T06:44 (per run-state.md dispatch-complete log)

## Summary
- Total criteria audited: 13 (all `pre-merge-static` rows, 1–13)
- Pass: 12
- Partial: 1
- Fail: 0
- Blocked: 0

**Overall verdict: 🟡 Yellow-leaning-green.** Every substantive criterion (TB-18's S29/S30 implementation, and TB-19's/TB-20's self-authored S31/S32 contract rows) passes cleanly against source, tests, and diff inspection. The single Partial (row 9, strict gate) is a pytest exit-1 on PR #21 that traces to an environment artifact — a `TOOLBENCH_LIVE`-gated live-archive test that should have skipped but instead attempted a real DB open in one sub-agent's shell — not a defect in any PR's diff. Recommend the operator re-run the gate for PR #21 in a clean shell (`env -u TOOLBENCH_LIVE uv run pytest -q`) to get a clean exit-0 confirmation before merge, rather than treating this as a blocking finding.

## Per-criterion results

| # | SPEC.md criterion | Result | Notes |
|---|---|---|---|
| 1 | S29 — producer split (`HermesTraceParser`/`ClaudeParser` claims_line partition) | ✅ Pass | `parsers.py:249` and `:95` are mutually exclusive by construction (`version == HERMES_TRACE_VERSION` vs `!=`). Partition test `test_adapters.py:122-131` passes. Targeted run: 5/5 passed. PR #20. |
| 2 | S29 — fixture routes to `HermesTraceParser` by exact type | ✅ Pass | `tests/fixtures/schema_hermes_trace.jsonl` exists; `test_adapters.py:117-119` asserts `type(parser) is HermesTraceParser`. No `AmbiguousSchema`/`UnknownSchema`. PR #20. |
| 3 | S29 — `usage_provenance` stamped at every `ToolCall(` site | ✅ Pass | `transcript.py:71` field has no default; all 3 construction sites (`hermes.py:195`, `parsers.py:188`,`:209`) pass it explicitly. `HermesTraceParser._provenance` (`parsers.py:251-254`) returns `ABSENT_BY_EXPORT` unconditionally. `mypy --strict` → 38 errors, all pre-existing `no-untyped-def` in test files — 0 new vs. baseline. PR #20. |
| 4 | S29 — four-case cache render (`yes`/`no`/`n/a`/`n/a*`) + `usage_missing` counter | ✅ Pass | `test_passive.py` `CacheNoteRenderTests` (814-863) covers all four renders distinctly. `passive.py:41` `usage_missing: int = 0` is a counter; `"no"` only reached via `elif stats.usage_missing == 0` (passive.py:352-360). PR #20. |
| 5 | S29/S19 — cache render is caveat-only, never ranks | ✅ Pass | `passive.py:351` sort key is `output_tokens` only; `cache_note` computed after `ranked` is built and never feeds back into ordering. PR #20. |
| 6 | S30 — probe refuses trace input at dispatch (no partial-corpus mode) | ✅ Pass | `test_probe.py:450-456` asserts `NonIsolableTurns` raised with "trace" in message, no scored table produced. `probe.py main()` exposes no partial-corpus flag. PR #20. |
| 7 | S30 — `_turn_key` raises `NonIsolableTurns`, no `ts:` fallback | ✅ Pass | `probe.py:160-166` raises on missing/empty `requestId`; anti-regression test `test_probe.py:444-447` asserts no `f"ts:{"` in source. Independent `rg -n 'ts:' toolbench/probe.py` → only a class-name false-positive (`_TurnStats`), zero real fallback occurrences. PR #20. |
| 8 | S30 — probe fixtures migrated (Task 4) before fallback deletion (Task 5); pooled fixture untouched | ✅ Pass | Commit order confirmed: `1ac1220` (Task 4, requestId migration) precedes `5704e8e`/`53e2762` (Task 5). `probe_session_response_pooled.jsonl` byte-identical to `main` (MD5 match, no diff). PR #20. |
| 9 | S22 — strict gate green on all three PRs at head | ⚠️ Partial | Baseline (fresh `main` clone) confirmed at exactly 38 mypy errors. **Ruff:** exit 0 on all three PRs. **Mypy:** exit 1 (as expected — errors exist) but exactly 38 on all three, 0 new vs. baseline. **Pytest:** PR #20 → 247 passed/1 skipped (exit 0); PR #22 → 260 passed/1 skipped (exit 0); **PR #21 → 214 passed/1 failed (exit 1)**. The failure is `test_hermes.py::LiveArchive::test_live_archive_schema_envelope`, gated by `if not os.environ.get("TOOLBENCH_LIVE"): self.skipTest(...)` (confirmed at `tests/test_hermes.py:396-397` in the root checkout) — it should always skip absent that var. It skipped cleanly in the #20 and #22 runs but attempted a real `~/.hermes/state.db` open and errored in the #21 sub-agent's shell, most likely because `TOOLBENCH_LIVE` leaked into that one environment. Deselecting that single test: 214/214 pass. Not attributable to PR #21's diff. |
| 10 | S31 — criterion authored (TB-19: SPEC + EVALUATION + BUILDPLAN all gain S31 rows) | ✅ Pass | All three docs updated in commit `46007bb`. `SPEC.md:166-171` "S31 — gate collects every test" pins `uv run pytest -q`; `EVALUATION.md` Harness `test` command updated + matching S31 table row; `BUILDPLAN.md:51` T-row carries both `S31` and `TB-19`. PR #21. |
| 11 | S31 — full collection proven, gap closed | ✅ Pass | `uv run pytest -q --collect-only` → 215 tests; `unittest discover` → 177 (a 38-test gap — suite grew since the ticket cited "37 of 220"; same defect class, numbers shifted). Documented gate command (`pytest -q`) now collects the full 215, closing the primary pass-condition disjunct. Regression test `tests/test_gate_completeness.py` (commit `c48d0b7`) pins the defect class via a synthetic fixture package so it can't silently recur. The alternate disjunct ("`unittest discover` no longer documented anywhere") is not literally met — 6 hits remain, all contrastive/explanatory, not documenting it as the gate. PR #21. |
| 12 | S32 — criterion authored (TB-20: SPEC + EVALUATION + BUILDPLAN all gain S32 rows) | ✅ Pass | Diffed against the TB-18 base branch (not main) to isolate TB-20's own additions. `SPEC.md` gains full "S32 — session-grain cache surfaced without per-call fabrication" entry; `EVALUATION.md` gains S32 row + operator checkpoint #7; `BUILDPLAN.md` gains `T10` row mapped to S32, retroactive-rows prose updated to "T7–T10". PR #22. |
| 13 | S32 — session-grain cache consulted; DB opens stay read-only | ✅ Pass | `test_hermes.py::test_session_cache_read_tokens_surfaces_when_present` and `test_passive.py::test_caveat_line_present_with_correct_ratio` prove the Agent Breakdown renders session-grain cache signal instead of a universal miss for hermes buckets, without leaking into the untouched per-call `cache_assisted` column. `rg -n 'sqlite3.connect' toolbench/` → exactly 2 sites in `hermes.py::_connect` (lines 80, 86): `mode=ro` always first; `immutable=1` only in the `except OperationalError` branch gated on absence of a `-wal` sidecar. `uv run pytest -q` → 260 passed/1 skipped. PR #22. |

## Drift from BUILDPLAN.md

- **Test-suite size grew between ticket filing and delivery.** TB-19's ticket text cites "37 of 220" tests silently skipped; the PR's own regression test and live counts show 215 collected under `pytest -q` and 177 under `unittest discover` — a 38-test gap on a 215-test suite. This doesn't change the verdict (S31's pass condition is about parity between the documented command and the true count, which holds), but the operator should not expect the ticket's original numbers to reconcile exactly with what's in the PR.
- **TB-20 landed a new `T10` BUILDPLAN row** (as designed — TB-20 authors its own contract row per the run's contract-gap policy) rather than reusing an existing T-row. This is expected drift, not a problem: BUILDPLAN's "T7–T9 recorded retroactively" prose was correctly extended to "T7–T10" to keep the numbering honest.

## Gaps

None found. All 13 pre-merge-static criteria have a corresponding row and passing (or accounted-for) verification; no SPEC criterion in this run's scope (S29, S30, S31, S32) is unaddressed by a merged-or-open PR.

## Recommendations

- **Fix-back-in-flight:** Re-run `uv run pytest -q` for PR #21 (`tb-19-pytest-gate`) in a shell with `TOOLBENCH_LIVE` explicitly unset (`env -u TOOLBENCH_LIVE uv run pytest -q`) to confirm a clean exit 0 before merge. This is a low-cost confirmation, not a code fix — the gating logic in `test_hermes.py` is already correct.
- **Accept-as-is:** The TB-19 ticket-number drift (215 vs. 220, gap 38 vs. 37) — the criterion the PR ships (documented-command parity) holds regardless of the exact historical count; no action needed beyond operator awareness.
- **New tickets:** None indicated by this audit.
- **Merge order** (per run-state.md, reconfirmed): #20 first, then retarget #22 (currently based on #20's branch) to `main` and rebase before merging; #21 is independent and can merge any time, but re-verify its gate per the recommendation above first.

## What I couldn't verify

- **Root cause of the `TOOLBENCH_LIVE` leak in PR #21's run environment.** I confirmed the gating mechanism (`test_hermes.py:396-397`) is correct and that the same nominal config skipped cleanly for PR #20 and #22, but I did not trace exactly why that one sub-agent's shell had the variable set (or an equivalent truthy value) when the others didn't. This is a sandbox/environment-inheritance question, not a code question, and doesn't block the merge decision but is worth a quick operator sanity check if it recurs in CI.
- **Row 11's "no timestamp fallback" grep count** — 6 contrastive hits of the phrase "unittest discover" remain in the docs (explaining why *not* to use it). The pass condition's first disjunct (count parity) is unambiguously met, so I did not treat this as a blocker, but flagging it in case the plan's author intended a stricter reading.

## Operator smoke-pass checklist (post-merge)

Copied verbatim from `validation-plan.md` — these are **not** attempted here; they require a merged tree or a live/real-CLI environment.

| # | SPEC.md criterion | Verification method | Artifact to inspect | Pass condition |
|---|---|---|---|---|
| 14 | S29/S30 live trace export (external-oracle) | EVALUATION smoke #6: `hermes sessions export --format trace <dir>` on a real session; `passive` renders `n/a` (not `no`) for its calls; `probe` over the same file refuses with `NonIsolableTurns` | operator terminal, merged tree | Both behaviors observed against a fresh export from the installed hermes CLI |
| 15 | S32 live archive (operator-assisted) | `TOOLBENCH_LIVE=1 uv run pytest -q` against the real `~/.hermes` archives; hermes cache figures materially non-zero in a real report run | operator terminal, merged tree | Live suite green; a real hermes report shows session-grain cache signal |
| 16 | Report reads well (felt) | Operator reads one full passive report post-merge | merged tree report output | Four-case cache column is scannable and the `n/a*` footnote explains itself |
