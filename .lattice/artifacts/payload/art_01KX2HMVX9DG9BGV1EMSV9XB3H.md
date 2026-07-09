Both in-scope items done.

1) Read path: toolbench/hermes.py reads hermes sessions directly from ~/.hermes (read-only, mode=ro), resolving each session across all three profile DBs and joining tool_calls[].id -> messages.tool_call_id. passive._parse_ref routes agent=='hermes' there. Live archive: 29 sessions, 176 tool calls, 0 dangling, 0 malformed -- corroborated by hermes' own sessions.tool_call_count on all 29. Recovers the corpus's only MCP-tool data (16 mcp_dash0_* calls). Merged as PR #13 (merge commit 0ae4f7d). 145 tests, ruff + mypy --strict clean.

2) Upstream reports: kenn-io/agentsview#1047 (session export returns the whole SQLite archive, rc=0) and #1048 (session list returns 89 hermes sessions where stats reports 789). Bodies archived in docs/upstream/.

Sharpened the ticket's diagnosis: the export streams the DEFAULT profile's state.db, so for the 2 sessions in profiles/aphrodite-mood it returns rc=0 plus a database with zero rows for the requested session. A fixed export would still not reach them.

Falsified the ticket's implicit premise, and my own first rationale: AgentsView's 89-of-814 is not a curated view. Its stats subsystem sees 789 on the same archive, so session list is losing sessions. Corrected in CLOSEOUT finding 8b, README, SPEC S9b, hermes.py docstring, and the PR body.

Design decision unchanged: discovery stays with AgentsView, because the corpus is DEFINED as what session list returns and every agent is sampled through that path. Hermes remains under-sampled here -- a named limitation blocked on #1048, not a hidden one.

NOT done, deliberately: hermes discovery from the archive (rejected -- would take hermes from 0% to ~29% of corpus tool calls and skew every cross-agent rate); the claude-ai rc=1 empty export (out of scope); removing the NUL sniff (must stay).