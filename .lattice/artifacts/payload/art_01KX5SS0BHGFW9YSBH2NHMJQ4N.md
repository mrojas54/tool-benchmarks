VALIDATION (TB-20)

Full gate: ruff clean; mypy --strict toolbench tests holds at the
38-error pre-existing baseline (0 new errors); uv run python -m
unittest discover tests -> 218 passed, 1 skipped (up from 205/1 at
Task-1 start; +13 new tests across the three TDD steps), 0 failures.

End-to-end evidence against the live hermes archive (read-only,
mode=ro/immutable=1 per _connect's contract -- never opened writable),
via `uv run python -m toolbench.passive --agent hermes --all
--index-source agentsview --exclude-subagents`:

BEFORE (parent branch, origin/chore/add-hermes-cli-export-plan @
5c74901, run in a disposable temporary worktree, removed after):
  | hermes | 94 sessions | 1455 calls | ... |
  (Agent Breakdown table only -- no session-grain signal anywhere in
  the report. Tool Leaderboard cache_assisted column: n/a for every
  hermes tool, with nothing distinguishing "no cache data exists" from
  "cache data exists but isn't at this grain.")

AFTER (this branch, HEAD @ 8cb7dd3):
  | hermes | 94 sessions | 1455 calls | ... |
  - hermes: 91 of 94 sessions carry session-grain `cache_read_tokens` > 0
    (S32: session grain only -- not attributable to individual tool calls).
  (Tool Leaderboard cache_assisted column: still n/a for every hermes
  tool -- unchanged from TB-18, and correctly so: no individual call
  became measurable. Session/call counts identical before/after, since
  discovery and per-call joining are untouched; only the session-grain
  signal is new.)

This satisfies the acceptance text directly: the report now states that
session-grain cache data exists (91/94, ~97% today vs. the ticket's
~90%/776-863 snapshot -- archive grew between filing and this run) and
is deliberately not attributed per call, while nothing anywhere implies
hermes achieved a 0% cache-hit rate.

Also re-ran the fixture-schema envelope test against the real archive
with TOOLBENCH_LIVE=1: cache_read_tokens confirmed present as a real
column on all 4 live profile DBs (.hermes 803 sessions, aphrodite-mood
34, light-mood 28, tech-interviewing 8), zero NULLs observed.