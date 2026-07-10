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
  `<project>/subagents/*.jsonl` still matches — TB-8; not merely
  `path.parent.name`) and file mtime (`since`, ISO-8601); raises
  `FileNotFoundError` if `root` is missing.
- **S8 — AgentsView listing.** `iter_agentsview_sessions(agent="all", …)`
  pages `agentsview session list --json --limit 500` with **cursor
  pagination** and yields `SessionRef(agent, source, project, session_id,
  path)`.
- **S9 — uniform open.** `open_session_jsonl(ref)` streams JSONL lines from
  either a filesystem path or `agentsview session export <id>`. Text is
  decoded with `errors="replace"` so a stray non-UTF-8 byte never aborts
  the open. A payload containing a NUL in its first 8 KiB is **not** a
  transcript: raise `NonTranscriptExport` rather than absorb megabytes of
  binary as malformed lines. The guard is agent-agnostic and stays so
  regardless of any upstream fix (TB-10).
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
- **S34 — skips carry a typed reason, not stringified prose.** Every skipped
  session is a `SkipRecord(session_id, agent, reason: SkipReason, detail)`, where
  `SkipReason` is a `StrEnum` — `MISSING_SOURCE` / `UNKNOWN_SCHEMA` /
  `NON_TRANSCRIPT` / `DECODE_ERROR` / `EXPORT_FAILED`. The reason is decided where
  the evidence lives, not by regex on the report: `AgentsViewLoader.lines` raises a
  distinct `MissingSourceExport` (a flat `RuntimeError` sibling of
  `NonTranscriptExport`, **not** a subclass — a gone file and a binary file are
  different diagnoses) when export stderr matches `source file not found`; every
  other non-zero export stays a plain `RuntimeError` → `EXPORT_FAILED`.
  `classify_skip` maps each caught exception type to its `SkipReason` one frame
  after the raise, before the type information is lost; `skip_record_for` stamps it
  with the ref's identity; `tally_skips` answers "how many sessions have no parser?"
  as `tally[UNKNOWN_SCHEMA]` with no prose parsing. Mirrors `UsageProvenance` (S29):
  type the absence rather than stringify it. `detail` preserves the original message
  for `--verbose`/sidecar output but is never parsed to recover `reason` (TB-23).

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
  churn), (5) Summary. Each callout (except ToolSearch, which already
  carries a token figure) renders as `N of M calls (P%)` and names the
  top-offending tool when the count is non-zero; ties break alphabetically.
- **S15 — report provenance.** The report states the index source used,
  sessions scanned, tool calls joined, malformed-line count, whether
  subagents were included, any AgentsView fallback reason, and skipped
  roots (including per-session `NonTranscriptExport` / decode failures, each
  now carrying a typed `SkipReason` — S34); it notes `--since` is file-mtime
  based.
- **S35 — the Summary reconciles discovery.** `scanned` is not the corpus
  size and must never read as it. The Summary opens with
  `Sessions discovered: D / scanned: M / skipped: K`, where `D = M + K` is
  derived (`M = reducer.sessions_scanned`, `K = len(skips)`) — never a third
  count that could drift. When any session skipped, a `Skipped by reason:`
  histogram follows, one `<reason>: <count>` line per `SkipReason` (S34) present,
  sorted count-descending with ties broken on the reason's value, zero-count
  reasons omitted. The pre-TB-21 single `Skipped roots:` line — 1639 ids joined by
  `; ` — is gone; individual session ids move behind `--verbose`, which appends a
  `### Skipped sessions (detail)` subsection (`<id> [<agent>] <reason>: <detail>`,
  root-level skips shown as `(root)`). The empty-selection message likewise reports
  a typed tally `(skipped K: <reason>=<count>, …)` rather than joining every id. A
  report whose skip line its own author could not tally is the defect this closes
  (TB-21).
- **S36 — the Summary carries a corpus fingerprint.** The corpus is not stable
  between runs: claude-mem observer transcripts age out of a ~30-day sliding
  window *mid-scan*, so its tail deletes itself at roughly re-run cadence, and the
  live session appends calls while it is read. Two reports were therefore not
  diffable — a delta could not be attributed to a code change. The Summary now
  emits `Corpus fingerprint: <digest> (<N> sessions scanned)`, a `sha256` (16 hex)
  over a sorted set of per-session **signatures**, one per *scanned* session
  (`session_signature(id, call_count)`). The basis is the scanned set — the
  sessions that produced the numbers — not the discovered set, which could match
  while transcripts slid `scanned → skipped`. The signature folds both drift
  mechanisms: identity catches a vanished tail (an id leaves the set) and call
  count catches an append (transcripts are append-only, so count is an exact proxy
  for content growth). An id-only digest would match across an append and let a
  reader mis-attribute the delta to code — the one outcome the ticket forbids. The
  fingerprint is order-independent (sorted before hashing) so paging order never
  moves it, and the count travels alongside so a collision cannot hide a size
  change (TB-22).
