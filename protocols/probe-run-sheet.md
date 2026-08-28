# Probe run sheet — ten arms, ten turns

Execute this in a **fresh session**, in the repo root, with serena loaded and a
project activated — turn 0 checks both, and you must run it. Do not read
`src/toolbench/probe.py` or `protocols/active-probes.md` during the run; you do not
need them, and reading them is what disqualified the last two sessions.

Everything you need is below. Copy each call exactly.

## The rules that make the run scoreable

1. **One tool call per turn.** Ten arms, ten turns. A *turn* here is one API
   response — one `requestId` — not one JSONL record. `_usage_output_tokens`
   attributes real usage only when that response emitted exactly one `tool_use`
   block. If you batch two calls into one turn, both arms lose their usage
   numbers.
2. **Never type a sentinel except in the one bash command that owns it.** Do not
   echo one, do not grep for one, do not paste this sheet into a shell. A call
   naming two sentinels is discarded; a call naming one is *indistinguishable
   from the arm that performs it*.
3. **Do not touch the corpus files with any other tool** during the run.
4. **Say nothing to the user mid-run that requires a tool call.** Prose between
   turns is free; tool calls are not.
5. **An arm turn carries the tool call and nothing else.** `output_tokens` is
   billed against the whole API response, so a sentence of prose or a block of
   reasoning emitted alongside the call is charged to the arm. Write your prose
   in the turns *between* arms, where it costs the run nothing. Rule 4 says
   prose between turns is free — this is the other half of that: prose *inside*
   an arm turn is not.

   The prose that kills an arm is rarely a paragraph. It is the transition
   sentence — "precondition green, starting the ten arms now" — emitted in the
   same response as turn 1's call, because announcing yourself feels natural at
   exactly that boundary. **Your last prose of the run comes before turn 0, not
   after it.** Once turn 0 passes, say nothing until turn 10 has returned. If
   turn 0 fails, you may speak while you fix it; the run has not started.
6. **Score the Claude Code session this run produces.** Do not feed a
   `hermes sessions export --format trace` file to `toolbench.probe` — it
   raises `NonIsolableTurns` (no `requestId`). Trace exports are for
   `passive` only.

Rules 1 and 5 are enforced (S26). A non-isolable arm still matches and keeps its
real context-token columns; only the usage cell shows `—`. It is **not**
re-seeded — `*` marks an absent arm, not a contaminated one (S18). The run is
not silently wrong, it is visibly incomplete on usage. Do not reach for
`--allow-seeded` to hide it.

Sentinels appear only in the bash arms. The tool arms carry none — serena's
schemas have no field to put one in, and the matcher identifies them
structurally by tool name plus corpus target.

## Turn 0 — precondition check

`mcp__plugin_serena_serena__find_file`

```json
{"file_mask": "pyproject.toml", "relative_path": "."}
```

Expect `{"files": ["pyproject.toml"]}`. Anything else — an error, an empty list,
a prompt to pick a project — means serena has no active project. Activate it
with `activate_project` on the repo root, re-run turn 0, and only then start
turn 1.

This turn exists because a *failed* arm call is unrecoverable. The matcher binds
an arm to any call carrying that arm's tool name and corpus target, and it
cannot see whether the call succeeded. A `find_file` for `regex_check.py` that
errors out still matches probe 01's tool arm, and still gets scored — as the
token cost of an error message. Re-running it only adds a second structurally
identical match. So the precondition must be tested by a call that is *not* an
arm, before any arm is spent.

`pyproject.toml` is what makes it not an arm. The five corpus targets are
`regex_check.py`, `mcp.py`, `monitor.py`, `llm_extraction.py`, and
`code_analysis.py`; `pyproject.toml` is none of them, so the tool-name half of
the match succeeds and the target half fails. The call exercises serena
end-to-end and matches nothing.

Turn 0 is not an arm, carries no sentinel, and touches no corpus file. It does
not count against the ten turns.

It is not an invitation to narrate, either. Turn 0 passing is the *quietest*
moment of the run, not a checkpoint to report: turn 1 follows immediately, and
a word of acknowledgement between them lands inside turn 1's response and
costs probe 01 its usage number (rule 5). Go straight from turn 0's result to
turn 1's call.

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
uv run toolbench probe --session <that path>
# equivalent: uv run python -m toolbench.probe --session <that path>
```

Expect ten unseeded cells and no `*` in the table. If any cell is seeded, an arm
did not match — do not paper over it with `--allow-seeded`. A fully-seeded table
raises `SeededReportError` by design.

Check the two usage columns as well. A `—` there means the arm matched but its
response was not isolable: it batched a second call, or carried prose or
reasoning (rules 1 and 5). The context-token columns are still good; the usage
number for that arm is gone, and only a fresh session can recover it. An arm is
spent once its call is in the transcript — re-running it adds a second match
rather than replacing the first, which is the same reason turn 0 exists.

When both usage cells *are* filled, do **not** treat them as a fair comparison.
The bash arm's sentinel (and any Bash `description` you typed) is billed into
bash `output_tokens` only — roughly 15–20 tokens of instrumentation that the
tool arm cannot carry (TB-17). Prefer the context-token columns for ranking
until that ticket lands a stated correction.

## Deviation from `active-probes.md`

That protocol says the bash arm uses `grep`/`find`. This sheet uses `rg`/`fd`,
which are this operator's standard tools and are what a bash arm would honestly
reach for here. The comparison stays fair — each probe pits one tool call against
one bash call over the same file and pattern — but the bash numbers are `rg`/`fd`
numbers, not `grep`/`find` numbers, and the report should be read that way.
