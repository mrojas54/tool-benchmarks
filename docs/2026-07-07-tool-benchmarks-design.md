# tool-benchmarks — design spec

**Date:** 2026-07-07
**Approach:** C (hybrid) — passive analyzer backbone + thin active-probe scorer
**Location:** `~/tool-benchmarks/`
**Builds on:** claude-mem observation #8376 (native Grep/Glob vs Bash benchmark methodology)
**Review artifact:** https://claude.ai/code/artifact/586e9793-06c4-4b70-ad91-ef75fa884de8

## Purpose

Measure, in real tokens, what Claude Code tools cost — so tool choice can be made on evidence, not intuition. Three targets:

1. **In-build tools vs. Bash** — is the `Read` tool / serena search cheaper than the Bash equivalent (`cat`, `rg`)? (controlled, active)
2. **ToolSearch deferral tax** — how many tokens does the deferred-tool pattern cost across history, and per load→call cycle? (mostly passive + one active probe)
3. **Which tools I actually use** — a per-tool context-cost leaderboard from transcript history. (passive)

Single deliverable: a re-runnable harness + a markdown report.

## Non-goals (scope guards)

- **No HTML report.** The `session-report` skill already owns rich HTML; this emits markdown only.
- **No live token-API calls.** All numbers derive from on-disk transcripts.
- **No transcript mutation.** Read-only over `~/.claude/projects`.
- **No unrelated refactors.** The active-probe corpus stays the fixed 5 files in `/Users/michellerojas/c11-sidequests`.
- **No third-party dependencies.** Python standard library only, so the harness runs anywhere `python3` exists.

## Data substrate

Source: `~/.claude/projects/**/*.jsonl` (one dir per project, one file per session).

Relevant record shapes (verified 2026-07-07):

- `assistant` entries carry `message.content[]` (blocks; `type=="tool_use"` gives `id`, `name`, `input`) and `message.usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`).
- `user` entries carry `toolUseResult` — a dict, string, or MCP-style list of content blocks — plus the `toolUseID` linking back to the call.
- Tool call ↔ tool result join key: the tool-use `id` (assistant side) equals the `toolUseID` (user side).

