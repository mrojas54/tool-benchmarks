# tool-benchmarks — SPEC

Numbered acceptance criteria with stable IDs. Derived from
`docs/2026-07-07-tool-benchmarks-design.md` (v2) and the v2 implementation
plan. Each ID is referenced by `EVALUATION.md` and by the BUILDPLAN tickets.

## Parser & records — `toolbench/transcript.py`

- **S1 — id-join.** `parse_session(path)` joins each assistant `tool_use`
  block to its result by id. The join key is `message.content[].id`
  (assistant, `type=="tool_use"`), matched against either top-level
  `toolUseID` or block-local `message.content[].tool_use_id` (user side).
- **S2 — payload resolution.** The result payload resolves from top-level
  `toolUseResult` **or** block-local `message.content[].content`. When both
  exist, block-local `content` wins, and which source was used is recorded.
- **S3 — `result_len`.** Normalizes dict / string / MCP block-list /
  block-local `content` payloads to a character length.
- **S4 — `ToolCall`.** Carries `agent, source, project, name, input_chars,
  output_chars, tokens (=output_chars/4), input_tokens (=input_chars/4),
  session_id, ts, usage, duration_ms, error, model`. `model` is the model
  string of the assistant turn that emitted the `tool_use` (sibling of
  `usage` on the transcript `message`); `None` when the source omits it.
- **S5 — malformed non-fatal.** Malformed / partial JSON lines are counted,
  skipped, never fatal; the count is exposed for the report footer
  (`ParseResult(calls, malformed)`).
- **S6 — interrupted kept.** A `tool_use` with no matching result yields
  `output_chars=0, no_result=True`; it is kept, not dropped.

## Multi-agent discovery — `toolbench/sources.py`

- **S7 — raw discovery.** `iter_session_files(root="~/.claude/projects",
  project=None, since=None)` yields Claude Code JSONL paths, filtered by
  owning-project-dir substring (`project in rel.parts[0]`, so
  `<project>/subagents/*.jsonl` still matches — TB-8) and file mtime
  (`since`, ISO-8601); raises `FileNotFoundError` if `root` is missing.
- **S8 — AgentsView listing.** `iter_agentsview_sessions(agent="all", …)`
  pages `agentsview session list --json --limit 500` with **cursor
  pagination** and yields `SessionRef(agent, source, project, session_id,
  path)`.
- **S9 — uniform open.** `open_session_jsonl(ref)` streams JSONL lines from
  either a filesystem path or `agentsview session export <id>`. A payload
  containing a NUL in its first 8 KiB is **not** a transcript: raise
  `NonTranscriptExport` rather than absorb megabytes of binary as malformed
  lines. The guard is agent-agnostic and stays so regardless of any upstream
  fix (TB-10).
- **S9a — hermes direct read.** `agent == "hermes"` sessions are read from the
  hermes archive (`$HERMES_HOME`, default `~/.hermes`) via
  `parse_hermes_session`, never via `agentsview session export`, which returns
  `rc=0` and streams the whole default-profile database. Databases are opened
  read-only. A session is resolved against every profile database
  (`state.db`, then `profiles/*/state.db`); two of the 29 in-corpus sessions
  exist only in a non-default profile and are unreachable even by a *fixed*
  export. An unreadable archive raises `NonTranscriptExport` and degrades the
  session to `skipped_roots` (TB-11).
- **S9b — hermes discovery is not ours.** Hermes sessions are enumerated by
  `agentsview session list`, never from the archive. The corpus is *defined* as
  what that call returns, for every agent; enumerating the archive for one agent
  would redefine the corpus and skew every cross-agent rate. Hermes is
  consequently under-sampled — `session list` returns 89 sessions where
  `agentsview stats` counts 789 from the same archive — which is an upstream
  defect to fix, not one to route around here (TB-11).
- **S10 — index-source policy.** `--index-source auto` tries AgentsView
  first and falls back to raw scanning (recording the reason) if the CLI is
  missing or exits nonzero; `agentsview` is strict and errors clearly;
  `raw` uses the filesystem only.

## Passive analyzer — `toolbench/passive.py`

- **S11 — incremental.** Aggregation streams per parsed session; **no full
  in-memory `list[ToolCall]`** for the corpus — only per-agent/per-tool
  reducers and report counters live globally.
- **S12 — CLI.** Flags: `--agent`, `--all | --project`, `--since`,
  `--date-from`, `--date-to`, `--out`, `--limit`, `--exclude-subagents`,
  `--index-source`, `--verbose`; default scope `--agent all --all`.
- **S13 — subagents.** Included by default; `--exclude-subagents` removes
  paths containing `/subagents/`.
