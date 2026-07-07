# tool-benchmarks — design spec

**Date:** 2026-07-07
**Approach:** C v2 (hybrid) — multi-agent passive analyzer backbone + thin active-probe scorer
**Location:** `~/tool-benchmarks/`
**Builds on:** claude-mem observation #8376 (native Grep/Glob vs Bash benchmark methodology)
**Review artifact:** https://claude.ai/code/artifact/586e9793-06c4-4b70-ad91-ef75fa884de8

## Purpose

Analyze tooling inefficiencies across agent systems such as Claude Code, Hermes, Codex, and other AgentsView-supported runtimes. The goal is evidence for where agent work is wasting context, time, retries, or tool calls.

Initial targets:

1. **Cross-agent tool cost** — which tools, agents, projects, and workflows dump the most context back into sessions?
2. **Tooling inefficiency patterns** — where do sessions show repeated failed calls, slow tools, edit churn, retry loops, context pressure, or subagent fan-out?
3. **Deferral and discovery tax** — what does deferred-tool loading/searching cost across Claude Code, Codex, Hermes, and similar surfaces?
4. **Controlled tool-vs-shell probes** — for comparable local tasks, when are native tools cheaper or more reliable than shell commands?

Single deliverable: a re-runnable harness + a markdown report.

## Non-goals (scope guards)

- **No HTML report.** The `session-report` skill already owns rich HTML; this emits markdown only.
- **No live token-API calls.** All numbers derive from on-disk transcripts.
- **No transcript mutation.** Read-only over all configured agent session sources.
- **No unrelated refactors.** Active probes stay scoped to a fixed local corpus and do not mutate source projects.
- **No third-party dependencies.** Python standard library only, so the harness runs anywhere `python3` exists.
- **No web-chat benchmarking.** Claude.ai web comparison is out of scope; this repo is about local/agentic tooling surfaces with inspectable sessions, transcripts, or AgentsView exports.

## Data substrate

Primary source of truth: inspectable agent session records. V2 supports two discovery/export paths:

- `raw`: scan known local transcript roots directly, starting with Claude Code `~/.claude/projects/**/*.jsonl` and adding Codex/Hermes adapters as their on-disk shapes are confirmed.
- `agentsview`: use the installed AgentsView CLI as the cross-agent session index, then parse exported raw session data through source-specific adapters.

The source parsers remain authoritative for context-cost and inefficiency metrics. AgentsView is an index, count source, and cross-agent retrieval layer, not a substitute metric engine.

Observed scale from AgentsView on 2026-07-07:

- Full visible corpus: **8,103 sessions**, **101,919 messages**, **86 projects**.
- Current 30-day window: **4,579 sessions**.
- Heavy sessions can exceed **90 tool calls**, **86 turns**, and include multiple subagents.

V2 requirement: the passive analyzer must stream and aggregate incrementally. It must not build one full in-memory `list[ToolCall]` for the entire corpus before reducing.

AgentsView CLI contract, verified locally on 2026-07-07 with `agentsview v0.36.1`:

- `/opt/homebrew/bin/agentsview stats --agent all --since 2026-06-08 --until 2026-07-07 --json` gives window-scoped corpus counts when the local daemon is healthy.
- `/opt/homebrew/bin/agentsview projects --json` gives project counts.
- `/opt/homebrew/bin/agentsview session list --agent AGENT_OR_ALL --date-from YYYY-MM-DD --date-to YYYY-MM-DD --include-children --include-automated --include-one-shot --limit 500 --json` pages session metadata; implementation must follow returned cursors because the CLI caps pages at 500 sessions.
- `/opt/homebrew/bin/agentsview session export <id>` streams the raw source JSONL for a session and can be fed directly into `parse_session`.
- `/opt/homebrew/bin/agentsview session tool-calls <id> --json` is useful for validation/debugging only; do not use it as the primary context-cost source because the benchmark needs the raw joined result payload.

Failure behavior: `--index-source auto` tries AgentsView first and falls back to raw filesystem scanning if the CLI is missing or exits nonzero (for example, local daemon "running but not responding"). `--index-source agentsview` is strict and exits with a clear error if AgentsView cannot serve data.

Initial Claude Code record shapes (verified 2026-07-07, revised for v2):

