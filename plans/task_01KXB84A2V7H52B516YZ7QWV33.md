# TB-28 — run attribution is blind to detached-HEAD sessions

Spec: S40 · Closed by PR #47 (branch `fix/tb-28-29-run-attribution-gaps`)

## The bug

git stamps a literal `"HEAD"` as `gitBranch` when the checkout is detached.
`"HEAD"` is the **absence** of a branch, not a branch, so it can never appear in a
run manifest's branch set. Two leaks followed from that in `_absorb_run`:

1. A session whose entries are **all** on `HEAD` produces an empty `in_set`, hits
   the `not in_set` early return, and contributes to **neither** the run total
   **nor** `unattributed`. The run number comes out low with no failure signal.
2. A **candidate** session's `HEAD` usage fell into the `else` branch and was booked
   as `unattributed` — whose docstring promises *"work done in the same session on
   another branch"*. `HEAD` is not another branch.

Only (1) is a silent drop, but fixing only (1) would have left `unattributed`
quietly polluted, so both are routed to the same new bucket.

## The premise the ticket got right, and the one it got wrong

The ticket offered two fixes. The second — *"consume the already-parsed
`RunManifest.worktrees` field"* — is **not viable**, and `SPEC.md` S40 already said
so before the ticket was written:

> delegators do not always run in worktrees (one is logged as having "Ran in ROOT
> checkout"), so **neither branch nor `cwd` partitions sessions cleanly**

Confirmed at the code level too: `worktrees` is populated by nothing on disk — it
defaults to `()` and only a test fixture ever sets it. Attributing detached usage
via worktree/`cwd` would contradict S40's founding premise and fabricate a number.

So the ticket's **first** option is the only honest one: **name the gap.** A
detached delegator and unrelated detached work are genuinely indistinguishable —
the usage can be neither claimed nor disclaimed. Folding it into the run total
would invent an attribution; dropping it undercounts the run. Reporting it is the
only move that lies about nothing (S23/S38: report the gap, never a silent zero).

`SPEC.md` S40 was corrected: its "verified lossless — 1834/1834 usage-bearing
entries carry `gitBranch`" is *true about presence* and invited exactly the wrong
inference. **Presence is not attributability.** `"HEAD"` is present and still drops.

## What shipped

`RunStats` grows a `detached_*` bucket (`sessions`/`read`/`creation`/`input`/
`output`), booked **before** the candidate test so a fully-detached session cannot
early-return past it. The run section names it and never folds it into the total.

## Measurement (live corpus, `run tb-27` manifest)

Run total and `unattributed` are **byte-identical to pre-fix** — no fabricated
attribution, no double-count — while previously-invisible usage is surfaced:

```
- Run cache tokens (run tb-27): read=64094964 creation=4485953 (33 candidate sessions)
  - unattributed: read=18103825 creation=393622
  - detached-HEAD (unattributable): read=1012132 creation=101772 input=76987 output=6546
    (1 session; may include run delegators -- run total may be low)
```

Pre-fix, `unattributed` was identical (18,103,825), proving that session was the
**severe** leak — the silent drop, not the mislabel. Cross-checked against an
independent scan of the raw transcripts: `1,012,132 + 101,772 == 1,113,904`.

## What the review round added

**The first pass reproduced the very bug it was fixing.** The blind spot was gated
on cache tokens alone (`detached.read or detached.creation`) while the fold's
`continue` skipped `HEAD` unconditionally. An **uncached** detached turn — real
`input`/`output`, zero cache, the normal shape of a first turn — fell through the
gate *and* the `continue`, vanishing exactly as it had before the fix. The blind
spot is *"cost we could not attribute"*, not *"cache we could not attribute"*.

Worse, the first pass's own test (`test_zero_usage_detached_entries_do_not_raise_a
_false_alarm`) **cemented** it by conflating "zero cache" with "zero usage" — the
same test-ratifies-the-bug pattern that let TB-29 live for months. Now gated on
`_spent_anything()` (any of read/creation/input/output), with the counter-trap
pinned by `test_uncached_detached_session_is_still_a_blind_spot`.

The report line renders `input`/`output` too: a bare `read=0 creation=0` on the one
line whose entire job is to disclose what was missed would be a lie by omission.

## Follow-ups

None. The `""` bucket (missing/None `gitBranch`) was checked and is not a second
blind spot — `parsers.py` buckets it under `""`, which falls through to
`unattributed` normally, and no real entry carries it (1834/1834 have `gitBranch`).