- **S37 — `--freeze <manifest>` pins the corpus for reproducibility.** Absent the
  manifest, the first `--freeze` run discovers as usual and writes the discovered
  ref list once (`toolbench/freeze.py`, JSON, `SessionRef` round-tripped, an
  identity fingerprint over the discovered ids stored alongside); the Summary notes
  `Corpus frozen to: <path>`. When the manifest exists, the run **replays** it:
  live discovery is bypassed and the frozen refs are scanned directly, so the input
  set cannot drift. Refs that no longer load — a raw file gone or an
  `agentsview export` that reports `source file not found`, both raising the typed
  `MissingSourceExport` — are counted as `Replaying frozen corpus: <path>
  (<V> vanished since freeze)`, with their ids listed under `--verbose` via the S35
  skip detail. Over an unchanged corpus a replay is byte-identical (the fingerprint
  line included); when the tail has moved, the vanished count names the mechanism
  rather than letting the delta pass as code (TB-22).

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
  `--allow-seeded`. **Open defect (TB-17):** when both usage cells are
  populated, they are not yet comparable — the bash arm's required sentinel
  (and optional `description`) inflate bash `output_tokens` by ~15–20 tokens
  of instrumentation the tool arm cannot carry. Context-token columns remain
  the trustworthy ranking until TB-17 lands a stated correction or drops the
  usage pair.

## Metrics, quality, errors

- **S19 — metric roles.** Context cost = joined result-payload tokens
  (`chars/4`) is the primary ranking; the cache flag is caveat-only and
  never ranks tools; failure / slow / retry-churn feed inefficiency
  callouts only.
- **S20 — stdlib runtime, uv project.** The shipped `toolbench` package
  imports nothing third-party; the project is uv-managed (`pyproject.toml`
  + `uv.lock`, empty runtime deps, `dev` group `ruff`/`mypy`/`pytest`).
- **S21 — entry points.** Runnable as `uv run python -m toolbench.passive`
  and `… toolbench.probe`; tests via `uv run pytest -q` (S31).
- **S22 — strict gate.** `uv run ruff check .`, `uv run mypy --strict
  toolbench tests`, and the full pytest suite are green before any PR.
- **S23 — error handling.** Empty session selection → clear message,
  exit 0. Missing selected raw root → exit 1 for a strict source; but
  `--agent all --index-source auto` continues with other sources and
  reports skipped roots. Per-session parse failures (`OSError`,
  `RuntimeError` including `NonTranscriptExport`, and `UnicodeDecodeError`)
  demote that session into skipped roots and continue the corpus scan —
  one bad export must not abort the run.

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
  the `requestId`; there is no timestamp fallback (superseded by S30). An arm's usage is
  attributable only when that response emitted exactly one `tool_use` and no
  non-empty `text` / `thinking` / `redacted_thinking` block. Batching,
  prose, or reasoning in the arm turn keeps the match and the context-token
  columns, and blanks usage (`—`). Only a fresh session recovers the number
  (TB-16).
- **S31 — gate collects every test.** The documented and enforced fast-suite
  command is `uv run pytest -q`, not `uv run python -m unittest discover
  tests`. `unittest.TestLoader.discover` only finds `unittest.TestCase`
  methods; it is blind to module-level `test_*` functions, which pytest
  collects uniformly alongside `TestCase` methods. A test added as a bare
  module-level function cannot silently escape the gate (TB-19).

## Schema dispatch — `toolbench/adapters.py` + `toolbench/registry.py`

- **S27 — schema dispatch.** `detect_parser` sniffs up to 100 non-empty lines
  and returns the single parser whose `claims_line` matches. Two matches raise
  `AmbiguousSchema`; zero matches raise `UnknownSchema`. Both subclass
  `RuntimeError`, so `passive.main` demotes the session to `skipped_roots`.
  Hermes claims by source (`agent == "hermes" and path is None`) because it is a
  SQLite read with no lines; every other session is claimed by content, since
  schema is a property of the payload, not of the producer (TB-13).
- **S28 — no parser is the default.** An unrecognized transcript is never parsed
  by `ClaudeParser`, and never reported as an agent with zero tool calls. `cursor`
  lands in `skipped_roots` pending a parser of its own. `codex` is claimed by
  `CodexParser` (S33 / TB-12).

## Usage provenance — `toolbench/parsers.py` + `toolbench/probe.py`

