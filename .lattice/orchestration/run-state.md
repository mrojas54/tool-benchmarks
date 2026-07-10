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

## Workspace panes (c11 refs)
- lattice_dashboard_port: **49427** (run-1 daemon still alive, PID verified 2026-07-10; reused)
- Pane geometry: **deferred to Phase 1 boot** — run-1 pane/surface refs are stale (dead sessions), and the operator is advised to start Phase 1 in a fresh session (session-budget guard: 4.4M tokens at Phase 0 close). Phase 1 re-runs the geometry step from `references/intake.md` and records refs here.

## Tickets in scope
| Ticket | Title | Status | Workflow mode | Branch base | Depends on |
|--------|-------|--------|---------------|-------------|------------|
| TB-19 | unittest discover silently skips 37 of 220 tests | backlog | fast-track | origin/main | — (dispatch FIRST) |
| TB-18 | hermes --format trace: usage provenance + probe refusal (Tasks 3–6 of the 7-task plan) | in_progress | inline-full | `chore/add-hermes-cli-export-plan` (existing branch, PR #20 OPEN — continue, do not rebranch) | — (plan: `docs/superpowers/plans/2026-07-09-tb-18-usage-provenance.md`) |
| TB-20 | hermes session-grain cache_read_tokens never consulted | backlog | inline-full | origin/main after PR #20 merges (else rebase onto #20's branch) | TB-18 (hard link) |

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
