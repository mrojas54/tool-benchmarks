---
name: cache-token-metrics
description: Measure a lattice-orchestrator run's cache-token cost (read + creation, per run, normalized per ticket) from its raw Claude transcripts — steps 1-4 of the token benchmark. Use when benchmarking orchestration token footprint, comparing a run before/after a change, or validating the lattice token-reduction levers. Engine is `toolbench.passive --run-manifest` (S40): run-grain grouping is a dimension ON the passive analyzer, not a standalone reader.
---

# cache-token-metrics

Answers one question: **did a change actually cut a run's token cost?** It reads the raw
Claude Code transcripts of a lattice run, sums `usage` per session, folds the run's
sessions into one total, and normalizes per ticket so runs of different size compare.

Session summing goes through **`ClaudeParser`** (S39 / CQ 1.2) — the same parse path as
every other passive-analyzer report. Run-grain grouping (`--run-manifest` / `--tickets`)
is TB-27 / S40: it landed as a grouping dimension on `toolbench.passive` itself, so there
is one analyzer and one number — no separate run-aggregation CLI to drift against it.

**Runs against `~/.claude/projects`** — invoke from `~` (cwd hygiene), not from a project
checkout, so the read doesn't bill an unrelated project's cache.

## The measurement (recipe steps 1-4)

1. **Pick two comparable runs.** Best: replay one small throwaway contract, levers off vs
   on. Cheapest: one recent pre-change run vs one new post-change run of *similar ticket
   count*. Record each run's `.lattice/orchestration/agents.md`, its Lattice ticket count,
   and its time window from `run-state.md`.
2. **Build the run manifest.** JSON, written by the orchestrator **at dispatch** (agents.md
   discards its Branch column once the run finishes, so reconstructing it after the fact
   isn't reliable): `{"run": "2", "tickets": ["TB-18", "TB-19"], "branches":
   ["feat/tb-18", "tb-19-pytest-gate"], "worktrees": ["~/wt/tb-19"]}` (`worktrees` is
   optional and currently unused for attribution — accepted/stored only;
   `branches` is not — an empty or missing list is refused as malformed rather
   than silently attributing nothing). No run-id exists inside a transcript — the branch
   set in this manifest *is* the correlation.
3. **Sum per session** — the passive analyzer's `ClaudeParser` stamps session-grain
   `cache_read` / `cache_creation` / input / output *and* buckets that same usage by each
   entry's `gitBranch` (S40), so a session that straddles branches only donates the entries
   that actually touched the run's branches, not its whole total.
4. **Aggregate + normalize** — `--run-manifest` folds every scanned session's matching
   branch buckets into one run total (reporting unattributed spillover and any manifest
   branch that matched zero entries); `--tickets N` overrides the per-ticket divisor,
   otherwise `len(manifest.tickets)` is used (`--tickets` alone is a no-op).

```bash
# from ~ , per run. `toolbench` is installed editable into the repo's venv (src
# layout), so `uv run --project` resolves it from any cwd -- no PYTHONPATH, and
# no `cd`ing into the repo, which would defeat the cwd hygiene above.
# Discovery flags (--agent/--project/--since/--limit) still bound which sessions get
# scanned; --run-manifest then filters+folds by branch within that scan.
uv run --project ~/tool-benchmarks toolbench passive \
    --agent claude --run-manifest run-A.json --tickets 12
```

## Reading the result — the traps

- **Win** = `TOTAL_BILLED`/ticket ↓ **with** `cache_read`/ticket ↓ **and** `cache_creation`
  flat-or-down.
- **Fake win** = `cache_read` ↓ but `cache_creation` ↑ by ~the same amount. A prefix-sharing
  change (per-ticket context extracts vs a shared contract) just moved cost between the two
  buckets; `TOTAL_BILLED` is unchanged. **This is why the reader always prints creation next
  to read** — cache-read alone misleads (S39). The eval
  `test_prefix_sharing_trap_read_drop_offset_by_creation_rise` pins exactly this.
- **Incomplete run total** = a `detached-HEAD (unattributable)` line appears. Detached
  checkouts stamp `gitBranch="HEAD"`, which can never match a manifest branch (TB-28). That
  usage is named (including input/output, so an uncached first turn is not invisible) and
  **never folded into the run** — folding would fabricate attribution; dropping would
  silently undercount. Treat a large detached line as "the headline may be low."
- **Narrow slice** = a large `unattributed` line. That is same-session work on non-run
  branches inside candidate sessions (S40), not corpus-wide `main`. The run total is only
  the in-set entry slice.
- **Guardrails to check alongside tokens:** wall-clock (`run-state.md` timestamps),
  full-contract-escalation count (delegator completion comments — the Standard Clause 13
  flag), Result Validator pass rate. A token win that raises escalations or fails validation
  is not a win.
- **Caveats to state in any writeup:** observational (uncontrolled) comparisons are confounded
  by run difficulty — normalize per ticket and say so; n=1-vs-1 is directional, not
  significant. The `/session-report` skill cross-checks these numbers from the same data via
  an independent implementation.

## Evals

Fixture-backed pytest, under the repo's standard gate — no dedicated test file for this
skill; the contracts live where the code that implements them lives:

```bash
cd ~/tool-benchmarks && uv run pytest -q tests/test_parsers.py tests/test_reducer.py tests/test_run_manifest.py
```

- **`tests/test_parsers.py`** — per-session read+creation summation, `0`-not-`None` when
  usage is present with zero cache, `None` when unmeasured, and `gitBranch` bucketing
  (including the no-`gitBranch`-on-the-entry case, which buckets under `""`).
- **`tests/test_reducer.py`** — run aggregation restricted to manifest branches, the
  straddling-session counter-trap (a session touching a run branch for one entry donates
  only that entry, not its session total), missing-branch reporting, per-ticket
  normalization (and its `tickets > 0` guard), the prefix-sharing trap
  (`test_prefix_sharing_trap_read_drop_offset_by_creation_rise`), and the detached-HEAD
  blind spot (named, not folded, not mislabelled as `unattributed`; an uncached detached
  turn with real input/output is still surfaced — TB-28).
- **`tests/test_run_manifest.py`** — the run-manifest JSON reader itself (malformed/empty
  `branches`, non-JSON input, UTF-8 errors).

## Engine & scope

`src/toolbench/passive.py` — `ClaudeParser` (which stamps `ParseResult.session_cache_*` /
input / output / `usage_by_branch`) feeds a `Reducer`; when `--run-manifest` names a run
(`src/toolbench/run_manifest.py`), the reducer folds only the manifest's branches into a
`RunStats` and, with `--tickets N`, reports per-ticket figures. Run-grain grouping is a
dimension on the one analyzer (TB-27 / S40) — there is no separate CLI for it. (The prior
standalone run-aggregation module that held this before `--run-manifest` landed has been
retired; its evals are re-homed above.)
