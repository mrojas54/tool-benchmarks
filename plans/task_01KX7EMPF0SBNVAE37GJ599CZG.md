# TB-27 — per-run cache-token grouping (S40)

Spec: S40 · Buildplan: T17 · Closed by PR #46 (merged `95dadce`)

## Premise correction

The ticket as minted was wrong on both halves, and the build deliberately does
something else.

**1. `agents.md` cannot be the manifest.** Its `Active` table — the only one with
`Branch`/`Worktree` columns — is overwritten each dispatch tick and collapses on
completion to `(none — dispatch complete)`. The surviving `Archived` table has no
branch column at all. By the time a run is measurable (always after it finishes),
the key we would filter on is gone. The manifest is **JSON**, emitted by the
orchestrator **at dispatch**, per the S37 freeze precedent.

**2. "The run's session set" is not well defined.** 29 of 158 sessions straddle
more than one `gitBranch`, and `agents.md:15` logs a delegator that "Ran in ROOT
checkout" — so neither branch nor `cwd` partitions sessions by run. Attribution
is **per-entry**, by that entry's `gitBranch`. Verified lossless: 1834/1834
usage-bearing entries carry one.

`SPEC.md` S40, `BUILDPLAN.md` T17 and this ticket's description were all corrected.
Leaving them would have left the contract asserting behavior the code refuses to
have.

## Why per-entry matters — measured, not argued

Live smoke, run `tb-21-23` (TB-21/22/23) against the real corpus:

    Run cache tokens: read=67,727,351 creation=2,605,747 (14 candidate sessions)
    per ticket (3):   read=22,575,783.7
    unattributed:     read=25,079,909 (same-session work off the run's branches)

25.1M cache-read tokens of those candidate sessions' spend went to branches
outside the run. The naive session-grain fold this ticket originally specified
would have charged every one of them to it — **~92.8M instead of 67.7M, a 37%
over-count on real data**.

That over-count is now unmergeable by accident:
`test_straddling_session_does_not_donate_its_whole_total` was mutation-verified —
implementing the naive fold makes it fail with `assert 10400 == 400`.

## Build

Seven tasks, subagent-driven (fresh implementer + reviewer each), three fix waves,
plus an independent whole-branch review.

- `ClaudeParser` buckets each entry's `usage` by `gitBranch` into `usage_by_branch`,
  in its existing pass (no second interpreter), **additive** beside the S39 session
  totals so TB-26 cannot regress. `session total == sum of buckets` is an enforced
  invariant.
- `toolbench/run_manifest.py` reads the JSON manifest and refuses loudly: empty
  branch set, malformed `run`, non-JSON, non-UTF-8.
- `Reducer` folds in-set branches into `RunStats`; `unattributed` is the straddle
  spillover, scoped to **candidate sessions** (>=1 entry on a run branch).
- Report renders read + creation together, per-ticket normalized, zero-match
  branches named. Caveat only, never ranked (S19).
- `toolbench/cache_tokens.py` deleted, as its own docstring scoped it. Every eval
  it carried was re-homed and verified before deletion.

## Review found three defects in the plan

- A malformed `run` key coerced silently to `""` (operator adjudicated: the prose
  contract governs — now raises).
- A non-UTF-8 manifest crashed with an uncaught `UnicodeDecodeError` (it subclasses
  `ValueError`, not `OSError`).
- A `git rm` that would have deleted fixtures shared with `test_parsers.py` — caught
  and refused by the implementer.

The final reviewer re-implemented the fold from scratch, reproduced all four live
figures exactly, and checked the additivity invariant across 600 real transcripts
(582 measured sessions, 0 breaks).

## Outcome

Gate green on merged main: ruff clean, `mypy --strict` clean, **381 passed**.
Feature verified running from a clean checkout of main.

## Follow-ups filed

- **TB-28** — the fold is blind to detached-`HEAD` sessions: `gitBranch: "HEAD"`
  entries are neither counted nor booked as `unattributed`, and `missing_branches()`
  stays silent. Zero impact on `tb-21-23` (verified); a future detached delegator
  would undercount silently.
- **TB-29** — `--exclude-subagents` is a silent no-op (`sources.py:102` tests the
  wrong path segment), so the report prints `Subagents included: no` while including
  them. Pre-existing.
