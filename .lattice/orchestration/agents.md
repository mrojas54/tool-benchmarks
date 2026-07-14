# Agents — tool-benchmarks (run 3: TB-34 · TB-36 · TB-37 · TB-38)

Active table overwritten each dispatch tick. Run 3 uses Agent-tool worktree
isolation, not c11 panes/surfaces — "Surface ref"/"Pane ref" are N/A this run;
ground truth is the Agent tool's background-task status plus verified git/PR
state. Run 1 + Run 2 delegator history retained under Archived.

## Active (Run 3, dispatched 2026-07-14, Phase 1)
| Role | Ticket | Branch | Worktree isolation | Phase | Spawned at |
|------|--------|--------|---------------------|-------|------------|
| delegator (inline-full) | TB-34 | `fix/tb-34-zero-match-census-disclosure` | Agent-tool worktree | dispatched | 2026-07-14 (Run 3 Phase 1) |
| delegator (fast-track) | TB-36 | `chore/tb-36-probe-argv-sole-builder` | Agent-tool worktree | dispatched | 2026-07-14 (Run 3 Phase 1) |
| delegator (inline-full) | TB-37 | `feat/tb-37-freeze-manifest-census` | Agent-tool worktree | dispatched | 2026-07-14 (Run 3 Phase 1) |
| delegator (inline-full) | TB-38 | `fix/tb-38-auto-fallback-mid-listing` | Agent-tool worktree | dispatched | 2026-07-14 (Run 3 Phase 1) |

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
