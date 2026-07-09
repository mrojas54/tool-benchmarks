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
| 01 | `tools/regex_check.py` | ~121 | find | `mcp__plugin_serena_serena__find_file` | `TB_PROBE_01_TOOL_V2` | `TB_PROBE_01_BASH_V2` |
| 02 | `tools/mcp.py` | ~352 | search | `mcp__plugin_serena_serena__search_for_pattern` | `TB_PROBE_02_TOOL_V2` | `TB_PROBE_02_BASH_V2` |
| 03 | `tools/monitor.py` | ~768 | find | `mcp__plugin_serena_serena__find_file` | `TB_PROBE_03_TOOL_V2` | `TB_PROBE_03_BASH_V2` |
| 04 | `tools/llm_extraction.py` | ~1,332 | search | `mcp__plugin_serena_serena__search_for_pattern` | `TB_PROBE_04_TOOL_V2` | `TB_PROBE_04_BASH_V2` |
| 05 | `tools/code_analysis.py` | ~2,242 | find | `mcp__plugin_serena_serena__find_file` | `TB_PROBE_05_TOOL_V2` | `TB_PROBE_05_BASH_V2` |

Task alternates by file so both seeded task types (`search`, `find`) are
exercised across the log-spaced size spread. The bash arm for every probe
uses plain `Bash` (`grep`/`find` over the same corpus file).

Only the **bash** sentinel is required to appear in a call. The tool sentinel
is retained as a rejection token (see S20): a tool arm may name its own
sentinel, but must never name another probe's.

## Tool naming (S19)

Claude Code namespaces an MCP tool by how its server was installed. Serena
reaches this machine as the `serena` plugin's `serena` server, so calls are
recorded as `mcp__plugin_serena_serena__find_file`. A bare (non-plugin)
install would record `mcp__serena__find_file`. `ProbeSpec.tool_names` accepts
both; the plugin form is primary and is what the table reports. Matching is
exact — never a substring or suffix test.

An earlier revision of this protocol named only `mcp__serena__find_file`,
which appears **zero** times as a `tool_use` name anywhere in the transcript
corpus. The serena arm was therefore unmatchable and every serena cell in
every report was seeded.

## How each arm is identified (S20)

The two arms carry different evidence, so they are matched differently.

**The tool arm is matched structurally**, on the accepted tool name plus the
corpus *target* (the basename, e.g. `regex_check.py`) appearing in the call's
input. It carries no sentinel. It cannot: serena's `find_file` accepts exactly

    {file_mask: str, relative_path: str}

`file_mask` must equal the file being searched for or the search misses, and
`relative_path` must be a real directory. There is no inert free-text field to
park a correlation id in. Nor is one needed — a `find_file` for
`regex_check.py` is already unambiguously probe 01's tool arm.

**The bash arm is matched by sentinel**, because a shell command is
unstructured text in which nothing else is reliably distinctive. A `#` comment
carries `TB_PROBE_01_BASH_V2` without changing what the command does.

An earlier revision required a sentinel in *both* arms. Since `find_file` has
nowhere to put one, all three `find` arms (01, 03, 05) were unperformable, and
`search_for_pattern` (02, 04) could only comply by bolting a never-matching
alternation onto the regex being measured — which is not the task.

The fixtures concealed this by inventing parameters serena does not accept
(`name_path`, `comment`). `ToolArmSchemaTests` now fails if any fixture uses a
parameter outside serena's real schema.

## Performing vs. mentioning a probe (S19)

A sentinel is a bare string, so a call that *greps for* `TB_PROBE_01_BASH_V2`
looks exactly like the call that *performs* probe 01's bash arm.
`toolbench.probe.find_probe_calls` therefore discards any call that:

- names more than one probe sentinel (a real bash arm names exactly its own), or
- targets the transcript corpus or the probe's own source — see
  `MENTION_MARKERS` (`.claude/projects`, `toolbench/probe.py`, this file).

A tool-arm candidate carrying some *other* probe's sentinel is rejected on the
same grounds: a serena search of `tools/mcp.py` for `TB_PROBE_01_TOOL_V2` is a
hunt for a sentinel, not probe 02's tool arm.

This narrows contamination; it does not eliminate it. A single-sentinel grep
against the corpus file is indistinguishable from the bash arm it imitates.
**Run probes in a dedicated, fresh session and score only that session's
JSONL.** Do not score a session in which the probes were discussed, edited,
or searched for.

Each arm must be alone in its turn: `_usage_output_tokens` attributes real
usage only when the turn holds exactly one `tool_use` block. Ten arms means
ten turns. Do not batch them.

## Seeded baselines are not measurements

`toolbench.probe.build_comparison_table` falls back to the seeded `#8376`
baseline for any `(task, arm)` pair with no matching call (S18):

| task | serena (tool arm) | bash arm |
|------|--------------------|----------|
| search | 723 | 794 |
| find | 68 | 89 |

These four constants are inputs, not results. A table in which *every* arm is
seeded restates them and measures nothing, so `render_report` raises
`SeededReportError` rather than emit it; `--allow-seeded` overrides this for
deliberate inspection. Partially-seeded tables still render, with `*` marking
each seeded cell.
