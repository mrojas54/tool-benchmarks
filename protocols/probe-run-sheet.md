# Probe run sheet — ten arms, ten turns

Execute this in a **fresh session**, in the repo root, with serena loaded. Do not
read `toolbench/probe.py` or `protocols/active-probes.md` during the run; you do
not need them, and reading them is what disqualified the last two sessions.

Everything you need is below. Copy each call exactly.

## The rules that make the run scoreable

1. **One tool call per turn.** Ten arms, ten turns. `_usage_output_tokens`
   attributes real usage only when a turn holds exactly one `tool_use` block.
   If you batch two calls into one turn, both arms lose their usage numbers.
2. **Never type a sentinel except in the one bash command that owns it.** Do not
   echo one, do not grep for one, do not paste this sheet into a shell. A call
   naming two sentinels is discarded; a call naming one is *indistinguishable
   from the arm that performs it*.
3. **Do not touch the corpus files with any other tool** during the run.
4. **Say nothing to the user mid-run that requires a tool call.** Prose between
   turns is free; tool calls are not.

Sentinels appear only in the bash arms. The tool arms carry none — serena's
schemas have no field to put one in, and the matcher identifies them
structurally by tool name plus corpus target.

## The ten turns

| turn | probe | arm | task |
|------|-------|-----|------|
| 1 | 01 | tool | find `regex_check.py` |
| 2 | 01 | bash | find `regex_check.py` |
| 3 | 02 | tool | search `^def ` in `mcp.py` |
| 4 | 02 | bash | search `^def ` in `mcp.py` |
| 5 | 03 | tool | find `monitor.py` |
| 6 | 03 | bash | find `monitor.py` |
| 7 | 04 | tool | search `^def ` in `llm_extraction.py` |
| 8 | 04 | bash | search `^def ` in `llm_extraction.py` |
| 9 | 05 | tool | find `code_analysis.py` |
| 10 | 05 | bash | find `code_analysis.py` |

### Turn 1 — probe 01, tool arm

`mcp__plugin_serena_serena__find_file`

```json
{"file_mask": "regex_check.py", "relative_path": "tools"}
```

### Turn 2 — probe 01, bash arm

`Bash`

```
fd -H -I --glob 'regex_check.py' tools  # TB_PROBE_01_BASH_V2
```

### Turn 3 — probe 02, tool arm

`mcp__plugin_serena_serena__search_for_pattern`

```json
{"substring_pattern": "^def ", "relative_path": "tools/mcp.py"}
```

### Turn 4 — probe 02, bash arm

`Bash`

```
rg -n '^def ' tools/mcp.py  # TB_PROBE_02_BASH_V2
```

### Turn 5 — probe 03, tool arm

`mcp__plugin_serena_serena__find_file`

```json
{"file_mask": "monitor.py", "relative_path": "tools"}
```

### Turn 6 — probe 03, bash arm

`Bash`

```
fd -H -I --glob 'monitor.py' tools  # TB_PROBE_03_BASH_V2
```

### Turn 7 — probe 04, tool arm

`mcp__plugin_serena_serena__search_for_pattern`

```json
{"substring_pattern": "^def ", "relative_path": "tools/llm_extraction.py"}
```

### Turn 8 — probe 04, bash arm

`Bash`

```
rg -n '^def ' tools/llm_extraction.py  # TB_PROBE_04_BASH_V2
```

### Turn 9 — probe 05, tool arm

`mcp__plugin_serena_serena__find_file`

```json
{"file_mask": "code_analysis.py", "relative_path": "tools"}
```

### Turn 10 — probe 05, bash arm

`Bash`

```
fd -H -I --glob 'code_analysis.py' tools  # TB_PROBE_05_BASH_V2
```

## Scoring

After turn 10, locate the session transcript and score it. Both commands name
`.claude/projects`, which is a `MENTION_MARKER`, so they are discarded by the
matcher rather than mistaken for arms — it is safe to run them in the same
session.

```
ls -t ~/.claude/projects/-Users-michellerojas-tool-benchmarks/*.jsonl | head -1
```

```
uv run python -m toolbench.probe --session <that path>
```

Expect ten unseeded cells and no `*` in the table. If any cell is seeded, an arm
did not match — do not paper over it with `--allow-seeded`. A fully-seeded table
raises `SeededReportError` by design.

## Deviation from `active-probes.md`

That protocol says the bash arm uses `grep`/`find`. This sheet uses `rg`/`fd`,
which are this operator's standard tools and are what a bash arm would honestly
reach for here. The comparison stays fair — each probe pits one tool call against
one bash call over the same file and pattern — but the bash numbers are `rg`/`fd`
numbers, not `grep`/`find` numbers, and the report should be read that way.
