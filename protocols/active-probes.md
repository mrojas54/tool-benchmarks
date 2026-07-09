# Active probes (S16, S17)

Five files vendored under `tools/` (committed, relative paths — reproducible
from a clean checkout, no external absolute paths). Each is the target of
exactly one probe: a matched pair of arms — one tool call, one `Bash`
call — over the same file, distinguished by globally-unique, non-substring
sentinels (`TB_PROBE_<id>_TOOL_V2` / `TB_PROBE_<id>_BASH_V2`).

Probe *output* — the comparison table and token measurements — is written
under `reports/` at run time. It is never mixed into this list or into
`tools/`: this file only names probe *inputs*.

| id | corpus path | lines | task | tool arm | tool sentinel | bash sentinel |
|----|-------------|-------|------|----------|----------------|----------------|
| 01 | `tools/regex_check.py` | ~121 | find | `mcp__serena__find_file` | `TB_PROBE_01_TOOL_V2` | `TB_PROBE_01_BASH_V2` |
| 02 | `tools/mcp.py` | ~352 | search | `mcp__serena__search_for_pattern` | `TB_PROBE_02_TOOL_V2` | `TB_PROBE_02_BASH_V2` |
| 03 | `tools/monitor.py` | ~768 | find | `mcp__serena__find_file` | `TB_PROBE_03_TOOL_V2` | `TB_PROBE_03_BASH_V2` |
| 04 | `tools/llm_extraction.py` | ~1,332 | search | `mcp__serena__search_for_pattern` | `TB_PROBE_04_TOOL_V2` | `TB_PROBE_04_BASH_V2` |
| 05 | `tools/code_analysis.py` | ~2,242 | find | `mcp__serena__find_file` | `TB_PROBE_05_TOOL_V2` | `TB_PROBE_05_BASH_V2` |

Task alternates by file so both seeded task types (`search`, `find`) are
exercised across the log-spaced size spread. The bash arm for every probe
uses plain `Bash` (`grep`/`find` over the same corpus file).

`toolbench.probe.find_probe_calls` verifies an arm only when a call carries
that arm's sentinel **and** used that arm's expected tool name — sentinel
alone or tool name alone is not a match. When an arm has no matching call in
the scored session, `toolbench.probe.build_comparison_table` falls back to
the seeded `#8376` baseline for that `(task, arm)` pair (S18):

| task | serena (tool arm) | bash arm |
|------|--------------------|----------|
| search | 723 | 794 |
| find | 68 | 89 |
