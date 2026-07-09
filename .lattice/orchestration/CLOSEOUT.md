# Run Closeout — tool-benchmarks (2026-07-08)

## Outcome
- **6/6 tickets built and landed at `review`** (TB-2…TB-7), 6 open PRs (#1–#6).
- Assembled `integration/full` strict gate GREEN: ruff clean, mypy --strict clean
  (10 files), 93 unittest OK.
- Phase-2 audit: **24/24 pre-merge-static rows PASS** (see validation-report.md),
  run in degraded mode (Orchestrator-as-validator; see caveat in that report).
- Merge + operator smoke checklist pending (leave-at-review policy).

## Timeless findings (failure mode → why it matters → fix)

1. **Probe Lattice subcommand availability during Phase 0, not just status vocab.**
   The orchestrator playbook's boot templates assume a Stage11 preset
   (`claim`, `plan-review`, `code-review`, `needs-human`, `in_validation`/`pr_open`).
   This install was v0.2.0 **classic** and had none of them. Had a delegator called
   `lattice claim`/`plan-review` it would have errored mid-run. Fix already applied:
   Phase 0 now probes each assumed subcommand (`lattice <cmd> --help`) and bakes
   substitutions (own-reviewer fallback, `status … needs_human`) into boot prompts.
   → Candidate promotion to `references/intake.md` install-facts checklist.

2. **`gh` aliased to `op plugin run -- gh` (1Password) breaks headless agents.**
   Fails with "interactive IO not available" and pops a blocking 1Password overlay
   that can also wedge fresh c11 surface init. Plus the default
   `GITHUB_PERSONAL_ACCESS_TOKEN` lacked PR-create scope. Fix baked into prompts:
   use `/usr/local/bin/gh` + `gh auth switch` to the keyring (repo-scoped) account;
   Escape to dismiss the overlay. → Machine-specific; belongs in personal/global
   env notes, not the skill.

3. **c11 new-surface PTY init can fail late in a long run.** Fresh terminal
   surfaces (background tabs especially) failed to initialize their PTY
   ("Surface not ready" / "Terminal surface not found") while existing surfaces
   read fine — a partial degradation, aggravated by the stuck 1Password overlay.
   Blocked the Phase-2 fresh-validator spawn. Mitigation used: halt-and-surface,
   then Orchestrator-as-validator (degraded mode). → c11/orchestrator footgun;
   recovery = degraded-mode audit or retry after clearing the overlay.

4. **Two-parent ticket → integration branch pattern (positive).** TB-5 needed both
   TB-3 (parser) and TB-4 (sources); no single parent branch had both. Cutting
   `integration/substrate = merge(tb-3-parse, tb-4-sources)`, validating the
   assembled tree green (40 tests), then basing TB-5 on it worked cleanly — same
   for `integration/full` before TB-7. Confirms the skill's integration-branch
   guidance for stacked/multi-parent work.

## Config decisions (see run-state.md decision log)
- Delegators Sonnet; Result Validator downgraded Opus→Sonnet at 76% weekly usage.
- Leave-at-review (no auto-merge). One delegate pane (well under surface cap).
