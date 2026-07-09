# TB-7: README + strict gate

README (agents/targets/run/index/metrics) then strict gate green: ruff check, mypy --strict toolbench tests, full unittest suite. PR.

SPEC: S22
BUILDPLAN anchor: T6
Depends on: T4, T5
## Delegator plan (filled in at plan phase)

Scope: docs + gate confirmation only. No behavior changes to transcript/sources/passive/probe modules.

1. Work against `origin/integration/full` (already assembles TB-2 scaffold + TB-3 parse + TB-4 sources + TB-5 passive + TB-6 probe on top of each other). Branch `tb-7-readme` is based on it.
2. Read the existing README.md in full before editing.
3. Update README to accurately reflect shipped code:
   - Agents/targets covered: Claude Code raw transcripts + AgentsView cross-agent source.
   - Run commands: `uv run python -m toolbench.passive …`, `uv run python -m toolbench.probe …`, `uv run python -m unittest discover tests`.
   - `--index-source` policy: auto / agentsview / raw.
   - Metrics: context-cost (joined result tokens, chars/4) as primary ranking metric; cache is caveat-only, not part of ranking; inefficiency callouts.
   - Do not invent features beyond what TB-2–TB-6 actually built.
4. Run the strict gate over the full assembled tree and capture output:
   - `uv run ruff check .`
   - `uv run mypy --strict toolbench tests`
   - `uv run python -m unittest discover tests`
   All three must be green (ruff clean, mypy --strict clean across 10 files, unittest ~93 tests OK per Orchestrator's prior confirmation). Known nit: a probe test prints a comparison table to stdout — not a failure, optional minimal quieting only, no scope creep.
5. Own-reviewer pass: diff `origin/integration/full..HEAD`, verdict + findings, attach as `review`.
6. Attach gate outputs as `validation`.
7. Push branch, open PR base `main` head `tb-7-readme` via `/usr/local/bin/gh` (after `gh auth switch` to keyring account), body noting integration/full assembles all tickets — merge substrate/consumer PRs first, rebase onto main after.
8. Attach PR URL as reference, set status to `review`, post completion comment, STOP. Do not merge.

Risk: if gate is not green, that's a real finding — fix minimal cause if in-scope for docs/gate, else `needs_human` + comment (do not silently expand scope into module behavior changes).
