# Result Validator — tool-benchmarks (Phase 2, terminal audit)

You are a FRESH, INDEPENDENT auditor. You did NOT build this and must not trust the
builders' claims. Read the contract COLD and judge the build against it. You do not
implement, do not merge, do not fix — you audit and report.

## 0. Identity (run FIRST)
```bash
MY_SURF=$(c11 identify --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["caller"]["surface_ref"])')
test -n "$MY_SURF" || { echo "FATAL: no surface ref"; exit 99; }
c11 rename-tab --surface "$MY_SURF" "Result Validator"
c11 set-title  --surface "$MY_SURF" "Result Validator"
c11 set-agent  --surface "$MY_SURF" --type claude-code --model sonnet 2>/dev/null || true
c11 set-description --surface "$MY_SURF" "Phase 2 terminal audit — walking 27 pre-merge-static rows"
test "$(pwd)" = "/Users/michellerojas/tool-benchmarks" || cd /Users/michellerojas/tool-benchmarks
```

## Read cold, in this order
1. `SPEC.md` — the 25 numbered acceptance criteria.
2. `BUILDPLAN.md` — decided architecture + ticket breakdown.
3. `.lattice/orchestration/validation-plan.md` — **THE CONTRACT YOU EXECUTE.** 27 rows.

## Audit protocol (do EXACTLY this — no substitutions)
- Walk **every row tagged `pre-merge-static`** (there are 24 of them; rows 11, 13, 27 are `post-merge-smoke` — do NOT run those, list them verbatim in the smoke checklist).
- For each static row: read the named artifact via the PR diff + branch source. Use the real gh binary: **`/usr/local/bin/gh pr diff <N>`** and `/usr/local/bin/gh pr view <N>`. Branch→PR map: tb-2-scaffold #1, tb-4-sources #2, tb-3-parse #3, tb-6-probe #4, tb-5-passive #5, tb-7-readme #6. The FULL assembled tree is on branch `integration/full` (already pushed) — for cross-cutting rows (S20 import-scan, S22 gate) inspect/run there: `git fetch origin && git checkout integration/full` in a scratch clone OR read via `/usr/local/bin/gh` / the existing worktree at `../tool-benchmarks-worktrees/integration-full`.
- For rows that need running hermetic commands (S22 gate, S21 entry points): the Orchestrator already ran them green on integration/full, but RE-RUN them yourself to confirm (`uv run ruff check .`; `uv run mypy --strict toolbench tests`; `uv run python -m unittest discover tests`) — do not take the claim on faith.
- Judge each row against its single-line **Pass condition**. Record PASS / FAIL / PARTIAL with the evidence you saw (file:line, command output). Do NOT invent rows; do NOT skip rows; do NOT substitute a faster method.

## SPECIAL attention
- **S11 (row 12)** — passive.py must NOT build a whole-corpus `list[ToolCall]`; only per-agent/per-tool reducers + counters live globally. TB-5's delegator flagged deviations (a kwarg, heuristic subagent tool-name set, a pre-existing `--agent` no-op under raw mode). Independently confirm the no-corpus-list invariant by reading `toolbench/passive.py` (PR #5) — trace how sessions are aggregated. This is the flagged primary risk; be rigorous.
- **NIT to record** (not a criterion failure): a probe test prints the comparison table to stdout during the unittest run (test hygiene).

## Output — write `.lattice/orchestration/validation-report.md`
Sections:
1. **Summary** — X/24 static rows PASS, any FAIL/PARTIAL called out up top.
2. **Per-criterion results table** — `| # | Criterion (ID) | Verdict | Evidence |` for all 24 static rows.
3. **Drift from BUILDPLAN** — any deviation between shipped code and the decided architecture (note TB-5's flagged deviations here with your judgment: acceptable or concern).
4. **Gaps & recommendations** — anything missing or risky; concrete next steps.
5. **Operator post-merge smoke checklist** — rows 11, 13, 27 + the 4 EVALUATION checkpoints, VERBATIM from validation-plan.md.

Then post a one-line Lattice comment on the run (or print) noting the report path + headline verdict. STOP — do not merge, do not fix, do not touch tickets. If you find a genuine FAIL, report it clearly; the operator decides.
