# Run State — tool-benchmarks Phase 0/1

## Configuration
- **Autonomy:** Moderate (confirmed 2026-07-08)
- **N (max concurrent delegators):** 3
- **PR merge policy:** leave at terminal pre-merge status (confirmed — no auto-merge)
- **Git remote (verified):** `origin` → git@github.com:mrojas54/tool-benchmarks.git
- **Terminal pre-merge status:** `review` (classic preset; transition `review → done`. No `pr_open`/`in_validation` lane — delegators land tickets at `review`, never `pr_open`)
- **Ticket fidelity:** verbose (full description + SPEC IDs + BUILDPLAN anchor + depends-on)
- **plan_review_mode / review_mode:** headless code-review/plan-review (inline-full default)
- **Master Validator:** on (6 tickets)
- **Result Validator:** on — **model Sonnet** (downgraded from Opus at tick 4 for weekly-usage-ceiling conservation; operator-confirmed. The 27 pre-merge-static rows are mechanical diff-checks, within Sonnet's competence; terminal-audit independence preserved.)
- **auto-close finished surfaces:** on
- **c11 workspace ref:** see Workspace panes below

## Workspace panes (c11 refs)
- workspace: workspace:1 "tool-benchmarks"
- main_view_area: pane:1 / surface:5 "Orchestrator" (Master Validator + Result Validator land here as tabs)
- control_surface: pane:2 / surface:6 "Control Surface" (terminal, protected) + surface:11 "Lattice Board" (browser → dashboard)
- delegate_view_area_1: pane:5 / surface:10 "Delegate View" (delegators land here as tabs; soft cap 15, ≤6 total this run so one pane suffices — routine call, logged)
- lattice_dashboard_port: 49427 (nohup lattice dashboard; log /tmp/lattice-dashboard-49427.log)

## Tickets in scope
| Ticket | Title | Status | Workflow mode | Branch base | Depends on |
|--------|-------|--------|---------------|-------------|------------|
| TB-2 | Scaffold + ToolCall + result_len | backlog | inline-full | main | — |
| TB-3 | parse_session id-join | backlog | inline-full | main | TB-2 |
| TB-4 | sources.py multi-agent discovery | backlog | inline-full | main | TB-2 |
| TB-5 | passive.py reducer + report + CLI | backlog | inline-full | main | TB-3, TB-4 |
| TB-6 | probe.py + active-probes.md | backlog | inline-full | main | TB-3 |
| TB-7 | README + strict gate | backlog | fast-track | main | TB-5, TB-6 |

Dispatch order: TB-2 first (unblocked). On TB-2 reaching `review` (press-ahead),
spawn TB-3 + TB-4 (both depend only on TB-2). TB-6 unblocks with TB-3. TB-5
needs TB-3 + TB-4. TB-7 needs TB-5 + TB-6 — last, fast-track (README + run gate).

## Decision log (append-only)
- 2026-07-08 [autonomy: Moderate] Merge policy = leave-at-review; autonomy = Moderate (operator-confirmed via Phase 0 config dialogue).
- 2026-07-08 [autonomy: Moderate] Workflow modes: TB-2..TB-6 inline-full (correctness-sensitive parsing/discovery/reporting), TB-7 fast-track (README + mechanical gate run). Routine call, logged.
- 2026-07-08 [autonomy: Moderate] S16 tagged `pre-merge-static` (not `operator-assisted` as in EVALUATION): corpus is vendored + committed under `tools/`, so `active-probes.md` path-list is diff-checkable against the repo tree from a clean checkout — the G1 resolution's whole point. More checkable than EVALUATION assumed pre-vendoring.
- 2026-07-08 [autonomy: Moderate] Contract checks passed with no gaps; G1/G2/G3 already RESOLVED in BUILDPLAN. No upstream routing needed.
- 2026-07-08 [autonomy: Moderate] Geometry: one delegate pane (not the prescribed 3) — 6 delegators total, N=3 concurrent, well under the 15-surface soft cap; delegators route as tabs into pane:5. More legible in the 620px right column.
- 2026-07-08 [autonomy: Moderate] Install is Lattice v0.2.0 `classic`, not Stage11. No `claim`/`plan-review`/`code-review`/`needs-human` subcommands, no `in_validation`/`pr_open` lanes. All tickets run inline own-reviewer (fresh-eyes review CLI unavailable); reviews attached as notes. Recorded in footguns; baked into boot prompts.
- 2026-07-08 [autonomy: Moderate] Delegator model = Sonnet (mechanical/well-specified TDD tickets; cost discipline per operator signals). Final Result Validator = Opus. Escalate a delegator to Opus only if a ticket's logic proves subtle (e.g. TB-3 join-key).
- 2026-07-08 [autonomy: Moderate] Result Validator downgraded Opus→Sonnet (operator-confirmed at tick 4). Reason: weekly usage at 76%; Opus validator was the priciest discretionary line and the static audit rows are mechanical. Delegators already Sonnet. Loop instructed to halt-and-surface (not thrash) if any agent hits the ceiling.
- 2026-07-08 [autonomy: Moderate] TB-5 base = `integration/substrate` (merge of origin/tb-3-parse + origin/tb-4-sources), since TB-5 (passive) needs BOTH transcript-parser (TB-3) and sources (TB-4) and no single parent has both. Merge was clean (no conflicts); assembled-tree suite GREEN (40 tests). Pushed to origin/integration/substrate. TB-5 PR body must note it assembles #2 (TB-4) + #3 (TB-3); after those merge, TB-5 rebases onto main. TB-6 (probe) bases off origin/tb-3-parse (needs parser only).

## Run-time footguns
(rows added during dispatch)
- **Status vocab.** Terminal pre-merge status is `review`, NOT `pr_open`. Ladder: `backlog → in_planning → planned → in_progress → review → done`. NO `in_validation`, NO `pr_open` lane. A delegator bumping to either hits Invalid transition. Validation evidence is attached as a note while `in_progress`/at `review`.
- **Missing subcommands (v0.2.0 classic, NOT Stage11).** `lattice claim`, `plan-review`, `code-review`, `needs-human` DO NOT EXIST here. Substitutions baked into every boot prompt:
  - claim → skip (no auto-rename); delegator sets its own title; use `--actor` on mutations. (`lattice assign <ID> <actor>` exists if assignment is wanted.)
  - plan-review / code-review → **own-reviewer fallback always** (compute diff with `git log origin/main..HEAD` + per-file diffs, write a Verdict/findings review, `lattice attach <ID> --role review --inline "..."`). No headless review CLI in this install.
  - needs-human → `lattice status <ID> needs_human`.
- Consequence: inline-full and fast-track collapse to the same shape here (inline own-reviewer) — there is no headless fresh-eyes review CLI. Cross-cutting fresh eyes come from the Master Validator (in-flight) and the Result Validator (terminal).
- **`gh` is aliased to `op plugin run -- gh`** (1Password) which FAILS headless ("interactive IO not available"). Mitigation baked into boot prompts: call the real binary `/usr/local/bin/gh` directly. (Found by TB-2 delegator, tick 2.)
- **PR-create auth:** `GITHUB_PERSONAL_ACCESS_TOKEN` env var lacks PR-create scope; TB-2 had to `gh auth switch` to the keyring account (has repo scope) before `gh pr create` succeeded. Mitigation baked in: `/usr/local/bin/gh auth switch` to the keyring/repo-scoped account before `gh pr create`; verify with `/usr/local/bin/gh auth status`.
- **Press-ahead branch base:** TB-3/TB-4 branch off `origin/tb-2-scaffold` (in-review parent, PR #1), NOT main — they build on `toolbench/transcript.py`. Their PRs name "based on #1 — merge that first; this rebases". `toolbench/__init__.py` is a potential shared-file edit (re-exports) — additive union at merge; flagged in both prompts.
- **Phase 2 validator spawn BLOCKED (tick 6).** Two fresh terminal surfaces (surface:20 pane:1, surface:21 pane:5) would not initialize their PTY ("Surface not ready" on send-key, "Terminal surface not found" on read-screen), despite being the selected/active tab. Existing surfaces (surface:10) read fine → PTY subsystem not globally wedged, but NEW-surface init is failing. Concurrent symptom: a blocking 1Password "Locate your GitHub Personal Access Token" interactive prompt sitting in surface:10 (the op/gh integration). Compounded by usage ceiling (76% weekly) + session-budget guard (~1.2M tokens). Decision: halt-and-surface to operator rather than thrash. Options offered: (a) Orchestrator runs the audit in degraded mode (loses cold independence; plan is mechanical + gate already verified green), (b) retry fresh validator later, (c) operator runs validation post-reset. Loop stopped pending operator choice.
