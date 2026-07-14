# Result Validation Report — Run 3 (TB-34, TB-36, TB-37, TB-38)

Validator: Result Validator (cold read, no prior dispatch context).
Date: 2026-07-14.
Baseline (pre-run, `main` @ c07609a): 578 passed, 2 skipped, 3 subtests; ruff clean; mypy --strict 0 errors.

Note: this file previously held a report for an earlier run (2026-07-10, TB-18/19/20).
That content is superseded and replaced below; see git history for the prior version.

## Per-ticket verdicts

### TB-34 — PR #60 `fix/tb-34-zero-match-census-disclosure` — **PASS**

Adds an additive `_sampling_notes(...)` disclosure block to the `reducer.calls_joined
== 0` early return in `toolbench/passive.py` (~line 645). Verified against the real
diff (not the ticket recap):

- The original `"toolbench.passive: no sessions matched the given selection."` line
  (plus the skip tally suffix) is preserved byte-for-byte and printed first; the
  census lines are appended after via `"\n".join(lines)`, never replacing it.
- Reuses the existing `_sampling_notes` helper (`toolbench/report.py`) with the exact
  same six-argument signature already used by the non-empty render path — no
  reinvented rendering logic.
- All of `census`, `reducer`, `skips`, `args.limit`, `limit_truncated`, and
  `sampled_by_agent` are already in scope/computed above the early-return check
  (`sampled_by_agent` at line 588, well before line 645) — the ticket's core claim
  ("the run already built a full AgentCensus... discarding it here") checks out in
  the actual control flow, not just in prose.
- New tests (`ZeroMatchCensusDisclosureTests`, 2 cases) exercise real scenarios: an
  agent present in the archive but unreached by the window, and an unenumerated
  archive residual — both via a scripted `FakeRunner`, asserting on the actual
  printed text, not a shape/type check.
- SPEC.md/EVALUATION.md S35 updated accurately to describe the additive behavior.
- No test pinning the old message was weakened — the assertion is `assertIn` on the
  unchanged base string, consistent with the pre-existing tests the ticket named as
  needing to stay green (`test_passive_cli.py:274,283,304,352` etc., unmodified in
  this diff).

Isolated gate: ruff clean, mypy --strict clean, `580 passed, 2 skipped, 3 subtests`
(baseline + 2 new tests).

### TB-36 — PR #61 `chore/tb-36-probe-argv-sole-builder` — **PASS**

Routes `_probe_agentsview` through `_list_argv` (Option A from the ticket, the
structural fix, not the "leave it and comment" fallback).

- Verified `_list_argv(agent="all", project=None, since=None, limit=1, includes=())`
  reproduces the exact prior hand-rolled argv
  `["agentsview", "session", "list", "--json", "--limit", "1"]`: `_list_argv` only
  appends `--agent`/`--project`/`--date-from`/`--cursor` when those args are
  non-default, so all four are skipped here — argv is byte-identical to before.
- New test `test_probe_argv_is_built_by_the_sole_builder` asserts the exact argv via
  `FakeRunner`, not just that `_probe_agentsview` still returns `None` on success —
  it would catch a filter accidentally leaking in.
- Smallest diff of the four (10 lines prod, 16 test) and touches only
  `toolbench/sources.py` — no `passive.py` overlap.

Isolated gate: ruff clean, mypy --strict clean, `579 passed, 2 skipped, 3 subtests`
(baseline + 1 new test).

### TB-37 — PR #63 `feat/tb-37-freeze-manifest-census` — **PASS**

Bumps `MANIFEST_VERSION` to `toolbench-freeze-2`, persists an optional `AgentCensus`
in the manifest at freeze time, and adds a historical-denominator caveat on replay.

- `write_manifest`/`read_manifest` (`toolbench/freeze.py`) verified: `census` is an
  optional kwarg at write time; `read_manifest` branches on **key presence**
  (`data.get("census")`), not the version string — confirmed by
  `test_v2_manifest_with_no_census_degrades_same_as_v1` and a hand-written v1
  fixture with the literal old `"toolbench-freeze-1"` string and no `census` key
  (`test_v1_manifest_replay_degrades_gracefully_named_by_version`), both routed to
  the same `unavailable_reason` code path.
- Three-way branch in `passive.py`'s replay logic verified: no census → generic
  unavailable reason naming the manifest version; census present but itself
  `unavailable_reason`-carrying (freeze-time failure) → that reason is propagated
  verbatim, not laundered into generic text; real census → real fractions render
  plus a `frozen_census_note` wired through `render_report` and printed adjacent to
  the sampling notes it qualifies. All three paths have a dedicated test, each
  asserting on actual rendered report text (`"Historical denominator"`,
  `"1 of 1 (100.0%)"`, `"boom: census call failed"`, etc.), not shape checks.
