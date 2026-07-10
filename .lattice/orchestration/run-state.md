# Run State — tool-benchmarks Run 2 (TB-19 · TB-18 remainder · TB-20)

Run 1 (TB-2…TB-7, closed 2026-07-08) is archived under [`run-1/`](run-1/);
its CLOSEOUT and validation report live there. Its footgun catalog is carried
forward below — every entry re-verified still applicable on 2026-07-10.

## Configuration
- **Autonomy:** Moderate (operator-confirmed 2026-07-10, Phase 0 config dialogue)
- **N (max concurrent delegators):** 2
- **PR merge policy:** leave at terminal pre-merge status (`review`); no auto-merge
- **Git remote (verified):** `origin` → git@github.com:mrojas54/tool-benchmarks.git
- **Terminal pre-merge status:** `review` (classic preset; verified via `valid_transitions` — no `pr_open`/`in_validation` lanes)
- **Ticket fidelity:** verbose-by-comment — tickets pre-date this run; run-2 obligations attached as `agent:orchestrator-intake` comments on TB-18/TB-19/TB-20
- **Workflow modes:** TB-19 fast-track · TB-18 inline-full · TB-20 inline-full (all collapse to inline own-reviewer on this install — see footguns)
- **Delegator model:** Sonnet (per project CLAUDE.md default for review/explore tier; escalate only if a ticket's logic proves subtle)
- **Master Validator:** OFF (3-ticket wave; operator-confirmed)
- **Result Validator:** ON, model Sonnet
- **auto-close finished surfaces:** on
- **Test gate (pinned for every boot prompt):** `uv run ruff check .` · `uv run mypy --strict toolbench tests` (baseline 38 pre-existing errors — new errors only are failures) · `uv run pytest -q`. **Never `unittest discover`** — it silently skips 37 tests and executes module-level code (the defect TB-19 fixes).
- **Contract-gap policy (operator-confirmed):** TB-19 and TB-20 author their own SPEC/EVALUATION/BUILDPLAN rows (S31, S32) in their DOCS phases, mirroring TB-18's S29/S30 precedent.

## Workspace panes (c11 refs) — set at Phase 1 boot 2026-07-10 05:48
- workspace: **workspace:1** ("tool-benchmarks")
- main_view_area: **pane:2** (Orchestrator = surface:9)
- control_surface: **pane:1** (pre-existing: Lattice Dashboard browser surface:6 → port **8799**, daemon terminal surface:11; design-doc markdown surfaces 7/8)
- delegate_view_area: **pane:12** (recreated 06:13 — closing pane:11's last surface collapsed the pane; TB-20 = surface:34. Earlier: TB-19 = surface:27, TB-18 = surface:31, both closed at review)
- lattice_dashboard_port: **8799** (live daemon in-workspace, browser surface already bound; run-1 daemon on 49427 also alive but unused by any surface)

## Tickets in scope
| Ticket | Title | Status | Workflow mode | Branch base | Depends on |
|--------|-------|--------|---------------|-------------|------------|
| TB-19 | unittest discover silently skips 37 of 220 tests | backlog | fast-track | origin/main | — (dispatch FIRST) |
| TB-18 | hermes --format trace: usage provenance + probe refusal (Tasks 3–6 of the 7-task plan) | in_progress | inline-full | `chore/add-hermes-cli-export-plan` (existing branch, PR #20 OPEN — continue, do not rebranch) | — (plan: `docs/superpowers/plans/2026-07-09-tb-18-usage-provenance.md`) |
| TB-20 | hermes session-grain cache_read_tokens never consulted | dispatched 06:14 (press-ahead) | inline-full | `tb-20-cache-read` off origin/chore/add-hermes-cli-export-plan @ 5c74901; **anchor PR #20** ("merge that first; this rebases"); PR base = parent branch | TB-18 (hard link; parent at review) |

Dispatch order: TB-19 and TB-18 concurrently (N=2; no code dependency —
TB-18's plan already pins pytest). TB-20 spawns when TB-18 reaches `review`
(press-ahead), rebased appropriately. SHARED-FILE flag: TB-19 and TB-18 Task 6
both touch README + EVALUATION; both PR bodies must name the overlap.

## Out of scope (recorded)
- TB-12 (CodexParser) — stays backlog; still unclaimed by any BUILDPLAN row.
- `lattice show TB-15` CLI crash (`_read_artifact_info` AttributeError on a
  string evidence ref) — upstream lattice-tracker bug, does not block status
  mutations; report upstream, not this run's work.

## Decision log (append-only)
- 2026-07-10 [autonomy: Moderate] Run-2 scope = TB-19 + TB-18 (Tasks 3–6) + TB-20 (operator-confirmed via Phase 0 config dialogue).
- 2026-07-10 [autonomy: Moderate] Board synced to verified git state (operator-confirmed): TB-13, TB-16 → done (PRs #19/#16 MERGED); TB-15 → done via review (PR #15 MERGED; `lattice show` crashes on it but status mutations work); TB-17 → done via planned→review (PR #18 MERGED, backlog status was stale).
- 2026-07-10 [autonomy: Moderate] Contract gap TB-19/TB-20 (no SPEC/EVALUATION rows) closed by policy: each ticket's DOCS phase authors S31/S32 (operator-confirmed; TB-18's S29/S30 is the precedent). Validation plan rows 10–13 audit the authored rows themselves.
- 2026-07-10 [autonomy: Moderate] Defaults accepted (operator-confirmed): N=2, Master Validator off, Result Validator on (Sonnet), merge policy leave-at-review, delegators Sonnet.
- 2026-07-10 [autonomy: Moderate] TB-20 hard-linked depends_on TB-18: both rework passive.py's cache render; serialization prevents a four-case-render race. Routine call, logged.
- 2026-07-10 [autonomy: Moderate] TB-19 scheduled first but NOT hard-linked as a dependency of the others: the gate defect is documentation-level; TB-18's plan already pins `uv run pytest -q`. Loose links kill parallelism.
- 2026-07-10 [autonomy: Moderate] Run-1 artifacts archived to `run-1/` via `git mv`; fixed paths (`validation-plan.md`, future `validation-report.md`) freed for run 2.
- 2026-07-10 [autonomy: Moderate] Run-1 dashboard (port 49427) verified alive and reused; no new daemon.
- 2026-07-10 [autonomy: Moderate] Workspace geometry deferred to Phase 1 boot; session-budget guard (4.4M tokens) makes a fresh session for dispatch the honest recommendation. Run-1 pane refs marked stale.
- 2026-07-10 [autonomy: Moderate] **TB-18 delegator runs in the ROOT checkout**, not a worktree: `.lattice/` is tracked and the entire run-2 board state exists only on `chore/add-hermes-cli-export-plan`, which is checked out at root — a second worktree on a checked-out branch is impossible, and switching root to main would rewind the board (TB-19/TB-20 would vanish from the working tree). Precedent: TB-18 Tasks 0–2 were committed in root. Mitigations baked into its boot prompt: no rebase/reset/clean/checkout, explicit-path commits only, nothing under `.lattice/` committed by the delegator.
- 2026-07-10 [autonomy: Moderate] Board-state git commits are **orchestrator-owned** this run (delegators leave `.lattice/` dirty); orchestrator commits board sync at closeout. Prevents interleaving with the orchestrator's uncommitted bookkeeping edits.
- 2026-07-10 [autonomy: Moderate] Inline-full's headless plan/code-review steps collapse to the own-reviewer fallback executed synchronously by the delegator (install has no `plan-review`/`code-review`); both delegators therefore run their arcs synchronously, no delegator-side /loop. Follows the Phase 0 "all collapse to inline own-reviewer" pin.
- 2026-07-10 [autonomy: Moderate] **Dispatch complete 06:42.** TB-19 → review (PR #21, off main), TB-18 → review (PR #20, MERGEABLE), TB-20 → review (PR #22, stacked on #20). All three arcs verified against git/PR ground truth, all delegator surfaces closed, board-sync commit by orchestrator follows. Merge order for operator: #20 → retarget #22 to main + rebase → #22; #21 independent (union-merge overlap with both at README/EVALUATION tails). Phase 2 Result Validator (fresh session, Sonnet) spawned next.
- 2026-07-10 [autonomy: Moderate] TB-18 spawn took 4 attempts (see footguns: pane-cwd inheritance + send/zsh-init race + `new-surface` silently ignoring an unsupported `--cwd` flag). Final recipe: create surface → exit any auto-launched claude → stateful `cd` at the live zsh → verify prompt path → launch. surfaces 28/29/30 were killed clean; no board or git damage.

## Run-time footguns (carried from run 1, re-verified 2026-07-10 + run-2 additions)
- **Status vocab.** Terminal pre-merge status is `review`, NOT `pr_open`. Ladder: `backlog → in_planning → planned → in_progress → review → done`. NO `in_validation`, NO `pr_open` lane.
- **Missing subcommands (v0.2.0 classic, NOT Stage11).** `lattice claim`, `plan-review`, `code-review`, `needs-human` DO NOT EXIST. Substitutions: claim → skip (use `--actor` on mutations); plan/code-review → own-reviewer fallback (diff + Verdict note via `lattice comment`); needs-human → `lattice status <ID> needs_human`.
- **Lattice CLI arg shapes.** `lattice comment <ID> "<text>" --actor …` (text positional, no `-m`). zsh does not word-split unquoted vars — never pass flags via `$VAR` expansion.
- **`lattice show TB-15` crashes** (upstream bug, string evidence ref). Status/comment mutations unaffected. Avoid `show` on TB-15.
- **`gh` is aliased to `op plugin run -- gh`** — fails headless. Use `/usr/local/bin/gh`, including inside `$(...)`. Token env var is `GH_TOKEN`: `GH_TOKEN=$(/usr/local/bin/gh auth token) /usr/local/bin/gh …`.
- **PR-create auth:** `GITHUB_PERSONAL_ACCESS_TOKEN` lacks PR-create scope; use the keyring account (`/usr/local/bin/gh auth switch`, verify `auth status`).
- **`gh pr edit --body` silently fails** (deprecated Projects GraphQL path, exit 0, body unchanged). Use `gh api -X PATCH repos/<o>/<r>/pulls/<N> --input body.json`; verify with `gh pr view <N> --json body`.
- **Test gate:** `uv run pytest -q` ONLY. `unittest discover` under-collects by 37 tests and executes module-level code (prints a probe table mid-run). This is the TB-19 defect — until its PR merges, the *documented* command lies.
- **mypy --strict baseline is 38 pre-existing errors.** A delegator must diff error count against baseline, not demand zero.
- **Hermes DBs are NEVER opened writable.** `mode=ro`; `immutable=1` only when no `-wal` sidecar exists (post-Task-0 `_connect`). `immutable=1` on a WAL DB reads stale data and `PRAGMA journal_mode` lies under it.
- **Press-ahead branch bases:** TB-18 continues `chore/add-hermes-cli-export-plan` (PR #20) — new delegator must NOT rebranch. TB-20 bases off main post-merge or rebases onto #20's branch.
- **`c11 new-surface` silently ignores unknown flags** (`--cwd` printed OK but did nothing — the flag does not exist). Never trust an OK for a flag not in `--help`.
- **PTY allocator wedge hit at end of run (06:44–06:51):** three consecutive fresh surfaces (36/37/38, panes 2 and 1) never presented a shell prompt; existing surfaces stayed fully functional. Earlier same-run precursors: close-surface timeout, identify timeout. Mitigation: stop spawning, escalate to operator (restart c11.app), run the pending seat from a manually opened tab if needed.
- **`c11 identify` can time out inside a fresh delegator shell** (same daemon slowness family as the close-surface timeout). The identity block's fatal-halt fires; recovery = orchestrator sets the tab identity itself and sends "skip the identity block, proceed" — identity is telemetry, not load-bearing.
- **Closing a pane's last surface collapses the pane** — `new-surface --pane <old-ref>` then fails "Pane not found". Re-split and re-title instead of retrying.
- **`claude "<prompt arg>"` launched via `c11 send` often strands the argument** (TUI comes up idle at the placeholder). Don't fight it: once the TUI is idle, `c11 send` the prompt text as the first message + `send-key enter`.
- **`c11 close-surface` can time out (10s) yet still complete** — the claude child reap is slow. Verify with `c11 tree` before retrying; a blind retry errors on the missing surface.
- **New-surface spawn cwd = pane's last shell cwd, and `send` races zsh init.** Text sent before the first prompt gets buffered and replays unpredictably (may execute pre-prompt text, may strand the claude arg). Recipe that works: create surface → wait → read-screen until a real zsh prompt shows → send standalone `cd <target>` + enter → read-screen to verify the prompt path changed → then send the claude launch line. The `cd <wt> && claude ...` single-line form also works but ONLY after the prompt is confirmed up.
