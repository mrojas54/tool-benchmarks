# tool-benchmarks — SPEC

Numbered acceptance criteria with stable IDs. Derived from
`docs/2026-07-07-tool-benchmarks-design.md` (v2) and the v2 implementation
plan. Each ID is referenced by `EVALUATION.md` and by the BUILDPLAN tickets.

## Parser & records — `src/toolbench/transcript.py`

- **S1 — id-join.** `ClaudeParser.parse(lines, …)` joins each assistant
  `tool_use` block to its result by id. The join key is `message.content[].id`
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

## Multi-agent discovery — `src/toolbench/sources.py`

- **S7 — raw discovery.** `iter_session_files(root="~/.claude/projects",
  project=None, since=None)` yields Claude Code JSONL paths, filtered by
  owning-project-dir substring (`project in rel.parts[0]`, so
  `<project>/<session-uuid>/subagents/*.jsonl` still matches — TB-8; not merely
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
  `raw` uses the filesystem only. The fallback is not limited to what a single
  `--limit 1` health probe can see: a daemon that answers the probe and then
  breaks during the pagination that follows -- a nonzero exit, a hang
  (`AgentsViewTimeout`, TB-32), or a schema-invalid listing payload
  (`MalformedAgentsViewResponse` / `ValueError`: invalid JSON, non-object
  payload, `sessions` not a list, row missing required `id`/`agent`/`project`,
  non-empty `id`/`agent` — empty `project` is valid for projectless/global
  sessions — bad `next_cursor`/`total`) -- also degrades `auto` to raw,
  discarding whatever partial agentsview listing that attempt had collected and
  rescanning the corpus wholesale from the filesystem rather than splicing the
  two (TB-38; TB-22's identity/fingerprint precedent is why nothing is spliced).
  The `auto` health probe validates that same listing contract, so a zero-exit
  but schema-invalid `--limit 1` response falls back at the probe rather than
  entering pagination. A
  source that vanishes outright mid-discovery (`FileNotFoundError` — the
  binary itself disappears) keeps its narrower, pre-existing handling: a named
  `MISSING_SOURCE` skip and an unavailable census, no raw rescan, since a
  vanished binary is not evidence the raw root is any healthier. An explicit
  `--index-source agentsview` request is unaffected by any of this: any of those
  mid-listing failure modes there still raises, because a strict demand for
  AgentsView is not a request to be answered by something else.
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

## Passive analyzer — `src/toolbench/passive.py` (+ `reducer.py` / `report.py` / `freeze.py`)

Aggregation and markdown rendering live in `reducer.py` and `report.py`;
`freeze.py` owns the opt-in corpus pin. `passive.py` is the CLI + scan loop
and re-exports the public symbols historical imports expect.

- **S11 — incremental.** Aggregation streams per parsed session; **no full
  in-memory `list[ToolCall]`** for the corpus — only per-agent/per-tool
  reducers and report counters live globally (`Reducer` in `reducer.py`).
- **S12 — CLI.** Flags: `--agent`, `--all | --project`, `--since`,
  `--date-from`, `--date-to`, `--out`, `--limit`, `--exclude-subagents`,
  `--index-source`, `--verbose`, `--freeze`, `--run-manifest`, `--tickets`;
  default scope `--agent all --all`.
- **S13 — subagents.** Included by default; `--exclude-subagents` drops refs
  with `SessionRef.is_subagent` set at discovery. Raw discovery attributes
  project as the first path segment under the session root and sets the flag
  for `<project>/<session-uuid>/subagents/*.jsonl` — never by stamping `project="subagents"`
  or filtering on a path substring after the fact.
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
  (TB-21). That same empty-selection return already holds a full `AgentCensus` by
  the time it prints — discovery built it before the zero-match check ever runs —
  so it also appends `_sampling_notes`' rendering of it (unreached agents, an
  agent whose every sampled session was skipped, an uneven-sampling spread, an
  unenumerated residual — TB-33's per-agent disclosure, reused rather than
  reinvented) after the base message, never in place of it: a narrow `--since` or
  `--date-from`/`--date-to` window must not read as indistinguishable from a truly
  empty archive (TB-34).
- **S36 — the Summary carries a corpus fingerprint.** The corpus is not stable
  between runs: claude-mem observer transcripts age out of a ~30-day sliding
  window *mid-scan*, so its tail deletes itself at roughly re-run cadence, and the
  live session appends calls while it is read. Two reports were therefore not
  diffable — a delta could not be attributed to a code change. The Summary now
  emits `Corpus fingerprint: <digest> (<N> sessions scanned)`, a `sha256` (16 hex)
  over a sorted set of per-session **signatures**, one per *scanned* session
  (`session_signature(id, call_count, malformed, unjoinable)`). The basis is the
  scanned set — the sessions that produced the numbers — not the discovered set,
  which could match while transcripts slid `scanned → skipped`. The signature folds
  every drift mechanism: identity catches a vanished tail (an id leaves the set) and
  the call, malformed-line, **and** unjoinable-record counts catch an append
  (transcripts are append-only, so each is an exact proxy for content growth —
  including an append that lands as a malformed line, or an appended
  `web_search_call` that moves "Unjoinable tool records" alone, S38/TB-24). An
  id-only digest, or one folding only some of these counts, would match across an
  append while a rendered number moved and let a reader mis-attribute the delta to
  code — the one outcome the ticket forbids. The
  fingerprint is order-independent (sorted before hashing) so paging order never
  moves it, and the count travels alongside so a collision cannot hide a size
  change (TB-22).
- **S37 — `--freeze <manifest>` pins the corpus for reproducibility.** Absent the
  manifest, the first `--freeze` run discovers as usual and writes the discovered
  ref list once (`src/toolbench/freeze.py`, JSON, `SessionRef` round-tripped, an
  identity fingerprint over the discovered ids stored alongside); the Summary notes
  `Corpus frozen to: <path>`. When the manifest exists **and is a regular file**,
  the run **replays** it: live discovery is bypassed and the frozen refs are
  scanned directly, so the input set cannot drift. Replay existence is
  `Path.is_file()` (not bare `exists()`), so a directory at the freeze path is a
  fatal freeze error rather than a silent discover-and-overwrite. `read_manifest`
  raises typed `MalformedFreezeManifest` for OS read failure, non-UTF-8, invalid
  JSON, non-object roots, bad `refs`, or missing required fields; `passive`
  maps that (and write-time `OSError`) to `fatal freeze error: …` on stderr and
  exit 1 (S23 / PR #87) — same operator contract as a bad `--run-manifest`.
  Refs that no longer load — a raw file gone or an
  `agentsview export` that reports `source file not found`, both raising the typed
  `MissingSourceExport` — are counted as `Replaying frozen corpus: <path>
  (<V> vanished since freeze)`, with their ids listed under `--verbose` via the S35
  skip detail. Over an unchanged corpus a replay is byte-identical (the fingerprint
  line included); when the tail has moved, the vanished count names the mechanism
  rather than letting the delta pass as code (TB-22).
  - **TB-37 — manifest format v2 persists the freeze-time census.** `MANIFEST_VERSION`
    bumped `toolbench-freeze-1` -> `toolbench-freeze-2`. A freeze pins the REF LIST,
    not the archive it was drawn from, so TB-22/TB-33 shipped replay with a
    deliberately STATED absence — `unavailable_reason` naming that no denominator
    exists — rather than a silent zero (persisting one was a manifest FORMAT change out
    of TB-22's scope). v2 closes that gap: `write_manifest` accepts an optional
    `AgentCensus` (`totals`, `archive_total`; `residual` is a derived property and is
    never itself stored) and, when given one, writes it under an optional `census` key.
    `read_manifest` branches on **key presence, not the version string** — a v1
    manifest (no such key ever existed) and a v2 manifest deliberately written without
    one (e.g. the freeze run's own census attempt failed) both yield
    `CorpusManifest.census = None`, and `passive.py`'s replay branch degrades both to
    the same `unavailable_reason`, now naming the manifest's `version` specifically
    rather than "freezing" in general, so a future v3 gap is never mistaken for this
    one. A freeze-time census that was itself unavailable (`unavailable_reason` set at
    write time) round-trips and propagates that reason on replay rather than being
    laundered into the generic no-census text — attempted-and-failed is a different
    fact from never-attempted. New freezes also persist `census_includes_subagents`
    beside the census: the population filter (`not --exclude-subagents`) used to
    measure that denominator. Replay discloses historical fractions only when that
    filter is present **and** matches the replay's `--exclude-subagents` choice —
    otherwise it marks fractions unavailable (legacy v2 census without the key, or a
    mismatched replay) rather than pairing a filtered numerator with the wrong
    denominator (false 50% / impossible 200%). When a v2 manifest carries a REAL
    census whose population filter matches, replay renders real per-agent fractions
    instead of "unavailable" — but they are a **historical** denominator (the archive
    as measured at freeze time, not today's), and the report says so explicitly beside
    the fractions it qualifies (`frozen_census_note`, `render_report`), on the same
    "state it, don't imply it" footing TB-33 established: a v2 census must never read
    as current.
- **S38 — unjoinable tool records are counted and surfaced, not dropped.** A tool
  record a parser RECOGNIZES as a real call but structurally CANNOT join — no join
  key and no output record — is neither a joined call nor a malformed line. It is
  counted in `ParseResult.unjoinable` (record kind → count) and folded in the
  `Reducer` by `(agent, kind)`. `CodexParser`'s `web_search_call` is the case: it
  carries no `call_id` and has no `web_search_output`, so codex's own reporting
  omits it (~4% of codex tool calls, web search invisible). It is **not** emitted as
  a joined call — corpus call counts and every inefficiency ratio (S19) are
  unchanged — nor as a `no_result` orphan (which would fabricate a call and inflate
  codex's count). Instead the Summary, when the total is non-zero, renders
  `Unjoinable tool records (seen, not joined): <T>` followed by one
  `<agent>/<kind>: <count>` line per pair, sorted, and the line is absent entirely
  when there is nothing to report. The count is a rendered number, so
  `session_signature` folds it (S36): an appended `web_search_call` moves the
  fingerprint even though the call and malformed counts are unchanged. This is the
  no-silent-zeros stance applied to a record that cannot be joined, so the gap is
  named rather than absent (TB-24; the home is TB-21's Summary reconciliation).
- **S39 — session-grain cache-token sums for Claude, read and creation.**
  `session_cache_read_tokens` (S32) is populated for Claude sessions too — summed
  over the session's per-message `usage.cache_read_input_tokens`, not only from the
  hermes `sessions` row — and a parallel `session_cache_creation_tokens` is added,
  summed from `cache_creation_input_tokens`. Both are `None` when no message in the
  session carried `usage` (unmeasured, SQL NULL) and an int — including `0` — once
  at least one did (measured). This promotes the `_is_cache_hit` boolean (S19, a
  caveat-only per-message cache signal) to a session total the token-cost benchmark
  can diff, without touching ranking: the sums render as a Summary caveat line
  (read + creation), never folded into an inefficiency ratio. The hermes path is
  unchanged (still the `sessions` row, no double-count), and the TB-25 survival
  invariant extends — `_apply_date_range` reconstructs via `replace()`, so the new
  field passes `--date-from`/`--date-to` intact. Read and creation are surfaced
  together deliberately: a prefix-sharing change (per-ticket context extracts vs a
  shared contract) trades one for the other, so a read delta read alone misleads
  (TB-26; foundation for the per-run `--run-manifest` grouping, TB-27).
- **S40 — per-run cache-token grouping, entry-grain.**
  A run's cache cost is the sum of `usage` on every transcript **entry** whose
  `gitBranch` is in the run's branch set, supplied by a JSON run-manifest
  (`--run-manifest`, format per the S37 freeze precedent) that the orchestrator
  emits **at dispatch**. Attribution is per-entry, not per-session: a session is
  not owned by one run (29/158 straddle >1 branch), and delegators do not always
  run in worktrees (one is logged as having "Ran in ROOT checkout"), so neither
  branch nor `cwd` partitions sessions cleanly. 1834/1834 usage-bearing entries
  carry `gitBranch` — but **presence is not attributability** (TB-28): a detached
  checkout stamps the literal `"HEAD"`, which is the absence of a branch and can
  never match a manifest, so such usage is neither foldable into a run nor
  disclaimable from it (a detached delegator and unrelated detached work are
  indistinguishable). It is therefore booked to a separate `detached_*` bucket and
  **named** in the run section — never folded into the total (which would fabricate
  an attribution) and never dropped (which silently undercounts the run, the
  project's signature failure). `ClaudeParser` buckets into
  `usage_by_branch` in its existing pass (no second interpreter, CQ 1.2), additive
  beside the S39 session totals, so `session total == sum of buckets` is an
  invariant. `unattributed` is the usage on non-run branches **within candidate
  sessions** (those with >=1 entry on a run branch) — the straddle spillover;
  scoped corpus-wide it would be dominated by unrelated `main` work. A manifest
  branch matching zero entries is reported, never a silent zero (S23/S38). The run
  section renders read + creation together, normalized per ticket, as a Summary
  caveat — never a ranking column (S19). `.lattice/orchestration/agents.md` cannot
  serve as the manifest: it discards its Branch column on run completion (TB-27;
  builds on the session-grain sums of S39/TB-26).
- **S41 — per-agent sampling disclosure.** `--limit` truncates discovery in
  **recency order across the whole archive**, not per agent, so each agent's row
  rests on a different fraction of its own history and an agent whose work is all
  older than the window can vanish at `sessions == 0`. Discovery therefore gathers
  an `AgentCensus` (`src/toolbench/sources.py`) under the *same* filters as the scan
  (including `--exclude-subagents`), and the Agent Breakdown carries a `sampled`
  cell per agent — numerator, census denominator, and fraction — with agents
  present in the archive but never reached still given a row (`sessions == 0` reads
  as looked-and-found-none, not never-looked). When the sampling is uneven,
  `_sampling_notes` / `_apportionment` (`src/toolbench/report.py`) name the cause from
  *observed signals only* — `SkipRecord`s for attrition, `limit_truncated` for
  truncation — and apportion the per-agent remainder (`total - sampled`) between
  the two rather than merely asserting both happened (TB-35): a limit that was
  passed but never bit is not truncation, a negative remainder is flagged as
  census/scan drift instead of laundered, and `limit_truncated is None` (the source
  could not say) is stated as its own third answer. Cross-agent ratios are
  trustworthy only when no uneven-sampling line prints. A `--freeze` replay has no
  *live* archive to census: when the v2 manifest carries a freeze-time census whose
  `census_includes_subagents` filter matches the replay (TB-37 / S37), replay
  discloses those fractions with an explicit historical-denominator caveat; when
  the manifest has no usable census (v1, a v2 write whose freeze-time census
  itself failed, a legacy v2 census without population-filter metadata, or a
  replay that flipped `--exclude-subagents`) — or when a live census failed at
  discovery — the report carries `unavailable_reason` and marks fractions
  unavailable rather than inventing a denominator. The empty-selection path
  reuses the same census disclosure so a narrow window is not mistaken for an
  empty archive (TB-34 / S35).

## Active probes — `src/toolbench/probe.py` + `protocols/active-probes.md`

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
  imports nothing third-party by default; the project is uv-managed
  (`pyproject.toml` + `uv.lock`, empty runtime deps, optional `tracing` extra
  that pulls in `lmnr` for opt-in Laminar CLI observability — not required for
  the gate or hermetic suite). The `dev` group holds `ruff`/`mypy`/`pytest`
  plus optional `logfire` for parallel-run tooling (also not imported by the
  shipped package). Real console processes may wrap subcommands in
  `toolbench.tracing.run_traced` when the extra and project key are present;
  programmatic `main([...])` calls and `worktrees --hook` stay untraced.
- **S21 — entry points.** Runnable as `uv run toolbench passive` /
  `uv run toolbench probe` / `uv run toolbench worktrees` (unified console
  script via `cli.py`) or `uv run python -m toolbench.passive` /
  `… toolbench.probe` / `… toolbench.worktrees`; tests via
  `uv run pytest -q` (S31). Run-grain grouping (`--run-manifest` / `--tickets`)
  is a dimension on `toolbench.passive` itself (S40) — the analyzer owns run
  grain, not a fourth analyzer CLI. Worktree reclaim inventory is a separate
  subcommand (S42), not a passive flag.
- **S22 — strict gate.** `uv run ruff check .`,
  `uv run python -m toolbench.complexity_gate --base <git-ref>`,
  `uv run mypy --strict src/toolbench tests`, and the full pytest suite are
  green before any PR. The complexity gate (`src/toolbench/complexity_gate.py`)
  compares Ruff `C901` scores for changed `src/` and `tests/` Python files
  against a Git baseline by `(path, qualified name)`. Threshold defaults to 10
  (`[tool.ruff.lint.mccabe] max-complexity`): a new function above 10, a
  function crossing 10, or a legacy hotspot that increases all fail; an
  increase of ≥2 that stays ≤10 is a warning only. `# noqa: C901` does not
  hide a symbol (`--ignore-noqa`). CI uses the PR base SHA (or the pre-push
  SHA) with `fetch-depth: 0`. Renaming/moving a function changes its identity,
  so a moved hotspot above 10 is treated as new.
- **S23 — error handling.** Empty session selection → clear message,
  exit 0. Missing selected raw root → exit 1 for a strict source; but
  `--agent all --index-source auto` continues with other sources and
  reports skipped roots. Per-session parse failures (`OSError`,
  `RuntimeError` including `NonTranscriptExport`, and `UnicodeDecodeError`)
  demote that session into skipped roots and continue the corpus scan —
  one bad export must not abort the run. Bad *manifest* paths are hard stops
  (exit 1 with a clear stderr message, no traceback): a malformed /
  non-UTF-8 / unreadable `--freeze` or `--run-manifest` file, a `--freeze`
  path that exists but is not a regular file (e.g. a directory), or an
  `OSError` while writing a new freeze manifest (`MalformedFreezeManifest`
  from `freeze.py`; same shape as `MalformedRunManifest`).

## Worktree reclaim — `src/toolbench/worktrees.py`

- **S42 — linked-worktree reclaim reporter.** `toolbench worktrees` classifies
  every **linked** worktree of the current clone (main checkout excluded and
  labelled never-a-candidate). Verdict precedence is
  `LOCKED > DIRTY > UNIQUE-WORK > CLAIMED > SAFE`. Ownership (`CLAIMED`) is a
  live remote-tracking upstream (`%(upstream)` + `rev-parse --verify`); it
  never reads `%(upstream:track)` and never expires. `reclaimable()` returns
  only `SAFE` trees idle ≥ `IDLE_DAYS` (7); unknown idle age fails the
  threshold. The command **prints only** — it never removes a tree, deletes a
  branch, or touches a ref. `--reclaimable-only` prints nothing when empty.
  `--hook` (mutually exclusive) is SessionStart mode: speak only on
  `startup`/`resume`, emit one context line or silence, always exit 0, swallow
  every failure. Registered in tracked `.claude/settings.json`. Terminal path
  raises `WorktreeProbeFailed` on verdict-bearing git failures rather than
  guessing SAFE/DIRTY.

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

## Schema dispatch — `src/toolbench/adapters.py` + `src/toolbench/registry.py`

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

## Usage provenance — `src/toolbench/parsers.py` + `src/toolbench/probe.py`

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
  value there either (TB-20). Because the value is session-grain and not a
  per-call quantity, `--date-from`/`--date-to` filtering (S12) narrows only
  `calls` and leaves `session_cache_read_tokens` intact — even a session whose
  every call falls outside the range still contributes its cache stat, since the
  session was measured (TB-25). `_apply_date_range` reconstructs the
  `ParseResult` with `dataclasses.replace(result, calls=kept)` so no
  session-grain field can be silently dropped by a hand-listed reconstruction.
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

  `web_search_call` is **not** joined as a call: it carries no `call_id` and has no
  matching output record, so this parser's join key cannot reach it. It is a real
  tool call that codex reporting omits, so it is counted in `ParseResult.unjoinable`
  and surfaced in the Summary (S38) rather than papered over (TB-24).

  The claim predicate is disjoint from `ClaudeParser`'s and `HermesTraceParser`'s,
  so `AmbiguousSchema` never fires between them (TB-12).
