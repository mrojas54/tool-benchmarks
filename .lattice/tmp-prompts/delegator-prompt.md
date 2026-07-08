# Delegator — TB-5 (passive.py reducer + report + CLI)

You OWN Lattice ticket **TB-5** end-to-end: plan → implement (TDD) → own-review →
validate → PR, landing at status **`review`**. You do NOT merge. This is the
largest ticket (7 SPEC IDs) — plan carefully.

## 0. Guards & identity (run FIRST, in order)

```bash
test "$(pwd)" = "/Users/michellerojas/tool-benchmarks-worktrees/tb-5-passive" || { echo "FATAL: wrong cwd"; exit 99; }
export LATTICE_SPAWN_BACKEND=headless
export LATTICE_ROOT=/Users/michellerojas/tool-benchmarks
MY_SURF=$(c11 identify --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["caller"]["surface_ref"])')
test -n "$MY_SURF" || { echo "FATAL: no surface ref"; exit 99; }
c11 rename-tab --surface "$MY_SURF" "TB-5 Delegator"
c11 set-title  --surface "$MY_SURF" "TB-5 Delegator"
c11 set-agent  --surface "$MY_SURF" --type claude-code --model sonnet 2>/dev/null || true
c11 set-description --surface "$MY_SURF" "TB-5: passive.py incremental reducer + 4-section report + CLI + exit contract"
```

## INSTALL FACTS (Lattice v0.2.0 classic)
- **Status ladder:** `backlog → in_planning → planned → in_progress → review → done`. NO `in_validation`/`pr_open`. Land at **`review`** and STOP.
- **Missing subcommands** — do NOT call: `lattice claim`, `plan-review`, `code-review`, `needs-human`. Use `--actor agent:tb-5-delegator` on every mutation. Reviews = own-reviewer fallback. Blocked-on-human → `lattice status TB-5 needs_human --actor agent:tb-5-delegator`.
- Every `lattice` mutation REQUIRES `--actor`.
- **Plan file:** ABSOLUTE path `$LATTICE_ROOT/.lattice/plans/<uuid>.md` (uuid from `lattice show TB-5 --json`).
- **Git:** remote `origin`. Branch `tb-5-passive` is based on **`origin/integration/substrate`** — an integration branch that ALREADY MERGES TB-3 (parser, PR #3) + TB-4 (sources, PR #2), so `toolbench/transcript.py` (parser) AND `toolbench/sources.py` are both present and the suite is green (40 tests). PR base `main`; body must say: "assembles #3 (TB-3 parser) + #2 (TB-4 sources) via integration/substrate — merge those first; this rebases onto main after."
- **`gh` FOOTGUNS:** the `gh` alias (`op plugin run -- gh`) fails headless AND pops a 1Password overlay. Use the real binary **`/usr/local/bin/gh`**. Before `gh pr create`: `/usr/local/bin/gh auth switch` to the keyring account (repo scope); confirm `/usr/local/bin/gh auth status`. If a 1Password overlay appears, press Escape and proceed.

## The ticket (SPEC S11, S12, S13, S14, S15, S19, S23 · BUILDPLAN T4)

New module `toolbench/passive.py` — the passive analyzer. Read `$LATTICE_ROOT/SPEC.md`
S11/S12/S13/S14/S15/S19/S23 as source of truth. Build on `sources.py` (discovery)
+ `transcript.py` (`parse_session`/`ParseResult`/`ToolCall`).

- **S11 incremental (CRITICAL).** Aggregation streams per parsed session. **NO full in-memory `list[ToolCall]` for the corpus** — only per-agent/per-tool reducers and report counters live globally. Your reducer accumulates counters as sessions stream in; it must never build a corpus-wide list. This is audited.
- **S12 CLI.** Flags: `--agent`, `--all | --project`, `--since`, `--date-from`, `--date-to`, `--out`, `--limit`, `--exclude-subagents`, `--index-source`, `--verbose`. Default scope `--agent all --all`.
- **S13 subagents.** Included by default; `--exclude-subagents` removes paths containing `/subagents/`.
- **S14 report — four sections in order:** (1) Agent breakdown, (2) Tool leaderboard (per agent+tool), (3) Inefficiency callouts (ToolSearch/deferral tax, failures, oversized outputs, subagent fan-out, churn), (4) Summary.
- **S15 provenance.** Report states: index source used, sessions scanned, tool calls joined, malformed-line count, whether subagents included, any AgentsView fallback reason; note that `--since` is file-mtime based.
- **S19 metric roles.** Context cost = joined result-payload tokens (`chars/4`) is the PRIMARY ranking. The cache flag is caveat-only and NEVER ranks tools. Failure/slow/retry-churn feed inefficiency callouts ONLY (never the ranking).
- **S23 error/exit contract.** Empty session selection → clear message, **exit 0**. Missing selected raw root → **exit 1** for a strict source; BUT `--agent all --index-source auto` continues with other sources and reports skipped roots.
- Entry point: runnable as `uv run python -m toolbench.passive` (TB-2 left a stub — replace it). Tests exercise the reducer (incremental, no corpus list), CLI arg parsing/defaults, subagent filter, report sections + provenance strings, metric ranking, and the exit-code contract (argv + tmp roots). Fully hermetic — no real `~/.claude`, no daemon.

Do NOT modify the parser, sources.py, or probe.py.

## Workflow (inline, own-reviewer)
1. **Plan.** `lattice status TB-5 in_planning --actor agent:tb-5-delegator`; plan → `$LATTICE_ROOT/.lattice/plans/<uuid>.md`; self-review (incremental-reducer design proving no corpus list, four section order, provenance fields, exit-code branches, ranking on context-cost); `lattice status TB-5 planned --actor agent:tb-5-delegator`.
2. **Implement (TDD).** `lattice status TB-5 in_progress --actor agent:tb-5-delegator`; `git fetch origin`, record "working against origin/integration/substrate @ <sha>"; tests first then implement; commit. Deviate-with-flag.
3. **Own-reviewer review.** `git log origin/integration/substrate..HEAD --stat` + per-file diffs; Verdict + findings (file:line); `lattice attach TB-5 --role review --inline "<review>" --actor agent:tb-5-delegator-reviewer`. Fix Critical/Major, re-review. Pay special attention to the S11 no-corpus-list invariant.
4. **Validate.** `uv run ruff check .`; `uv run mypy --strict toolbench tests`; `uv run python -m unittest discover tests` (all green); a smoke that `uv run python -m toolbench.passive --help` works. `lattice attach TB-5 --role validation --inline "<outputs>" --actor agent:tb-5-delegator`.
5. **PR.** `git push -u origin tb-5-passive`; verify head==origin. `/usr/local/bin/gh pr create --base main --head tb-5-passive --title "TB-5: passive.py reducer + report + CLI" --body "assembles #3 (TB-3) + #2 (TB-4) via integration/substrate — merge those first; rebases onto main after. SPEC S11/S12/S13/S14/S15/S19/S23. Validation green."`. Confirm head != base. `lattice attach TB-5 <pr-url> --type reference --actor agent:tb-5-delegator`; `lattice status TB-5 review --actor agent:tb-5-delegator`.
6. **Completion comment + STOP.** `lattice comment TB-5 "<landed, PR #, validation green, deviations, confirm S11 no-corpus-list>" --actor agent:tb-5-delegator`. STOP.

## Guardrails
- Never commit to main; never `cd` to the root repo before commit/push. Stay in this worktree.
- Read-before-Write on pre-existing files (transcript.py, sources.py, passive.py stub).
- Genuine human blocker → `needs_human` + comment; do not spin.
