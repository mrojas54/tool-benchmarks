---
name: cache-token-metrics
description: Measure a lattice-orchestrator run's cache-token cost (read + creation, per run, normalized per ticket) from its raw Claude transcripts — steps 1-4 of the token benchmark. Use when benchmarking orchestration token footprint, comparing a run before/after a change, or validating the lattice token-reduction levers. Engine is `toolbench.cache_tokens`, a run-aggregation façade over ClaudeParser (S39).
---

# cache-token-metrics

Answers one question: **did a change actually cut a run's token cost?** It reads the raw
Claude Code transcripts of a lattice run, sums `usage` per session, folds the run's
sessions into one total, and normalizes per ticket so runs of different size compare.

Session summing goes through **`ClaudeParser`** (S39 / CQ 1.2) — the same parse path as
the passive analyzer. `toolbench.cache_tokens` is the run-aggregation + CLI layer until
TB-27's `--run-manifest` lands on passive.

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
3. **Sum per session** — `toolbench.cache_tokens` delegates to `ClaudeParser`, which
   stamps session-grain `cache_read` / `cache_creation` / input / output from messages
   that carry `usage`.
4. **Aggregate + normalize** — it folds the manifest's sessions into one `RunUsage` and, with
   `--tickets N`, reports per-ticket figures.

```bash
# from ~ , per run. Invoke the reader BY FILE PATH: `-m toolbench.cache_tokens` only
# resolves from the repo root (toolbench isn't installed into the venv), whereas this
# skill runs from ~ for cwd hygiene — so the module form fails here, the path form works.
uv run --project ~/tool-benchmarks python ~/tool-benchmarks/toolbench/cache_tokens.py \
    --manifest run-A.manifest --tickets 12
# or pass transcript paths directly:
uv run --project ~/tool-benchmarks python ~/tool-benchmarks/toolbench/cache_tokens.py \
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

`toolbench/cache_tokens.py` — `sum_session` façades over `ClaudeParser` (which stamps
`ParseResult.session_cache_*` / input / output); `sum_run` / `per_ticket` + CLI remain
here for run-grain aggregation. Passive by path (`python toolbench/cache_tokens.py …`) or
`-m toolbench.cache_tokens` from the repo root. Per-run grouping via `--run-manifest` on
passive is TB-27 (this skill is that ticket's manual precursor).