- **S14 — report sections.** Five, in order: (1) Agent breakdown, (2) Tool
  leaderboard (per agent+tool), (3) Model breakdown (per agent+model+tool,
  `model` normalized to `unknown` when absent), (4) Inefficiency callouts
  (ToolSearch/deferral tax, failures, oversized outputs, subagent fan-out,
  churn), (5) Summary.
- **S15 — report provenance.** The report states the index source used,
  sessions scanned, tool calls joined, malformed-line count, whether
  subagents were included, and any AgentsView fallback reason; it notes
  `--since` is file-mtime based.

## Active probes — `toolbench/probe.py` + `protocols/active-probes.md`

- **S16 — vendored corpus.** The probe corpus is **five files vendored under
  `tools/`** (relative paths, committed to the repo so probes are reproducible
  from a clean checkout — no external absolute paths). The five files are a
  log-spaced size spread (~121 → ~2,242 lines) so the comparison shows how
  tool-vs-Bash context cost scales with target size. The corpus files are
  probe *inputs* (search/read targets); `active-probes.md` lists each of the
  five relative paths, and each probe is a matched tool-vs-Bash pair over one
  of them. Probe *output* (the comparison table, token measurements) is
  written under `reports/`, never mixed with inputs.
- **S17 — arm identification.** Globally unique per-arm sentinels
  (`TB_PROBE_*_V2`), none a substring of another. The two arms leave
  different evidence, so they match differently (TB-15):
  - **Tool arm** — structurally: an accepted tool name
    (`mcp__plugin_serena_serena__…` or bare `mcp__serena__…`) plus the
    corpus *target* basename in the call input. Serena's schemas have no
    inert free-text field, so a tool arm carries no required sentinel.
  - **Bash arm** — by its own bash sentinel in the command text.
  A call is discarded when it trips `MENTION_MARKERS` (transcript corpus /
  probe source / run sheet), names more than one sentinel, or (for a tool
  arm) carries some *other* probe's sentinel. Matching an arm is not the
  same as pricing it — see S26.
- **S18 — comparison table.** Emits context-tokens per matched arm plus
  real `usage.output_tokens` when the turn is isolable (S26). An absent
  arm is seeded with #8376 baselines (`search` 723 serena / 794 Bash;
  `find` 68 serena / 89 Bash) and marked `*`. A matched but non-isolable
  arm keeps its real context tokens and shows `—` for usage — it is **not**
  re-seeded. A fully-seeded table raises `SeededReportError` unless
  `--allow-seeded`.

## Metrics, quality, errors

- **S19 — metric roles.** Context cost = joined result-payload tokens
  (`chars/4`) is the primary ranking; the cache flag is caveat-only and
  never ranks tools; failure / slow / retry-churn feed inefficiency
  callouts only.
- **S20 — stdlib runtime, uv project.** The shipped `toolbench` package
  imports nothing third-party; the project is uv-managed (`pyproject.toml`
  + `uv.lock`, empty runtime deps, `dev` group `ruff`/`mypy`/`pytest`).
- **S21 — entry points.** Runnable as `uv run python -m toolbench.passive`
  and `… toolbench.probe`; tests via `uv run python -m unittest discover
  tests`.
- **S22 — strict gate.** `uv run ruff check .`, `uv run mypy --strict
  toolbench tests`, and the full unittest suite are green before any PR.
- **S23 — error handling.** Empty session selection → clear message,
  exit 0. Missing selected raw root → exit 1 for a strict source; but
  `--agent all --index-source auto` continues with other sources and
  reports skipped roots.

## Testing

- **S24 — fixtures.** Parser fixtures exercise: a string result, an MCP
  block-list, a **block-local `content`** payload, an interrupted (no-result)
  call, and a malformed line. `test_sources.py` uses a **fake `agentsview`
  runner** so pagination + arg construction are tested without the daemon.
  Probe fixtures must pin shapes observed in real transcripts (multi-entry
  `requestId` responses, real serena parameter schemas) — not the shape the
  matcher expects.
- **S25 — acceptance smoke.** A `--project` slice reports parsed counts;
  `--all --limit 200 --verbose` completes without unbounded memory;
  `--index-source auto --limit 20` completes via AgentsView or falls back to
  raw and states the reason.
- **S26 — response-pooled isolability.** `output_tokens` is billed per API
  response. Claude Code writes one response as several JSONL entries
  (`thinking` / `text` / each `tool_use`) that share a `requestId` and a
  single `usage` figure but carry distinct timestamps. A turn is therefore
  the `requestId` (falling back to timestamp when absent). An arm's usage is
  attributable only when that response emitted exactly one `tool_use` and no
  non-empty `text` / `thinking` / `redacted_thinking` block. Batching,
  prose, or reasoning in the arm turn keeps the match and the context-token
  columns, and blanks usage (`—`). Only a fresh session recovers the number
  (TB-16).
