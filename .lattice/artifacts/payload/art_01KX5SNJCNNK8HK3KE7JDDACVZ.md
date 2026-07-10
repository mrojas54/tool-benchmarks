CODE SELF-REVIEW (TB-20) — Verdict: PASS

Diff base: origin/chore/add-hermes-cli-export-plan @ 5c74901. 8 files
changed, +286/-12 across 7 commits (RED/GREEN x3 + docs + a self-review
refactor), all with explicit `git add <file>` (no `-A`/`-a`).

Scope check against acceptance text: "either hermes cache data reaches
the report at a grain it actually has, or the report states that
session-grain cache data exists and is deliberately not attributed per
call" -- delivered via the Agent Breakdown caveat line
(toolbench/passive.py render_report). "Must not survive: a report
implying hermes achieved a 0% cache-hit rate" -- the Tool Leaderboard's
per-call cache_assisted column for hermes still renders `n/a` (never
`no`), which TB-18 already made correct; TB-20 does not change that,
and test_tool_leaderboard_cache_column_unaffected_by_session_grain_hit
pins it against a session carrying a real (999) session-grain figure.
"Do NOT divide by tool_call_count" -- no division against tool_call_count
or calls anywhere in the diff; the two new AgentStats counters are
incremented exactly once per absorbed ParseResult, outside the per-call
loop (test_counters_accumulate_across_sessions_one_increment_each_...
pins a 3-call session incrementing by 1, not 3).

Composition with S29/UsageProvenance: verified NOT forked --
UsageProvenance enum untouched, ClaudeParser/HermesTraceParser
untouched, four-case cache_note render logic in render_report
byte-for-byte unchanged from TB-18. session_cache_read_tokens flows on
an entirely separate axis (ParseResult -> AgentStats, never ToolCall).

Findings:
- Minor (FIXED before this review): render_report looped
  sorted(reducer.agents) twice (table rows, then caveat lines).
  Consolidated to one pass in 8cb7dd3; full suite re-verified green
  after the change.
- No Critical / Major findings.

Verified: mypy --strict holds at the pre-existing 38-error baseline
(zero new errors across all 7 commits); ruff clean throughout; every
ParseResult(...) construction site in toolbench/ and tests/ uses
keyword args, so the new session_cache_read_tokens default-None field
cannot silently shift a positional argument anywhere (grepped all call
sites). test_live_archive_schema_envelope re-run with TOOLBENCH_LIVE=1
against the real archive (read-only) confirms `cache_read_tokens`
actually exists as a column on all 4 live profile DBs, not just the
synthetic fixture schema.