- `residual` is correctly *not* persisted (derived property, reconstructed from
  `totals`/`archive_total`), avoiding a stored-vs-derived drift bug.
- Backward compatibility with a genuine v1 manifest (old version string, no key)
  verified explicitly, not just a same-run round-trip.
- SPEC.md nests the TB-37 addendum under the existing S37 bullet (rather than
  minting a new S-number) and EVALUATION.md extends the S37 row in place — consistent
  with how other tickets (TB-21/TB-22/etc.) have extended a criterion previously.

Isolated gate: ruff clean, mypy --strict clean, `585 passed, 2 skipped, 3 subtests`
(baseline + 7 new tests, split across `test_freeze.py` and `test_passive_cli.py`).

### TB-38 — PR #62 `fix/tb-38-auto-fallback-mid-listing` — **PASS**

Widens `_discover_refs`'s exception handling so `--index-source auto` falls back to
raw on a mid-listing `RuntimeError` **or** `AgentsViewTimeout` (both subclass
`RuntimeError`, confirmed via `test_agentsview_timeout_is_a_runtimeerror` on main),
gated to `auto` only — an explicit `--index-source agentsview` still raises.

- One code path for both failure modes, as the ticket demanded: a single new
  `except RuntimeError` block in `_discover_refs` (`toolbench/passive.py`) handles
  both the pre-existing nonzero-exit case and TB-32's timeout case identically —
  `classify_skip(exc)` picks `EXPORT_FAILED` vs `EXPORT_TIMEOUT` correctly per type.
- Partial agentsview refs are discarded, not spliced: refactored the ref-collection
  loop into a new `_collect_refs` helper whose local `refs` list is never assigned
  back to the caller until it returns cleanly — a mid-loop exception leaves the
  caller's `refs` untouched, so the fallback path starts a clean raw rescan via
  `iter_sessions(index_source="raw", ...)` rather than a second hand-rolled path.
  This matches TB-22's "no spliced/incoherent corpus identity" precedent, cited in
  both the ticket and the new code comments.
- The `FileNotFoundError` branch (vanished binary) is deliberately **not** widened —
  it keeps the pre-existing `MISSING_SOURCE`/no-rescan handling, and a dedicated test
  (`test_source_vanishing_after_a_healthy_probe_is_unaffected`) pins that boundary.
  This is the correct scope: the ticket only asked to fix the "unhealthy-but-present"
  failure modes, not the "vanished" one.
- New tests (`MidListingAutoFallbackTests`, 4 cases) each assert on real `_discover_refs`
  return values (`refs`, `fallback_reason` substring, `skips[0].reason`, `census.totals`,
  `truncated`), not just exit codes.
- Old test `test_mid_discovery_timeout_is_fatal_like_any_other_source_error` was
  **renamed and re-described**, not weakened: its assertion (`assertRaises` for both
  `AgentsViewTimeout` and plain `RuntimeError` out of `iter_sessions` itself) is
  unchanged — the docstring now correctly explains that `iter_sessions` still raises,
  and it's `_discover_refs` one layer up (new in this PR) that recovers. Confirmed by
  diff: no assertion lines changed, only comments/name.
- **Scope-boundary correction from the orchestrator's framing**: this PR does *not*
  touch `toolbench/sources.py` production code at all — only `tests/test_sources.py`
  (a comment/rename in an already-passing test) and `toolbench/passive.py`. The
  dispatch prompt's premise that "TB-36/TB-38 both claim disjoint regions of
  `sources.py`" doesn't hold as stated: TB-38's actual fix lives in `passive.py`'s
  `_discover_refs`, not `sources.py`. Disjointness with TB-36 holds trivially (TB-38
  has no production changes there), but this is worth noting as drift from the
  dispatch framing, not a defect in the PR itself.

Isolated gate: ruff clean, mypy --strict clean, `582 passed, 2 skipped, 3 subtests`
(baseline + 4 new tests).

## Assembled-gate check (all four merged onto `origin/main`)

Performed in a throwaway worktree/branch (`tb-run3-assembled-gate-scratch`, off
`origin/main` @ `c07609a`), merged in ticket order TB-34 → TB-36 → TB-37 → TB-38,
**not pushed**, and removed afterward (`git worktree remove --force` + `git branch -D`).

