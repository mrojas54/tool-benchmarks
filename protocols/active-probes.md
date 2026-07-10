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
is retained as a rejection token (see S17): a tool arm may name its own
sentinel, but must never name another probe's.

## Tool naming (S17)

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

## How each arm is identified (S17)

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

## What an arm's usage may include (S26)

Identifying an arm is not the same as pricing it. `output_tokens` is reported
per **API response**, and Claude Code writes one response as several JSONL
entries — a `thinking` block, a `text` block, and one entry per `tool_use` —
each stamped with the *same* `usage` figure and a *different* timestamp.

The turn is therefore the `requestId`, not the timestamp. An arm's usage is
attributable only when its response emitted that one `tool_use` block and
nothing else. Prose, reasoning, and a second batched call are all billed to the
same number, and none of them are the cost of the tool call.

A non-isolable arm still matches — the matcher cannot see, and does not care,
what else the response emitted. Context-token columns keep the real joined
payload size; the usage column shows `—`. The arm is **not** re-seeded: `*`
marks only an *absent* arm (S18). The run therefore reads as visibly incomplete
on usage rather than quietly wrong on context cost. Only a fresh session can
recover the usage number.

This defect shipped for three revisions because isolability was keyed on the
timestamp, which no real response shares across its entries. Measured over 400
session files: **no** assistant record holds two `tool_use` blocks, while **245**
`requestId`s do. The check for batched calls had never once fired on real data.

The fixtures hid it. All four put every block of a response in a single record —
a shape the runtime never emits — because they were authored from the same
mental model as the matcher they were meant to test. Fixtures written that way
supply confirmation, not verification. It is the third time this family of bug
has surfaced here (see S17 arm identification, and the `name_path`/`comment`
parameters serena never accepted), and the lesson has not changed: **pin
fixtures to a shape observed in a real transcript, not to the one the code
expects.**

## Usage columns are not yet comparable (TB-17)

Even when both usage cells are populated (isolable arms, S26), the two numbers
are **not** a fair tool-vs-Bash comparison today.

`output_tokens` is billed against the whole emitted `tool_use` block — tool
name plus serialized input. The bash arm is structurally required to carry a
sentinel comment (`  # TB_PROBE_<NN>_BASH_V2`, ~23 chars / ~10–12 BPE tokens)
that the tool arm cannot carry: serena's schemas have no free-text field
(TB-15), which is why the matcher identifies tool arms structurally. Operators
also often supply Bash's optional `description` (~8–10 tokens). Together that
is ~20 tokens of instrumentation charged only to the bash arm.

Measured on the first fully-isolable ten-arm run (session `ca1a80df`): mean
bash penalty **14.6** output tokens, and the instrumentation exceeds the gap
in 5/5 probes. "MCP is cheaper on output tokens" is therefore not established
by this data; removing the tax could flip the sign.

**Not a confound:** Serena's long MCP-namespaced tool name (~10 tok/call vs
Bash's 4) is a real cost of MCP namespacing and belongs in the comparison.

**Unaffected:** the context-token columns (`tool tokens` / `bash tokens`)
measure the returned `tool_result`, never the emitted call. They remain the
trustworthy half of the table.

TB-17 tracks the fix (stated correction, symmetric dead-weight, drop the
columns, or reconstruct analytically). Until one lands, read usage as
non-comparable and rank arms on context tokens.

## What `probe.py` will refuse (S30)

Score a **native Claude Code** session JSONL. `hermes sessions export
--format trace` is valid input to `passive.py` (claimed by
`HermesTraceParser`, usage stamped `ABSENT_BY_EXPORT`) but invalid input to
`probe.py`: the export drops `requestId`, and turns are keyed solely by that
field. `find_probe_calls` raises `NonIsolableTurns` at the door rather than
falling back to timestamps (the TB-16 defect class). There is no
partial-corpus mode.

## Performing vs. mentioning a probe (S17)

A sentinel is a bare string, so a call that *greps for* `TB_PROBE_01_BASH_V2`
looks exactly like the call that *performs* probe 01's bash arm.
`toolbench.probe.find_probe_calls` therefore discards any call that:

- names more than one probe sentinel (a real bash arm names exactly its own), or
- targets the transcript corpus or the probe's own source — see
  `MENTION_MARKERS` (`.claude/projects`, `toolbench/probe.py`, this file, and
  `protocols/probe-run-sheet.md`, which prints every bash arm verbatim).

The run sheet in `protocols/probe-run-sheet.md` is the executable form of this
protocol. Run it in a fresh session; it is written so the operator never needs
to open this file or `toolbench/probe.py` mid-run.

A tool-arm candidate carrying some *other* probe's sentinel is rejected on the
same grounds: a serena search of `tools/mcp.py` for `TB_PROBE_01_TOOL_V2` is a
hunt for a sentinel, not probe 02's tool arm.

This narrows contamination; it does not eliminate it. A single-sentinel grep
against the corpus file is indistinguishable from the bash arm it imitates.
**Run probes in a dedicated, fresh session and score only that session's
JSONL.** Do not score a session in which the probes were discussed, edited,
or searched for.

Each arm must be alone in its turn: `_usage_output_tokens` attributes real
usage only when the turn holds exactly one `tool_use` block and no non-tool
output (S26). Ten arms means ten turns. Do not batch them.

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