- `assistant` entries carry `message.content[]` (blocks; `type=="tool_use"` gives `id`, `name`, `input`) and `message.usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`).
- `user` entries can expose the join key in either top-level `toolUseID` or in `message.content[]` blocks where `type=="tool_result"` and `tool_use_id` equals the assistant-side tool-use id.
- The authoritative result payload can be top-level `toolUseResult` **or** block-local `message.content[].content`. Real Claude Code transcripts commonly use the block-local shape; v2 must parse it directly.
- Tool call/result join key: the assistant-side `message.content[].id` equals either top-level `toolUseID` or block-local `tool_use_id`.

**Token estimate convention:** `est_tokens(chars) = chars / 4`, applied identically to inputs and outputs (matches #8376). Where a turn issues exactly one tool call, the real `usage` delta is available and preferred for the active arm.

Normalized records must carry `agent`, `source`, `project`, `session_id`, and `ts` so reports can compare Claude Code, Hermes, Codex, and other sources without collapsing them into one bucket.

## Architecture

```
raw roots + AgentsView exports
          │
          ▼
 source adapters ── normalize tool calls and inefficiency signals
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
│   ├── transcript.py             # shared normalized records + Claude Code parser
│   ├── sources.py                # source/agent discovery and AgentsView adapters
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
  Yields Claude Code session JSONL paths from the raw filesystem source, optionally filtered to one project dir and/or a start timestamp.
- `iter_agentsview_sessions(agent="all", date_from=None, date_to=None, project=None, include_children=True) -> Iterator[SessionRef]`
  Pages `agentsview session list --json` and yields session ids plus metadata for export.
- `open_session_jsonl(ref) -> Iterable[str]`
  Streams JSONL lines from either a filesystem path or `agentsview session export <id>`, so the parser and reducers do not care which index source found the session.
- `parse_session(path) -> list[ToolCall]`
  Streams one session; joins each `tool_use` block to its matching `tool_result`/`toolUseResult` payload by id.
- `ToolCall` (dataclass): `agent, source, project, name, input_chars, output_chars, tokens (=output_chars/4), input_tokens (=input_chars/4), session_id, ts, usage (optional turn usage dict), duration_ms, error`.
- `result_len(toolUseResult) -> int`
  Normalizes dict / string / block-list / block-local `content` payloads to a character length.

Robustness:

- Malformed / partial JSON lines are counted and skipped, never fatal. The count is exposed for the report footer.
- A `tool_use` with no matching result (interrupted turn) yields `output_chars = 0` with a `no_result=True` flag; it is kept, not dropped.
- If both top-level `toolUseResult` and block-local `content` exist, v2 uses block-local `content` for the joined `tool_result` block and records which source was used.
- Parser tests must include a sanitized real-shaped record where top-level `toolUseResult` is absent or wrapper-like and the payload is block-local `content`.

### `passive.py` — targets #3 + #2-passive

Aggregates `ToolCall`s across a session selection and writes the repeatable report.

CLI: `python3 -m toolbench.passive [--agent all|claude|codex|hermes|...] [--all | --project NAME] [--since ISO8601] [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD] [--out PATH] [--limit N] [--exclude-subagents] [--index-source auto|raw|agentsview] [--verbose]`
Default scope is **`--agent all --all`**. `--project` narrows to one project; `--since` bounds by timestamp for raw sources; `--date-from` / `--date-to` are passed through to AgentsView session listing.

V2 scale behavior:

- Aggregate incrementally per parsed session file; keep only per-tool reducers and report counters globally.
- Include subagent transcripts by default because they are real tool use; `--exclude-subagents` removes paths containing `/subagents/`.
- `--since` is file-mtime based in v2 unless a future `--since-message-ts` is added; the report must state this.
- `--index-source auto` records whether the run used AgentsView or raw scanning. Reports must include index source, scanned/exported session count, and any AgentsView fallback reason.
- Every aggregate is keyed by agent as well as tool; the report must include an agent breakdown before the global leaderboard.
- `--limit N` caps session files for smoke tests.
- `--verbose` reports periodic progress to stderr every fixed number of files.

Report sections:

1. **Agent breakdown** — per agent: sessions, tool calls, total context-tokens, failures, slow-call count, retry/edit-churn indicators where available.
2. **Tool leaderboard** — per agent + tool: call count, total context-tokens, median context-tokens, total input-tokens, error count, slowest observed call.
3. **Inefficiency callouts** — ToolSearch/deferred-tool tax, repeated failed calls, oversized outputs, subagent fan-out, context pressure, and edit churn where source data exposes it.
4. **Summary** — total sessions, total tool calls, total tool-output tokens, top-5 cost drivers, count of skipped malformed lines.

### `probe.py` + `protocols/active-probes.md` — targets #1 + #2-active

`active-probes.md` defines sentinel-tagged fixed probes on an explicit 5-file corpus under `/Users/michellerojas/c11-sidequests`, each a **matched pair** — the tool this build ships vs. its Bash equivalent. V2 must list the exact five file paths in the protocol before implementation; `/Users/michellerojas/c11-sidequests` itself contains more than five files and does not guarantee a root `README.md`.

| Task | In-build tool arm | Bash arm |
|------|-------------------|----------|
| Read a file | `Read` (full file) | `cat` / `sed -n` |
| Content search | serena `search_for_pattern` (`rename`) | `rg -n rename` |
| File find | serena `find_file` / `list_dir` (`*.md`) | `rg --files -g '*.md'` / `find` |
| Deferral (02-active) | one `ToolSearch` load → first call cycle | — (baseline is the pre-loaded call) |

Each probe embeds a globally unique sentinel per arm (for example `TB_PROBE_READ_TOOL_V2`, `TB_PROBE_READ_BASH_V2`). Sentinels must not be substrings of one another, and the scorer must verify the expected tool name as well as the sentinel.

Flow: the operator (Claude) executes the probes in a session, then runs
`python3 -m toolbench.probe --session PATH` which finds the sentinel-marked calls, extracts context cost and, where an isolable single-tool turn is available, **real** usage tokens per arm, and emits the controlled comparison table — seeded with the #8376 serena baselines (content `rename` ≈ 723 tok serena / 794 tok Bash; file-find `*.md` ≈ 68 tok serena / 89 tok Bash).

**Optional bonus arm:** if native `Grep`/`Glob` are present in the build, they are probed too and added as extra columns. They are no longer required — every primary arm uses a tool that exists here, so the probe always completes without a fallback (the reverse of the #8376 gap).

## Metric definitions

| Metric | Definition | Role |
|--------|------------|------|
| **Context cost** | joined tool-result payload tokens (`chars/4`) — what the tool dumps into context | primary ranking |
| **Real tokens** | `usage` delta on an isolable single-tool turn | active arm primary when available |
| **Tool failure** | tool result/error state when available, or source-specific failure signal | inefficiency callout |
| **Slow call** | duration above report threshold when source metadata includes timing | inefficiency callout |
| **Retry/edit churn** | repeated similar tool calls, high edit churn, or source-provided churn signal | inefficiency callout |
| **Cache flag** | `cache_creation_input_tokens > 0` on a turn | caveat only; never used for per-tool ranking |

## Testing

`tests/fixtures/sample.jsonl` is hand-crafted to exercise the parser's edge cases:

- a tool call with a **string** `toolUseResult`,
- a tool call with an **MCP block-list** `toolUseResult`,
- a real-shaped tool result whose payload is `message.content[].content`,
- a tool call with **no result** (interrupted),
- one **malformed** line.

`tests/test_transcript.py` (stdlib `unittest`) asserts: the id-join is correct, `result_len` handles all three result shapes, the no-result call is kept with `output_chars=0`, and the malformed line is counted and skipped. Run with `python3 -m unittest discover tests`.

V2 acceptance smoke tests:

- `python3 -m toolbench.passive --agent claude --project c11_sidequests --limit 20 --out /tmp/toolbench-smoke.md` completes and reports parsed file/call counts.
- `python3 -m toolbench.passive --agent all --all --limit 200 --out /tmp/toolbench-scale.md --verbose` shows progress and completes without unbounded memory growth.
- `python3 -m toolbench.passive --agent all --all --index-source auto --limit 20 --out /tmp/toolbench-agentsview.md --verbose` completes through AgentsView when the daemon is healthy, or falls back to raw scanning and states the fallback reason when it is not.
- The final report states whether subagents were included, the session-file count scanned, the tool-call count joined, and the malformed-line count.

## Error handling

- Malformed JSONL line → counted, skipped, surfaced in report footer.
- Tool call missing its result → `output_chars=0`, flagged.
- Empty session selection → clear message, exit 0 (not an error).
- Missing selected raw root → clear message naming the agent/source, exit 1 for strict source selection; `--agent all --index-source auto` may continue with other available sources and report skipped roots.

## Open follow-ups (out of scope for v1)

- HTML rendering via `session-report` if a richer view is wanted later.
- Trend tracking across dated report runs.
- Per-model breakdown (usage records carry `model`).