- TB-34 → main: clean merge.
- TB-36 → +TB-34: clean merge (different files entirely: `sources.py` vs `passive.py`).
- TB-37 → +TB-34,36: clean merge, including `toolbench/passive.py` (auto-merged
  despite TB-34 and TB-37 both touching that file — confirmed disjoint hunks: TB-34's
  change is in the `calls_joined == 0` early-return block, ~line 645; TB-37's is in
  the replay branch, ~lines 441–483, plus the `render_report(...)` call site).
- TB-38 → +TB-34,36,37: **one conflict**, in `tests/test_passive_cli.py`'s import
  block — TB-37 added `AgentCensus` and TB-38 added `AgentsViewTimeout` to the same
  `from toolbench.sources import (...)` statement. Trivial, mechanical: both names
  kept, no logic conflict. `toolbench/passive.py` itself (all three of TB-34/37/38
  touch it) merged with **zero conflicts** — confirmed disjoint hunks: TB-38's changes
  are the new `_collect_refs` helper and the widened `except RuntimeError` inside
  `_discover_refs` (~lines 347–440), well clear of both TB-34's and TB-37's regions.

After resolving the one import-ordering conflict and committing the merge:

- `uv run ruff check .` → **All checks passed.**
- `uv run mypy --strict toolbench tests` → **Success: no issues found in 35 source files.**
- `uv run pytest -q` → **592 passed, 2 skipped, 3 subtests passed** — exactly
  578 (baseline) + 2 (TB-34) + 1 (TB-36) + 7 (TB-37) + 4 (TB-38) = 592, confirming no
  test was lost, duplicated, or silently dropped by the merges.

**Assembled-gate verdict: PASS**, modulo the one mechanical import-conflict a real
merge (e.g. via GitHub's merge UI or a maintainer rebase) will also need to resolve
by hand — it is not auto-mergeable by GitHub's three-way merge without a human/CI
touching that one line, but it is not a design conflict, just two independent PRs
appending a name to the same import statement.

## Drift from ticket/delegator claims

- **TB-38 scope-boundary framing**: see above — the PR does not touch
  `toolbench/sources.py` production code, contrary to the implied disjoint-region
  framing in the dispatch prompt. Not a defect; just a correction for the record.
- No other drift found. All four PRs' actual diffs matched their tickets' FIX
  SKETCH and their lattice event-log "Delegator plan" comments. No loosened
  assertions, no shape-only tests standing in for behavior tests, no scope creep
  into files outside each ticket's stated area.

## Operator post-merge smoke-checklist

Everything below needs human eyes because it depends on live systems the hermetic
suite fakes (`FakeRunner`) rather than exercises for real:

1. **TB-34/TB-37 shared machinery** — run `passive` with a deliberately narrow
   `--since`/`--date-from` window against a real `~/.claude/projects` tree (or via
   `--index-source agentsview` against a healthy daemon) until it hits zero matches;
   confirm the new census disclosure lines actually appear and name a real agent/
   archive-total, not just pass in the fixture.
2. **TB-37 freeze/replay round-trip on real data** — `--freeze <path>` once against a
   real corpus, inspect the manifest JSON for a populated `census` key, then replay
   and confirm the report shows real fractions plus the "Historical denominator"
   caveat text. Also worth doing once against an **old, pre-existing v1 manifest**
   left over from before this change (if one exists on disk) to confirm the
   graceful-degrade path fires outside the test fixtures.
3. **TB-38 mid-listing fallback against a real flaky daemon** — the ticket's own
   language flags this as hard to fully fixture-test. With `--index-source auto`,
   kill or block the `agentsview` daemon *after* it answers the initial `--limit 1`
   probe but *during* the full listing (e.g. `kill -STOP` on the subprocess, or point
   at a wrapper script that exits nonzero on the second call) and confirm the run
   completes via raw fallback with a `mid-listing` fallback_reason in the report,
   rather than exiting 1. Also spot-check that an explicit `--index-source
   agentsview` under the same failure still exits 1 (the deliberately unwidened path).
4. **TB-36** — low risk, but a single live `--index-source auto` run against a
   healthy daemon is enough to confirm the probe still returns cleanly with the new
   argv path (would show up immediately as a "fatal source error" if the argv were
   wrong, so this is a fast confirm, not a deep check).

## Process note (out of scope, flagged for the orchestrator)

A `PostToolUse:Bash` hook fired during this validation reporting "4 open failed
roborev reviews on main" and instructing to fix them. This is unrelated to the four
PRs under validation and outside this validator's mandate (no merges, no pushes, no
changes to `main`). Not acted on here — flagging for the orchestrator/operator to
route separately.
