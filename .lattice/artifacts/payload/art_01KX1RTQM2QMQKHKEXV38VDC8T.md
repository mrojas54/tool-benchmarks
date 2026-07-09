Own-reviewer review (TB-7)

Diff vs origin/integration/full: single commit, README.md only (+30/-8 lines).

Verdict: APPROVE

Findings:
- Scope respected: docs-only change, no edits to transcript.py/sources.py/passive.py/probe.py behavior.
- Status section corrected from 'contract complete; implementation pending' to 'Implemented', matching that T1-T6 all shipped.
- Usage section updated from '(planned)' to real, verified CLI flags for both toolbench.passive and toolbench.probe (--agent, --all/--project, --since, --date-from/--date-to, --out, --limit, --exclude-subagents, --index-source, --verbose for passive; --session, --out for probe) -- cross-checked directly against argparse definitions in toolbench/passive.py and toolbench/probe.py, no invented flags.
- New 'Agents / targets' section documents the two source adapters (raw Claude Code transcripts, AgentsView cross-agent) accurately against sources.py.
- New '--index-source policy' subsection documents auto/agentsview/raw semantics, matching _discover_refs in passive.py (auto falls back and records reason; agentsview/raw are fatal-on-error).
- Metrics description (context-cost primary ranking metric, chars/4, cache caveat-only, inefficiency callouts) was already accurate pre-existing content -- left untouched, verified still correct against passive.py Reducer/render_report.
- No features invented beyond what TB-2-TB-6 built.