**Token estimate convention:** `est_tokens(chars) = chars / 4`, applied identically to inputs and outputs (matches #8376). Where a turn issues exactly one tool call, the real `usage` delta is available and preferred for the active arm.

## Architecture

```
~/.claude/projects/**/*.jsonl
          │
          ▼
   transcript.py  ── join tool_use ⇄ toolUseResult by id
          │
     ┌────┴─────┐
     ▼          ▼
 passive.py   probe.py
 (03 + 02p)   (01 + 02a)
     │          │
     └────┬─────┘
          ▼
 reports/YYYY-MM-DD-tool-usage.md
```

### Directory layout

```
~/tool-benchmarks/
├── README.md                     # how to run each entry point
├── toolbench/
│   ├── __init__.py
│   ├── transcript.py             # shared parser: JSONL → ToolCall records
│   ├── passive.py                # targets #3 + #2-passive
│   └── probe.py                  # targets #1 + #2-active
├── protocols/
│   └── active-probes.md          # fixed probe calls with sentinel markers
├── tests/
│   ├── fixtures/sample.jsonl     # hand-crafted transcript fixture
│   └── test_transcript.py        # stdlib unittest
├── docs/
│   └── 2026-07-07-tool-benchmarks-design.md   # this file
└── reports/
    └── YYYY-MM-DD-tool-usage.md  # generated
```

Runnable as `python3 -m toolbench.passive` / `python3 -m toolbench.probe` from `~/tool-benchmarks/`.

## Components

### `transcript.py` — shared substrate

One job: turn raw JSONL into normalized, joined tool-call records.

Public interface:

- `iter_session_files(root="~/.claude/projects", project=None, since=None) -> Iterator[Path]`
  Yields session JSONL paths, optionally filtered to one project dir and/or a start timestamp.
- `parse_session(path) -> list[ToolCall]`
  Streams one session; joins each `tool_use` block to its `toolUseResult` by id.
- `ToolCall` (dataclass): `name, input_chars, output_chars, tokens (=output_chars/4), input_tokens (=input_chars/4), session_id, ts, usage (optional turn usage dict)`.
- `result_len(toolUseResult) -> int`
  Normalizes dict / string / list-of-blocks to a character length (JSON-serializes dicts; sums text of block lists).

Robustness:

- Malformed / partial JSON lines are counted and skipped, never fatal. The count is exposed for the report footer.
- A `tool_use` with no matching result (interrupted turn) yields `output_chars = 0` with a `no_result=True` flag; it is kept, not dropped.

### `passive.py` — targets #3 + #2-passive

Aggregates `ToolCall`s across a session selection and writes the repeatable report.

CLI: `python3 -m toolbench.passive [--all | --project NAME] [--since ISO8601] [--out PATH]`
Default scope is **`--all`** (every project). `--project` narrows to one; `--since` bounds by timestamp.

Report sections:

1. **Leaderboard** — per tool: call count, total context-tokens (ranked desc), median context-tokens, total input-tokens.
2. **ToolSearch callout** (target #2-passive) — total `ToolSearch` calls + total tokens its schema dumps have cost across the selection: the accumulated deferral tax, with an average-per-load figure.
3. **Summary** — total tool calls, total tool-output tokens, top-5 cost drivers, count of skipped malformed lines.

### `probe.py` + `protocols/active-probes.md` — targets #1 + #2-active

`active-probes.md` defines sentinel-tagged fixed probes on the 5-file `c11-sidequests` corpus, each a **matched pair** — the tool this build ships vs. its Bash equivalent:

| Task | In-build tool arm | Bash arm |
|------|-------------------|----------|
| Read a file | `Read` (full file) | `cat` / `sed -n` |
| Content search | serena `search_for_pattern` (`rename`) | `rg -n rename` |
| File find | serena `find_file` / `list_dir` (`*.md`) | `rg --files -g '*.md'` / `find` |
| Deferral (02-active) | one `ToolSearch` load → first call cycle | — (baseline is the pre-loaded call) |

Each probe embeds a unique sentinel (e.g. a marker string in the query/path) so the scorer can locate it in the current session transcript.

Flow: the operator (Claude) executes the probes in a session, then runs
`python3 -m toolbench.probe --session PATH` which finds the sentinel-marked calls, extracts **real** input/output token cost per arm, and emits the controlled comparison table — seeded with the #8376 serena baselines (content `rename` ≈ 723 tok serena / 794 tok Bash; file-find `*.md` ≈ 68 tok serena / 89 tok Bash).

**Optional bonus arm:** if native `Grep`/`Glob` are present in the build, they are probed too and added as extra columns. They are no longer required — every primary arm uses a tool that exists here, so the probe always completes without a fallback (the reverse of the #8376 gap).

## Metric definitions

| Metric | Definition | Role |
|--------|------------|------|
| **Context cost** | `toolUseResult` tokens (`chars/4`) — what the tool dumps into context | primary ranking |
| **Real tokens** | `usage` delta on an isolable single-tool turn | active arm |
| **Cache flag** | `cache_creation_input_tokens > 0` on a turn | caveat only; never used for per-tool ranking |

## Testing

`tests/fixtures/sample.jsonl` is hand-crafted to exercise the parser's edge cases:

- a tool call with a **string** `toolUseResult`,
- a tool call with an **MCP block-list** `toolUseResult`,
- a tool call with **no result** (interrupted),
- one **malformed** line.

`tests/test_transcript.py` (stdlib `unittest`) asserts: the id-join is correct, `result_len` handles all three result shapes, the no-result call is kept with `output_chars=0`, and the malformed line is counted and skipped. Run with `python3 -m unittest discover tests`.

## Error handling

- Malformed JSONL line → counted, skipped, surfaced in report footer.
- Tool call missing its result → `output_chars=0`, flagged.
- Empty session selection → clear message, exit 0 (not an error).
- Missing `~/.claude/projects` → clear message, exit 1.

## Open follow-ups (out of scope for v1)

- HTML rendering via `session-report` if a richer view is wanted later.
- Trend tracking across dated report runs.
- Per-model breakdown (usage records carry `model`).
