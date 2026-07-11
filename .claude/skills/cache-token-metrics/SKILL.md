---
name: cache-token-metrics
description: Measure a lattice-orchestrator run's cache-token cost (read + creation, per run, normalized per ticket) from its raw Claude transcripts — steps 1-4 of the token benchmark. Use when benchmarking orchestration token footprint, comparing a run before/after a change, or validating the lattice token-reduction levers. Standalone reader that runs before TB-26/TB-27 land; engine is `toolbench.cache_tokens`.
---

# cache-token-metrics

Answers one question: **did a change actually cut a run's token cost?** It reads the raw
Claude Code transcripts of a lattice run, sums `usage` per session, folds the run's
sessions into one total, and normalizes per ticket so runs of different size compare.

Deliberately a **standalone reader**, separate from the passive analyzer's
`ParseResult`/Summary path — it works today, before TB-26 wires cache sums into the
analyzer and TB-27's `--run-manifest` automates the grouping this does by hand.

**Runs against `~/.claude/projects`** — invoke from `~` (cwd hygiene), not from a project
checkout, so the read doesn't bill an unrelated project's cache.

## The measurement (recipe steps 1-4)

1. **Pick two comparable runs.** Best: replay one small throwaway contract, levers off vs
   on. Cheapest: one recent pre-change run vs one new post-change run of *similar ticket
   count*. Record each run's `.lattice/orchestration/agents.md`, its Lattice ticket count,
   and its time window from `run-state.md`.
2. **Build the run manifest.** From each run's `agents.md` archive table, take its
   worktrees/branches + time window; the run's sessions are the
   `~/.claude/projects/*/<uuid>.jsonl` transcripts whose cwd matches a run worktree and
   whose mtime falls in the window. Write one transcript path per line to a manifest file.
   (No run-id exists in transcripts — this correlation *is* the manifest, and is exactly
   what TB-27 will automate.)
3. **Sum per session** — `toolbench.cache_tokens` reads each transcript and sums
   `cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`,
   `output_tokens` over messages that carry `usage`.
4. **Aggregate + normalize** — it folds the manifest's sessions into one `RunUsage` and, with
   `--tickets N`, reports per-ticket figures.

```bash
# from ~ , per run:
uv run --project ~/tool-benchmarks python -m toolbench.cache_tokens \
    --manifest run-A.manifest --tickets 12
# or pass transcript paths directly:
uv run --project ~/tool-benchmarks python -m toolbench.cache_tokens \
    ~/.claude/projects/<proj>/*.jsonl --tickets 12 --json
```

## Reading the result — the one trap

- **Win** = `TOTAL_BILLED`/ticket ↓ **with** `cache_read`/ticket ↓ **and** `cache_creation`
  flat-or-down.
- **Fake win** = `cache_read` ↓ but `cache_creation` ↑ by ~the same amount. A prefix-sharing
  change (per-ticket context extracts vs a shared contract) just moved cost between the two
  buckets; `TOTAL_BILLED` is unchanged. **This is why the reader always prints creation next
  to read** — cache-read alone misleads (S39). The eval
  `test_prefix_sharing_trap_conserves_total` pins exactly this.
- **Guardrails to check alongside tokens:** wall-clock (`run-state.md` timestamps),
  full-contract-escalation count (delegator completion comments — the Standard Clause 13
  flag), Result Validator pass rate. A token win that raises escalations or fails validation
  is not a win.
- **Caveats to state in any writeup:** observational (uncontrolled) comparisons are confounded
  by run difficulty — normalize per ticket and say so; n=1-vs-1 is directional, not
  significant. The `/session-report` skill cross-checks these numbers from the same data via
  an independent implementation.

## Evals

Fixture-backed pytest, under the repo's standard gate (`tests/test_cache_tokens.py`,
fixtures in `tests/fixtures/cache_tokens/`):

```bash
cd ~/tool-benchmarks && uv run pytest -q tests/test_cache_tokens.py
```

They pin the S39 contracts: read+creation summation, `0`-not-`None` when usage is present
with zero cache, `None` when unmeasured, run aggregation with an unmeasured-session count,
per-ticket normalization (and its `tickets > 0` guard), and the prefix-sharing trap above.

## Engine & scope

`toolbench/cache_tokens.py` — dependency-free; `sum_session` / `sum_run` / `per_ticket` +
a `python -m` CLI. It does **not** touch `passive.py`; integrating cache sums into the
production analyzer is TB-26, and per-run grouping via `--run-manifest` is TB-27 (this skill
is TB-27's manual precursor).