- **S29 — producer provenance for usage.** Schema and producer are separate
  axes. A transcript claimed by the claude schema is routed by producer:
  `version == "hermes-agent"` selects `HermesTraceParser`, otherwise
  `ClaudeParser`. The two claim predicates partition, so `AmbiguousSchema`
  never fires between them. Every `ToolCall` carries a `UsageProvenance` of
  `PRESENT`, `ABSENT_BY_SCHEMA`, `ABSENT_BY_EXPORT`, or `ABSENT_UNEXPECTED`,
  stamped by its producer. The passive cache-hit flag renders `n/a` when no
  call in a bucket could be measured, `n/a*` when only some could, and `no`
  only when usage was available and zero hits were observed. Per S19 the flag
  remains caveat-only and never affects ranking (TB-18).
- **S30 — probe requires the billing unit.** `probe.py` groups turns solely by
  `requestId`, amending S26. It rejects `hermes-trace` input at dispatch, and
  `_turn_key` raises `NonIsolableTurns` on any entry lacking `requestId`.
  There is no timestamp fallback and no partial-corpus mode.
  `hermes sessions export --format trace` output is therefore valid input to
  `passive.py` and invalid input to `probe.py` (TB-18).
- **S32 — session-grain cache surfaced without per-call fabrication.**
  `parse_hermes_session` additionally reads `cache_read_tokens` off the
  session's own `sessions` row (hermes carries cache data at session grain,
  never per call — see S29) and stamps `ParseResult.session_cache_read_tokens`
  with the raw value: `None` when the column is SQL `NULL` (not measured), an
  `int` — including `0` — when the session was measured. This field is never
  attributed to an individual `ToolCall`, never folded into `UsageProvenance`,
  and never divided by `tool_call_count` to invent a per-call rate — that is
  the exact class of fabrication S29 exists to eliminate. `Reducer.absorb`
  folds it into two session-grain-only counters per agent
  (`AgentStats.sessions_with_cache_data`, `.sessions_with_cache_hit`),
  incremented once per session regardless of call count. The Agent Breakdown
  section (S14 §1) renders one caveat line per agent whose measured count is
  non-zero (`M of N sessions carry session-grain cache_read_tokens > 0 —
  not attributable to individual tool calls`); this does not add a sixth
  section and never touches the Tool Leaderboard's per-call `cache_assisted`
  column (S29's four-case render), so the two signals are never mixed in one
  column (S19). `HermesTraceParser` output never populates this field: the
  trace export drops the cache channel entirely, so there is no session-grain
  value there either (TB-20).
- **S33 — the codex schema is parsed, not skipped.** `CodexParser` claims a line
  whose top-level `type` is one of codex's record kinds (`session_meta`,
  `response_item`, `event_msg`, `turn_context`, `compacted`) and whose `payload`
  is an object. It joins **three** paired call shapes on `payload.call_id` — never
  `tool_use_id`. The shapes agree on the join key and on nothing else, so each
  declares its own input field, output field, and name source:

  | call | input field | output record | output field | name |
  |---|---|---|---|---|
  | `function_call` | `arguments` (JSON string) | `function_call_output` | `output` | `payload.name` |
  | `custom_tool_call` | `input` (string) | `custom_tool_call_output` | `output` | `payload.name` |
  | `tool_search_call` | `arguments` (object) | `tool_search_output` | `tools` | *none* → `ToolSearch` |

  Reading `arguments` for `custom_tool_call` would zero every `apply_patch`'s
  input size; requiring `payload.name` would skip `tool_search_call`, which has
  no name field, silently dropping it and understating the deferral tax (S19)
  that `Reducer` keys on the literal name `ToolSearch`.

  A session is identified by the first `session_meta`'s **`id`** — the rollout's
  own identity. Not `payload.session_id`, which is absent from older rollouts and
  names the *parent* thread in a subagent rollout; keying on it would stamp calls
  with an empty identifier and collapse subagent rollouts into their parents.
  `model` comes from the most recent preceding `turn_context`, since it appears on
  no call record and may change between turns.

  Every resulting `ToolCall` carries `usage=None` with
  `UsageProvenance.ABSENT_BY_SCHEMA`: codex reports tokens as per-turn
  `token_count` events, and a turn holds many calls, so no per-call attribution
  exists to read (S29). `error` is always `None` — codex encodes exit status in the
  output text and reports `status: completed` even for a failed tool, so no error
  flag is inferred from prose. Unmatched calls at EOF keep S6's `no_result`
  semantics. `SUBAGENT_TOOL_NAMES` includes codex's fan-out primitive
  `spawn_agent` (but not `wait_agent`, which awaits an already-spawned subagent),
  so the fan-out callout is no longer measured with its most relevant agent absent.

  `web_search_call` is **not** claimed: it carries no `call_id` and has no matching
  output record, so this parser's join key cannot reach it. It is a real tool call
  that codex reporting omits, tracked as TB-24 rather than papered over.

  The claim predicate is disjoint from `ClaudeParser`'s and `HermesTraceParser`'s,
  so `AmbiguousSchema` never fires between them (TB-12).
