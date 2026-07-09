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
  `parse_session` remains as a compat shim over `ClaudeParser`.
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
  scanning, recording the reason.
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
multi-agent source layer, the passive analyzer, and the active probes. The
strict gate (`ruff`, `mypy --strict`, `unittest`) is green — 213 tests
passing (1 skipped when the live hermes archive is absent).

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
- **Turn 0 before arms.** Confirm serena has an active project with a
  non-corpus target (`pyproject.toml`) so a failed arm call is not scored as
  a successful match.

### `--index-source` policy

- `auto` (default) — tries AgentsView first; on failure, falls back to
  scanning the raw root directly and records the fallback reason in the
  report's Summary section.
- `agentsview` — AgentsView only; a source error is fatal.
- `raw` — raw local transcript roots only; a source error is fatal.

The fast test suite is hermetic — it fakes the `agentsview` CLI, points
`$HERMES_HOME` at a fixture database, and never touches `~/.claude` or
`~/.hermes`, so the inner loop never depends on a live daemon. One test in
`tests/test_hermes.py` reads the real hermes archive to pin the schema
compatibility envelope, and skips when that archive is absent.

Hermes databases are always opened `file:…?mode=ro`. A running hermes owns
those files; the adapter never writes to them.

## Quality gate

Before any PR: `uv run ruff check .`, `uv run mypy --strict toolbench tests`,
and the full unittest suite must be green.
