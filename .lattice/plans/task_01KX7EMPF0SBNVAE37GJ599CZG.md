# TB-27: per-run cache-token grouping (S40)

`--run-manifest <run.json>` on passive — orchestrator-emitted `{run, tickets,
branches, worktrees?}` at dispatch — folds **entry-grain** usage whose
`gitBranch` is in the run's branch set into one reducer. Out-of-set usage
within candidate sessions lands in `unattributed` (reported). Emits cache
read+creation per run, normalized per ticket. Claude-only. Retires
`cache_tokens` once landed.

**Not** `agents.md` (discards branch columns on completion) and **not** a
session-set partition (sessions straddle branches). Design:
`docs/superpowers/specs/2026-07-12-tb-27-per-run-cache-grouping-design.md`.
Depends on TB-26 (S39).
