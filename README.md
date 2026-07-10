# tool-benchmarks

A re-runnable harness that analyzes **tooling inefficiencies across agent
systems** — Claude Code, Codex, Hermes, and other
[AgentsView](https://github.com/)-supported runtimes — from their on-disk
session transcripts. The goal is evidence for where agent work wastes
context, time, retries, or tool calls, emitted as a single markdown report.

Builds on the native-tool-vs-Bash benchmarking methodology from claude-mem
observation #8376.

## What it measures

1. **Cross-agent tool cost** — which tools, agents, projects, and workflows
   dump the most context back into sessions.
2. **Tooling inefficiency patterns** — repeated failed calls, slow tools,
   edit churn, retry loops, context pressure, subagent fan-out.
3. **Deferral / discovery tax** — what deferred-tool loading and searching
   (e.g. `ToolSearch`) costs across Claude Code, Codex, and Hermes.
4. **Controlled tool-vs-shell probes** — for comparable local tasks, when
   native tools (Grep/Glob/Read) are cheaper or more reliable than shell
   commands.

The primary metric is **context cost** = joined tool-result payload tokens
(`chars / 4`). Cache flags are caveat-only and never rank tools; failure /
slow / retry-churn feed the inefficiency callouts only.

## Scope guards (non-goals)

- **Markdown only** — no HTML report (the `session-report` skill owns that).
- **No live token-API calls** — all numbers derive from on-disk transcripts.
- **Read-only** — never mutates transcripts or the probe corpus's source
  projects.
- **Python standard library only** — no third-party runtime dependencies, so
  the harness runs anywhere `python3` exists.
- **No web-chat benchmarking** — local/agentic surfaces with inspectable
  sessions only.

## Architecture

```
raw roots + AgentsView exports
          │
   loaders (sources.py)  ── acquisition: bytes → lines
          │
   parsers (parsers.py)  ── interpretation: lines → ToolCall
          │
   adapters (adapters.py + registry.py)  ── SessionRef → ParseResult
          │
     ┌────┴─────┐
 passive.py   probe.py
     └────┬─────┘
   reports/YYYY-MM-DD-tool-usage.md
```

### Three layers, one seam (TB-13)

Acquisition and interpretation are orthogonal, so they are separate ABCs:

- A **`SessionLoader`** turns a `SessionRef` into lines. It owns the NUL sniff,
  which therefore runs *before* schema detection — a SQLite dump has no first
  JSON line to detect.
- A **`TranscriptParser`** interprets already-acquired lines. It never opens a
  file and never decides which schema it is looking at.
- A **`SessionAdapter`** composes the two. `registry.pick_adapter` walks an
  ordered list of `claims(ref)` predicates and returns the first match.

**Hermes is keyed on source; everything else is keyed on content.** Hermes is a
SQLite read, not a transcript, so it claims by `agent == "hermes" and path is
None`. Every other session is content-sniffed over a bounded 100-line window,
because schema is a property of the payload, not of the producer: `cowork` emits
Claude's exact schema and parses correctly with zero registry entries of its own.

**No parser is the default.** An unrecognized transcript raises `UnknownSchema`
(a `RuntimeError`, so `passive.main`'s existing per-session guard demotes it to
`skipped_roots`). Previously such a session fell through to the Claude parser,
matched nothing, and reported a healthy zero. `codex` and `cursor` land in
`skipped_roots` today, pending a `CodexParser` in TB-12.

- **`transcript.py`** — the schema-neutral records: `ToolCall`, `ParseResult`,
  and `result_len`, which normalizes a payload to a character length.
  `parse_session` remains as a compat shim over `ClaudeParser`. Stray
  non-UTF-8 bytes decode with `errors="replace"` so one bad byte never aborts
  the session (TB-10).
- **`parsers.py`** — one class per schema. `ClaudeParser` joins each assistant
  `tool_use` block to its result by id. Malformed lines are counted and
  skipped, never fatal.
- **`adapters.py`** — `detect_parser`, `UnknownSchema`, `AmbiguousSchema`, and
  `ComposedAdapter` (the terminal fallback).
- **`registry.py`** — the ordered adapter list and `pick_adapter`. Exists to
  break the `hermes.py` ↔ `adapters.py` import cycle. Adding an agent means
  adding an entry here, never editing a dispatcher.
- **`sources.py`** — multi-agent discovery plus the loaders. Either scans raw
  local transcript roots or pages the AgentsView CLI (`--index-source auto |
  agentsview | raw`). `auto` tries AgentsView first and falls back to raw
  scanning, recording the reason. Exports that are not JSONL (e.g. a SQLite
  dump with a NUL in the header) raise `NonTranscriptExport` and are skipped
  by name (TB-10).
- **`hermes.py`** — direct read-only SQLite adapter for Hermes sessions
  (TB-11). Discovery still comes from AgentsView; only the read is redirected
  because `session export` returns the whole default-profile database.
- **`passive.py`** — streams and aggregates **incrementally** (per-agent /
  per-tool reducers only, never a whole-corpus `list[ToolCall]`), then emits
  a five-section report: agent breakdown, tool leaderboard, model breakdown,
  inefficiency callouts, summary.
- **`probe.py`** — scores matched tool-vs-Bash probe pairs from a dedicated
  session JSONL and emits a context-token + usage comparison table under
  `reports/`. Tool arms match structurally (name + corpus target); bash arms
  match by sentinel. Usage is attributed only when the API response is
  isolable (one `tool_use`, no prose/reasoning — S26).

## Probe corpus

Five files are vendored under [`tools/`](tools/) — a log-spaced size spread
(~121 → ~2,242 lines) so the tool-vs-Bash comparison shows how context cost
scales with target size:

| File | Lines |
|------|-------|
| `regex_check.py` | 121 |
| `mcp.py` | 352 |
| `monitor.py` | 768 |
| `llm_extraction.py` | 1,332 |
| `code_analysis.py` | 2,242 |

They are committed so probes re-run from a clean checkout with no external
absolute paths. Probe *output* lands in `reports/`, kept separate from these
inputs.

## Status

**Implemented.** `toolbench/` ships all of tickets **T1–T6** in
[`BUILDPLAN.md`](BUILDPLAN.md): the scaffold, the transcript parser, the
multi-agent source layer, the passive analyzer, and the active probes.
Post-merge hardening covers **TB-8** (subagent `--project` filter), **TB-9**
(callout denominators), **TB-10** (non-UTF-8 / non-transcript exports),
**TB-11** (Hermes SQLite direct read — discovery still via AgentsView),
probe isolability (**S26** / TB-14–16), and schema dispatch (**S27–S28** /
TB-13). The strict gate (`ruff`, `mypy --strict`, `unittest`) is green —
**176** tests collected by `unittest discover` (1 skipped when the live
hermes archive is absent). **37** additional module-level `test_*`
functions (TB-13 seam coverage) are only collected by `pytest` — **213**
total — and are silently skipped by the documented gate today (TB-19).

Source-of-truth documents:

- [`SPEC.md`](SPEC.md) — 28 numbered acceptance criteria (S1–S28).
- [`EVALUATION.md`](EVALUATION.md) — verification map for every criterion.
- [`BUILDPLAN.md`](BUILDPLAN.md) — decided architecture and the T1–T6 tickets.
- [`docs/2026-07-07-tool-benchmarks-design.md`](docs/2026-07-07-tool-benchmarks-design.md)
  — full v2 design spec.
- [`protocols/active-probes.md`](protocols/active-probes.md) — probe corpus,
  arm matching (S17), isolability (S26), and the seeded `#8376` baseline table.
- [`protocols/probe-run-sheet.md`](protocols/probe-run-sheet.md) — executable
  ten-turn operator run sheet for scoring a fresh probe session.

## Agents / targets

Three source adapters. The first two are selected per-session by
`--index-source`; the third is selected by agent.

- **Claude Code raw transcripts** — scans on-disk JSONL session files
  directly under a root (default `~/.claude/projects`).
- **AgentsView** — pages the `agentsview` CLI for any AgentsView-registered
  runtime (Claude Code, Codex, Hermes, …), yielding one `SessionRef` per
  session with cursor-based pagination.
- **Hermes SQLite** — reads hermes sessions straight from `~/.hermes`
  (`$HERMES_HOME` overrides). `agentsview session export` returns `rc=0` and
  streams the whole 37 MB default-profile database for every hermes session
  instead of that session's transcript, so hermes contributed zero tool calls
  until this adapter landed (TB-11).

Hermes **discovery** still comes from AgentsView; only the read is redirected.
The corpus is *defined* as what `agentsview session list` returns, and every
agent is sampled through that one path. Enumerating the hermes archive here
would redefine the corpus for a single agent and skew every cross-agent rate.

**Known limitation.** Hermes is under-sampled. `agentsview session list --agent
hermes` reports 89 sessions while `agentsview stats --agent hermes` reports 789
from the same archive — one binary, two subsystems, an 8.9× disagreement. That
is an upstream defect
([kenn-io/agentsview#1048](https://github.com/kenn-io/agentsview/issues/1048)),
not a curation to work around by forking discovery into one adapter. The export
bug this adapter exists for is
[#1047](https://github.com/kenn-io/agentsview/issues/1047).

## Usage

The project is [uv](https://docs.astral.sh/uv/)-managed (`pyproject.toml` +
`uv.lock`, empty runtime deps, `dev` group `ruff`/`mypy`/`pytest`).

```sh
# Passive analyzer — default scope is every agent, every project
uv run python -m toolbench.passive --agent all --all

# Scope by project / time / index source
uv run python -m toolbench.passive --project my-repo --since 2026-06-01
uv run python -m toolbench.passive --all --index-source agentsview
uv run python -m toolbench.passive --all --date-from 2026-06-01 --date-to 2026-06-30
uv run python -m toolbench.passive --all --exclude-subagents --out reports/2026-07-08-tool-usage.md

# Active tool-vs-Bash probes. Score a dedicated probe session; without
# --session every arm is seeded and the report is refused (SeededReportError).
# Operator run sheet: protocols/probe-run-sheet.md (ten arms, ten turns).
uv run python -m toolbench.probe --session /path/to/probe-session.jsonl --out reports/active-probe-comparison.md
uv run python -m toolbench.probe --allow-seeded   # baseline table only; measures nothing

# Tests
uv run python -m unittest discover tests
```

### Probe scoring pitfalls

- **Fresh session only.** Mentions of sentinels, the run sheet, or
  `toolbench/probe.py` are discarded as contamination (`MENTION_MARKERS`).
- **One tool call per API response.** Usage is keyed by `requestId` (S26).
  Batching, prose, or reasoning in an arm turn blanks the usage column (`—`)
  while keeping real context tokens — it does **not** re-seed the cell.
- **Usage columns are not yet comparable (TB-17).** `output_tokens` bills
  the whole emitted `tool_use` block. The bash arm must carry a sentinel
  comment (and often a `description`) that the tool arm cannot carry, so
  bash usage is inflated by ~15–20 tokens of instrumentation — enough to
  swamp the measured gap. Trust the context-token columns; do not conclude
  "MCP is cheaper on output tokens" from the usage pair until TB-17 lands
  a stated correction (or drops those columns).
- **Turn 0 before arms.** Confirm serena has an active project with a
  non-corpus target (`pyproject.toml`) so a failed arm call is not scored as
  a successful match.

### `--index-source` policy

- `auto` (default) — tries AgentsView first; on failure, falls back to
  scanning the raw root directly and records the fallback reason in the
  report's Summary section.
- `agentsview` — AgentsView only; a source error is fatal.
- `raw` — raw local transcript roots only; a source error is fatal.

`--agent` filters AgentsView listing only. Under `--index-source raw` the
discovery root is Claude Code sessions, so `--agent` is a no-op there.

`--project` matches the **owning project directory** under the raw root
(first path segment after the root), not `path.parent.name`. Nested
subagent transcripts at `<project>/subagents/*.jsonl` therefore survive
`--project` and are only dropped when you pass `--exclude-subagents`.

The fast test suite is hermetic — it fakes the `agentsview` CLI, points
`$HERMES_HOME` at a fixture database, and never touches `~/.claude` or
`~/.hermes`, so the inner loop never depends on a live daemon. One test in
`tests/test_hermes.py` reads the real hermes archive to pin the schema
compatibility envelope, and skips when that archive is absent.

Hermes databases are always opened `file:…?mode=ro`. A running hermes owns
those files; the adapter never writes to them.

### Reading the report

Inefficiency callouts are written as `N of M calls (P%)` and name the
worst tool when the count is non-zero, for example:

```text
- Failures: 147 of 997 calls (14.7%); top: Bash (109)
- ToolSearch/deferral tax: 12 of 997 calls (1.2%), 3400 tokens
```

Ties for "top" break alphabetically so the report stays deterministic. A
zero count omits the top-offender clause.

### Troubleshooting / common pitfalls

| Symptom | Likely cause | What to do |
|---|---|---|
| Run dies with `UnicodeDecodeError` | Pre-TB-10 behavior, or a custom strict-decode runner | In-tree readers use `errors="replace"`. One bad session should land in **Skipped roots**, not abort the corpus. |
| Summary shows `Skipped roots: … non-transcript payload` for a non-Hermes session | AgentsView `session export` returned binary with returncode 0 | Expected for off-contract exports; the NUL sniff rejects them before parse (TB-10). Hermes sessions should not hit this path — they route through `hermes.py`. |
| `--project X` silently omits every subagent | Pre-TB-8 filter matched `path.parent.name` (`subagents`) | Current code matches the owning project dir. Re-run on current `main`. |
| Callouts are bare integers (`Failures: 865`) | Pre-TB-9 report formatting | Current callouts include denominators and a top offender. |
| `--agent hermes` yields far fewer sessions than expected | AgentsView `session list` under-counts Hermes vs `stats` (~89 vs ~789) | Known upstream limit ([#1048](https://github.com/kenn-io/agentsview/issues/1048)); discovery is intentionally not forked into the Hermes adapter (S9b). |
| Hermes session skipped / archive not found | `$HERMES_HOME` / `~/.hermes` missing, or session only in an unread profile DB | Confirm `HERMES_HOME`; skipped roots name the session. Profile DBs under `profiles/*/state.db` are searched. |
| `Malformed lines` explodes into the hundreds of thousands | Binary export absorbed as text (would happen without the NUL sniff) | Should not occur on current code — binary payloads are rejected before parse. |
| Empty selection message | No sessions matched filters, or all matched sessions were skipped | Check `--project` / `--since` / `--date-*`, and the skipped-roots suffix on the message. |
| `toolbench.probe` without `--session` refuses to write | Seeded-only report is blocked (`SeededReportError`) | Pass `--session PATH`, or `--allow-seeded` for the baseline table only. |
| Probe usage column shows `—` but context tokens are real | Arm matched, but the API response was not isolable (prose, thinking, or batched `tool_use` — S26) | Re-run from [`protocols/probe-run-sheet.md`](protocols/probe-run-sheet.md); one tool call per turn, no surrounding prose. |
| Bash usage looks ~15–20 tokens higher than the tool arm | Sentinel + optional Bash `description` are billed into bash `output_tokens` only (TB-17) | Expected until TB-17. Compare context-token columns; treat usage as non-comparable. |
| `codex` / `cursor` sessions appear only in Skipped roots | No parser claims their schema yet (`UnknownSchema`, S28) | Expected until TB-12 lands a `CodexParser`. They must not appear as healthy zero-call agents. |
| `unittest discover` reports ~176 tests; `pytest` reports ~213 | 37 module-level `test_*` functions are invisible to `unittest.TestLoader.discover` | Known gate gap (TB-19). Until that lands, run `uv run pytest -q` when you need the full seam suite. |

## Quality gate

Before any PR: `uv run ruff check .`, `uv run mypy --strict toolbench tests`,
and the full unittest suite must be green.
