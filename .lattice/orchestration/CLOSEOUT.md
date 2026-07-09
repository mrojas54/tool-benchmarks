# Run Closeout — tool-benchmarks (2026-07-08)

## Outcome
- **6/6 tickets built and landed at `review`** (TB-2…TB-7), 6 open PRs (#1–#6).
- Assembled `integration/full` strict gate GREEN: ruff clean, mypy --strict clean
  (10 files), 93 unittest OK.
- Phase-2 audit: **24/24 pre-merge-static rows PASS** (see validation-report.md),
  run in degraded mode (Orchestrator-as-validator; see caveat in that report).
- Merged (PRs #1–#7). **Operator smoke checklist run 2026-07-08: 3 PASS, 1 PARTIAL,
  1 bug found (TB-8, fixed).** See "Operator smoke results" below.

## Operator smoke results (2026-07-08)

Gate re-verified on merged `main` first: ruff clean, mypy --strict clean (10 files),
99 unittest OK.

| Row | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Join-key on real data (S1/S2) | **PASS** | 301 calls joined, 470k output tokens, 0 malformed. Real transcripts hold 302 block-local `tool_use_id` results and **zero** top-level `toolUseID` — the block-local branch is the only path that ever fires. Reversed precedence would have zeroed every join. |
| 2 | AgentsView live path (S10/S25) | **PASS** | Healthy daemon → `auto` uses AgentsView (`fallback reason: none`). Binary hidden from `PATH` → falls back to raw, names the reason. `--index-source agentsview` → fatal, exit 1. |
| 3 | Scale / flat memory (S11) | **PASS** | Pushed past the 200-session bar to the full corpus: 3,705 sessions / 417 MB / 5.2 s, peak RSS **44.8 MB** vs 35.9 MB at 20 sessions (+25% for 185× sessions). Joined 6,683 of 6,684 ground-truth `tool_use` blocks; the 1 gap is a call appended to the live transcript mid-read. |
| 4 | Report reads well (`felt`) | **PARTIAL** | Four sections in spec order, scannable. Callouts are bare counts with no denominators or attribution — `Failures: 865` names no tool, `Churn: 222` names no site. Signal, not action. Operator judgment; left open. |

### Bug found: TB-8 — `--project` silently dropped every subagent session

`sources.py` globbed recursively but filtered on `path.parent.name`. Subagent
transcripts live at `<project>/subagents/*.jsonl`, so `parent.name == "subagents"`
never contained the project substring: **every subagent session was dropped whenever
`--project` was passed**, `--exclude-subagents` was a no-op (nothing left to filter),
and the report still printed `Subagents included: yes` (false provenance).
`--all` was unaffected — `project is None` short-circuits the branch — which is why
the full-corpus run looked correct and only the per-project run exposed it.

Measured on `-Users-…-wids-nyc-reading-group-assistant`: `iter_session_files(project=P)`
→ 197 files / 0 subagent; unfiltered ∩ P → 249 files / 52 subagent. Post-fix the two
sets are identical, joining 2,689 calls — matching an independent `tool_use` block count.

Violated **S13** and **S15**. Fixed in `tb-8-subagent-project-filter` (RED → GREEN,
101 tests).

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

5. **A criterion "verified" only against hand-built fixtures is not verified.**
   Validation-plan rows 15 (S13) and 17 (S15) both passed the static audit while the
   shipped code violated both (TB-8). The tests constructed `SessionRef` objects
   directly and never called `iter_session_files`, so the discovery layer and the
   subagent filter were each correct in isolation and wrong in composition. A fixture
   proves your test is self-consistent, not that real data has that shape.
   → When a criterion asserts behavior over a real directory layout or payload shape,
   its verification row must exercise the real seam end-to-end, or be explicitly
   tagged `post-merge-smoke` and **not counted as a pass**. Two rows here were counted
   as passes. Corollary: "leave-at-review" merged one row too early — the smoke rows
   were the only ones that could have caught this, and they ran after the merge.

## Config decisions (see run-state.md decision log)
- Delegators Sonnet; Result Validator downgraded Opus→Sonnet at 76% weekly usage.
- Leave-at-review (no auto-merge). One delegate pane (well under surface cap).
