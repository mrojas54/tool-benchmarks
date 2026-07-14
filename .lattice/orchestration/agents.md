# Agents — tool-benchmarks (run 3: TB-34 · TB-36 · TB-37 · TB-38)

Active table overwritten each dispatch tick. Run 3 uses Agent-tool worktree
isolation, not c11 panes/surfaces — "Surface ref"/"Pane ref" are N/A this run;
ground truth is the Agent tool's background-task status plus verified git/PR
state. Run 1 + Run 2 delegator history retained under Archived.

## Active
| Role | Ticket | Branch | Worktree isolation | Phase | Spawned at |
|------|--------|--------|---------------------|-------|------------|
| (none — Run 3 complete: dispatch + Phase 2 Result Validator both done, all four PASS; awaiting operator merge decision) | | | | | |

## Completed this run (Run 3, dispatch complete 2026-07-14, awaiting Phase 2 Result Validator)
| Ticket | PR | Status | Notes |
|--------|----|--------|-------|
| TB-34 | [#60](https://github.com/mrojas54/tool-benchmarks/pull/60) OPEN, mergeable | review | Additive census disclosure via reused `report._sampling_notes`; SPEC S35 extended in place; 2 new tests; 580 passed/2 skipped/3 subtests. Freeze-replay region (TB-37) untouched. |
| TB-36 | [#61](https://github.com/mrojas54/tool-benchmarks/pull/61) OPEN, mergeable | review | Option A (structural): `_probe_agentsview` now built via `_list_argv`, argv reproduced byte-for-byte; 1 new test; 579 passed/2 skipped/3 subtests. `_list_argv` and TB-38's functions untouched. |
| TB-37 | [#63](https://github.com/mrojas54/tool-benchmarks/pull/63) OPEN, mergeable | review | MANIFEST_VERSION -> toolbench-freeze-2; census persisted at freeze time; key-presence-tolerant v1 read (mirrors TB-29 precedent); historical-denominator caveat on v2 replay; SPEC/EVALUATION S37 extended; 7 new tests; 585 passed/2 skipped/3 subtests. TB-34's region untouched. |
| TB-38 | [#62](https://github.com/mrojas54/tool-benchmarks/pull/62) OPEN, mergeable | review | Operator-confirmed design implemented: `_discover_refs` widened to catch `RuntimeError`/`AgentsViewTimeout` mid-listing (auto only), discards partial refs, rescans raw via one code path; SPEC S10 + EVALUATION updated; 1 test rewritten, 4 new; 582 passed/2 skipped/3 subtests. `sources.py` has zero diff (TB-36 untouched). |

### Cross-PR note for the Result Validator
All four branches are independently based off `origin/main` (pre-Run-3 tip)
and each PR is small/disjoint by construction, but none have been merged
into each other — TB-34/TB-37 both touch `passive.py`, TB-36/TB-38 both touch
`sources.py`, all in disjoint regions per the delegators' own diffs. A true
assembled-gate check (all four branches merged together) has NOT been run;
only each PR's quality gate in isolation. Flag this as a Phase 2 checklist
item if the validator wants a merged-gate rehearsal before the operator
merges for real.

## Archived (run history)
| Actor | Ticket | Outcome | Notes |
|-------|--------|---------|-------|
| agent:tb-20-delegator | TB-20 | review (PR #22 OPEN, head 8cb7dd3, base chore/add-hermes-cli-export-plan) | Run 2, press-ahead child of TB-18 (stacked; anchor #20 named on body line 1). Session-grain cache_read_tokens surfaced as report caveat; S32+T10 rows authored (S31 adjacency with PR #21 flagged). Full arc: plan self-review, own-review, validation evidence attached. One identity-block halt (c11 identify timeout) recovered by orchestrator mid-run; one deviation flagged (branch-base prompt wording vs actual, followed actual). Surface closed 06:42. Awaiting operator merge (#20 first, then retarget #22 to main + rebase). |
| agent:tb-18-delegator | TB-18 | review (PR #20 OPEN, head 5c74901, MERGEABLE) | Run 2, Tasks 3-6 (Tasks 0-2 pre-run). Ran in ROOT checkout (logged constraint) — no git damage, .lattice left dirty for orchestrator closeout commit as instructed. Four-case cache render (yes/no/n-a/n-a*), probe fixtures carry requestId, probe refuses non-isolable turns, S29/S30 docs + README + ticket correction (token_count dead column). Own-review PASS, one deviation flagged+resolved (render_report kwargs). Validated on 64-session corpus. PR body rewritten via gh api PATCH, overlap named. Surface closed 06:12. Awaiting operator merge. |
| agent:tb-19-delegator | TB-19 | review (PR #21 OPEN, head 46007bb) | Run 2. Fast-track, full arc, all statuses verified. Documented gate switched unittest-discover→`uv run pytest -q` (37 module-level tests were silently skipped); S31 rows authored in SPEC/EVALUATION/BUILDPLAN per contract-gap policy; PR body names README/EVALUATION overlap with PR #20 (union merge expected). Review artifact + validation evidence attached. Surface closed 06:01. Awaiting operator merge. |
| agent:tb-2-delegator | TB-2 | review (PR #1 OPEN) | Clean scaffold: pyproject/uv.lock, toolbench/{transcript(62L: ToolCall+result_len), passive, probe stubs}, tests/test_transcript.py (75L). Full arc, all statuses bumped correctly. Surface closed. Flagged gh footguns (op alias fails headless; token lacked PR scope → auth switch). Awaiting operator merge. |
| agent:tb-3-delegator | TB-3 | review (PR #3 OPEN) | parse_session + ParseResult + block-local precedence + malformed/interrupted + fixtures. Off tb-2-scaffold. Surface closed. Awaiting operator merge. |
| agent:tb-4-delegator | TB-4 | review (PR #2 OPEN) | sources.py (SessionRef, iter_session_files, AgentsView cursor pagination, open_session_jsonl, index-source policy) + fake-runner tests (243L). __init__.py untouched (no TB-3 collision). Off tb-2-scaffold. Surface closed. Awaiting operator merge. |

| agent:tb-5-delegator | TB-5 | review (PR #5) | passive.py incremental reducer + 4-section report + CLI + exit contract. Off integration/substrate. Flagged deviations (a kwarg, heuristic subagent tool-name set, pre-existing --agent no-op under raw mode) in plan+comment. Surface closed. VERIFY S11 no-corpus-list in Phase 2. Awaiting operator merge. |
| agent:tb-6-delegator | TB-6 | review (PR #4) | probe.py sentinels + tool-name verify + comparison table + active-probes.md (5 vendored paths). Off tb-3-parse. Surface closed. Awaiting operator merge. |
| agent:tb-7-delegator | TB-7 | review (PR #6) | README updated to shipped impl + strict gate green. Off integration/full. Surface closed. FINAL ticket — Phase 1 complete. Awaiting operator merge. |

### Notes
- integration/substrate = merge(tb-3-parse, tb-4-sources); clean, 40 tests green; pushed. TB-5 based on it.
- integration/full = merge(tb-5-passive, tb-6-probe) = ALL tickets. Clean. Orchestrator-verified assembled gate GREEN: ruff clean, mypy --strict clean (10 files), 93 unittest OK exit 0. TB-7 based on it. NIT: a probe test prints the comparison table to stdout (test hygiene, not a failure) — flagged to TB-7 + Result Validator.
- ⚠️ USAGE: 76% weekly at tick 4; Result Validator downgraded Opus→Sonnet. Watch ceiling through TB-7 + validation.
