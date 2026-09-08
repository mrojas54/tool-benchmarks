# TB-27 — per-run cache-token grouping (spec S40)

Date: 2026-07-12
Ticket: `TB-27` (buildplan `T17`)
Depends on: `TB-26` / `S39` (session-grain cache sums), landed
**Status:** shipped (TB-27 / PR #46). Body below is the design-time snapshot
(example paths may be flat-layout). Live contract rows: SPEC/EVALUATION/BUILDPLAN
**S40**; operator recipe: [`.claude/skills/cache-token-metrics/SKILL.md`](../../../.claude/skills/cache-token-metrics/SKILL.md).
`--tickets` without `--run-manifest` is a no-op; empty/missing `branches` exits 1;
optional `worktrees` is unused for attribution.

## Why this exists

The lattice token-reduction work needs a before/after number per **run**: what
did one orchestration run cost in cache tokens, normalized per ticket. Session
grain (S39) is not that number — a run spans many sessions, and a session can
outlive the run.

## The premise correction

TB-27 was minted as: *"`--run-manifest <agents.md>` folds a lattice run's session
set into one reducer."* Investigation before implementation found that sentence
contains two claims that do not hold, and both change the design.

**1. `agents.md` cannot serve as the manifest.** It is a narrative document, not
a machine-readable one. Its `Active` table — the only one carrying `Branch` and
`Worktree` columns — is overwritten each dispatch tick, and on run completion
collapses to a single row:

    | (none — dispatch complete 2026-07-10 06:42; all three tickets at review) |

The `Archived` table that survives has a *different* schema
(`Actor | Ticket | Outcome | Notes`) with **no branch column at all**; branches
appear only inside free prose. So by the time a run is measurable — which is
always after it finishes — `agents.md` has already discarded the key we would
filter on.

**2. "the run's session set" is not a well-defined object.** Two facts kill the
clean partition it assumes:

- **Sessions straddle branches.** 29 of 158 sessions in this project span more
  than one `gitBranch` (e.g. `8ced3766… → [chore/lattice-doctor-fix,
  feat/tb-21-discovery-reconciliation, feat/tb-23-typed-skips, main]`). A
  session is not owned by one run.
- **Delegators do not always run in worktrees.** `agents.md:15` logs
  `tb-18-delegator` as having *"Ran in ROOT checkout"*. Its transcript therefore
  sits in the root project slug alongside ordinary interactive work, so `cwd`
  alone cannot discriminate it.

The ticket text and BUILDPLAN `T17` must be corrected to match this design.

## The run-grain criterion (S40)

> **A run's cost is the sum of usage on every transcript *entry* whose
> `gitBranch` is in the run's branch set.** Attribution is per-entry, not
> per-session. An entry whose branch falls outside the set is *unattributed* —
> counted and reported, never silently dropped.

This is viable because **every usage-bearing entry carries `gitBranch`** —
verified 1834/1834 across the project's transcripts. Entry-grain attribution is
therefore lossless: no billed token lacks a branch to attribute it to.

**What `unattributed` is measured over.** A *candidate session* is one with at
least one entry on a run branch. `unattributed` is the usage on **non-run
branches within candidate sessions** — i.e. the straddle spillover — and nothing
else. Sessions that never touch a run branch are simply not part of the run and
contribute to neither figure.

This scoping is what makes the number mean something. Measured against the whole
corpus, `unattributed` would be dominated by every unrelated session on `main`
and would read as alarming noise in every run. Scoped to candidate sessions, it
answers the one question worth asking: *how much of the work interleaved with
this run did we decline to charge to it?* A large value means the run's
delegators were doing substantial off-branch work in the same sessions, and the
run total is correspondingly a narrower slice of what was actually spent.

It is also the only criterion that survives both facts above. A session-grain
rule must either over-count (a session that touched a run branch once donates its
entire total) or under-count (straddling sessions dropped), and the data says
~18% of sessions straddle.

## Architecture

```
run.json (manifest)  ──┐
  run, tickets,        │
  branches, worktrees  │
                       ▼
~/.claude/projects/**  ──►  ClaudeParser  ──►  ParseResult
                              buckets usage        session_cache_read_tokens      (S39, UNCHANGED)
                              by gitBranch         session_cache_creation_tokens  (S39, UNCHANGED)
                              in the same pass     usage_by_branch: {br -> sums}  (NEW)
                                                        │
                                                        ▼
                                                   Reducer ──► RunStats
                                                     folds only branches ∈ run set
                                                     everything else -> unattributed
                                                        │
                                                        ▼
                                                   Report: per-run section
                                                     totals, per-ticket normalized,
                                                     unattributed count
```

### Components

**Run manifest — JSON.** Format follows the existing `--freeze` manifest
precedent (S37, `toolbench/freeze.py` `write_manifest`/`read_manifest`); no new
format is invented, and the stdlib-only posture (S20) holds.

```json
{
  "run": "2",
  "tickets": ["TB-18", "TB-19", "TB-20"],
  "branches": ["feat/tb-18-usage-provenance", "tb-19-pytest-gate", "tb-20-cache-read"],
  "worktrees": ["~/tool-benchmarks/worktrees/tb-19-pytest-gate"]
}
```

The orchestrator emits this **at dispatch**, while the branch data is still live.
That is the direct fix for `agents.md` discarding it. `tickets` supplies the
per-ticket normalization denominator when `--tickets` is omitted. `worktrees` is
an optional field that is **accepted and stored, then ignored** — Lattice TB-28
rejected consuming it as a scan-narrowing hint, because the root-checkout
delegator proves `cwd` cannot be trusted to define membership. Attribution is
branches-only (`gitBranch` ∈ `branches`).

**`ClaudeParser`.** Gains one additive field, `usage_by_branch`, bucketing
per-message `usage` by that entry's `gitBranch`. Computed in the existing single
pass — no second JSONL interpreter (the CQ 1.2 rule). The S39 session totals are
computed exactly as they are today and are **not** disturbed.

**`Reducer`.** A new `RunStats` fold sums the buckets whose branch is in the run
set; out-of-set buckets accumulate into `unattributed`. Cache **read and creation
travel together** — per S39, a read drop that raises creation by roughly the same
amount is a prefix-sharing artifact, not a win, and read alone misleads.

**`Report`.** A per-run section: run totals, per-ticket normalized figures, and
the unattributed tally. It is a **caveat surface only** — never folded into a
ranking or an inefficiency ratio (S19, S39).

**CLI.** `passive --run-manifest <run.json> [--tickets N]`.

### Module boundary

`toolbench/cache_tokens.py` is **deleted**. Its own docstring scopes it as
holding run aggregation *"until TB-27's `--run-manifest` lands on the passive
analyzer"* — that condition is now met. `sum_run` / `per_ticket` move into the
reducer, and the `cache-token-metrics` skill re-points at `toolbench.passive`.

Keeping both would leave two code paths computing the same number. That is
precisely the drift TB-26 was just bitten by, and AGENTS.md already flags a
"third CLI" as a smell.

## Error handling

House style (S23 skipped-roots, S38 unjoinable): **name the gap, never swallow
it.**

- **Unattributed usage is counted and rendered** beside the run total, scoped to
  candidate sessions as defined above. A run whose candidate sessions are mostly
  unattributed is a measurement not to be trusted, and the report must say so
  rather than print a confident number over a thin base.
- **A manifest branch matching zero entries is reported, not silently zero.**
  That is the signature of a typo'd, renamed, or never-pushed branch; left
  silent it reads as "this ticket cost nothing."
- **Empty / missing `branches`** → `MalformedRunManifest`, passive exits 1
  (same hard-stop family as a bad freeze path). A run with no branch set would
  attribute nothing and print a confident zero — refuse rather than emit that.
- **`--tickets 0`** is rejected at argparse (`ArgumentTypeError`); `--tickets`
  without `--run-manifest` is a no-op.

## Testing (S40 eval row)

- Parser buckets usage by branch; a straddling session splits across two buckets.
  Fixture pinned to the real `8ced3766` shape (S24: fixtures pin *observed*
  shapes, not the shape the code expects).
- **Invariant:** session totals equal the sum of all buckets — pins that S39 /
  TB-26 is not regressed by the additive field.
- Reducer folds only in-set branches; out-of-set lands in `unattributed`.
- **Counter-trap:** a session touching a run branch for a *single* entry must not
  contribute its whole session total. This is the over-count the ticket's
  original "session set" framing would have shipped, and the test exists to make
  that failure loud.
- A zero-match manifest branch is reported, not silently zero.
- Date-range survival extends to `usage_by_branch` (the TB-25 → TB-26 invariant,
  now a standing habit for every field added to `ParseResult`).

## Out of scope

- Non-Claude agents. `gitBranch` is a Claude Code transcript field; codex/hermes
  runs have no equivalent and are excluded from run grouping, not faked.
- Retrofitting a manifest for historical runs. One can be hand-written, but the
  orchestrator emitting it at dispatch is the supported path.

## Contract rows

Landed with the implementation (PR #46):

- `SPEC.md` — **S40**, the run-grain criterion.
- `EVALUATION.md` — S40 eval row.
- `BUILDPLAN.md` — `T17` corrected (`run.json` + entry-grain; not `agents.md`).
- `TB-27` — ticket title/description corrected; status `done`.
