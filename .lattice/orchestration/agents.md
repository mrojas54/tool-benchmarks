# Agents — tool-benchmarks run

Active table overwritten each dispatch tick (Lattice + `c11 tree` are ground truth).

## Active
| Role | Ticket | Surface ref | Pane ref | Branch | Worktree | Phase | Last seen | Spawned at |
|------|--------|-------------|----------|--------|----------|-------|-----------|------------|
| (none — run complete) | | | | | | | | |

## Archived (run history)
| Actor | Ticket | Outcome | Notes |
|-------|--------|---------|-------|
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
