# tool-benchmarks

A re-runnable harness that analyzes **tooling inefficiencies across agent
systems** — Claude Code, Codex, Hermes, and other
[AgentsView](https://github.com/kenn-io/agentsview)-supported runtimes — from their on-disk
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
- **Python standard library by default** — normal runs have no third-party
  runtime dependencies. The opt-in `tracing` extra adds Laminar observability
  without changing the default install.
- **No web-chat benchmarking** — local/agentic surfaces with inspectable
  sessions only.

## Architecture

```
raw roots + AgentsView exports
          │
   loaders (sources.py)  ── acquisition: bytes → lines
          │
   parsers (parsers.py)  ── interpretation: lines → ToolCall / ParseResult
          │
   adapters (adapters.py + registry.py)  ── SessionRef → ParseResult
          │
     ┌────┴──────────────────┬────────────────────┬──────────────────┐
 passive.py (CLI / scan)   probe.py          complex.py +       worktrees.py
     │                         │             shell_safety.py +   (reclaim
 reducer.py → report.py        │             complex_runner.py   inventory;
 freeze.py (opt-in pin)        │             (library only)      prints only)
 run_manifest.py (S40)         │
     └──────────┬──────────────┴────────────────────┴──────────────────┘
          reports/*.md   (complex profile is rendered in-process; no CLI yet)
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

**Hermes SQLite is keyed on source; everything else is keyed on content.**
Hermes archive sessions claim by `agent == "hermes" and path is None` — a
SQLite read with no lines. Every other session is content-sniffed over a
bounded 100-line window, because schema is a property of the payload, not of
the producer: `cowork` emits Claude's exact schema and parses with zero
registry entries of its own. A `hermes sessions export --format trace` file
is also content-sniffed: it speaks Claude's shape but declares
`version == "hermes-agent"`, so `HermesTraceParser` claims it (S29) rather
than letting `ClaudeParser` swallow a usage-less export as a measured zero.

**No parser is the default.** An unrecognized transcript raises `UnknownSchema`
(a `RuntimeError`, so `passive.main`'s existing per-session guard demotes it to
`skipped_roots`). Previously such a session fell through to the Claude parser,
matched nothing, and reported a healthy zero. `codex` is now claimed by
`CodexParser`, which joins its three paired `response_item` shapes —
`function_call`, `custom_tool_call`, and `tool_search_call` — on `payload.call_id`
(S33 / TB-12). `cursor` still lands in `skipped_roots`, pending a parser of its own.
codex's `web_search_call` has no `call_id` and no output record, so it is not
joined as a call; instead it is counted in `ParseResult.unjoinable` and named in the
Summary (`Unjoinable tool records`), so codex's ~4% web-search undercount is surfaced
rather than silently absent (S38 / TB-24).

- **`transcript.py`** — the schema-neutral records: `ToolCall` (with
  `UsageProvenance` and parse-time inefficiency tags), `ParseResult`
  (optional session-grain cache sums, `unjoinable`, optional `turns`), and
  `result_len`. `JsonLines` is the shared JSONL reader (blanks skipped;
  undecodable and non-object lines counted into `malformed` — PR #107).
  Path-based `parse_session` is gone; open lines (with
  `errors="replace"` for TB-10) and call `ClaudeParser.parse` or
  `pick_adapter(ref).parse(ref)`.
- **`parsers.py`** — one class per schema. `ClaudeParser` joins each assistant
  `tool_use` block to its result by id, stamps inefficiency tags at emit
  (CQ 3.1), and sums session-grain cache read/creation (S39). Usage and turn
  accounting live in module-level `_account_usage` / `_track_turn` (local
  `_UsageTally`, no instance state on `parse`). Optional `keep_raw_input` /
  `track_turns` (CQ 7.1) let probe reuse this pass instead of a second
  Claude-shaped walker. `HermesTraceParser` subclasses it for the
  claude-shaped hermes trace export and stamps every call `ABSENT_BY_EXPORT`
  (S29). Malformed lines are counted and skipped, never fatal.
- **`adapters.py`** — `detect_parser`, `UnknownSchema`, `AmbiguousSchema`, and
  `ComposedAdapter` (the terminal fallback). `PARSERS` currently holds
  `ClaudeParser`, `HermesTraceParser`, and `CodexParser`; Claude and HermesTrace
  partition on `version`, Codex on top-level `type`.
- **`registry.py`** — the ordered adapter list and `pick_adapter`. Exists to
  break the `hermes.py` ↔ `adapters.py` import cycle. Adding an agent means
  adding an entry here, never editing a dispatcher.
- **`cli.py`** — unified console entry (`toolbench passive …` /
  `toolbench probe …` / `toolbench worktrees …`). Dispatches remaining argv
  verbatim to the sub-CLIs; imports are lazy per subcommand so a broken complex
  fixture cannot break `passive`, `worktrees`, or `--help`. Real console
  processes (`argv is None`) wrap the subcommand in `tracing.run_traced` only
  when `TOOLBENCH_TRACING=1`; programmatic `main([...])` calls stay untraced,
  and `worktrees --hook` is always excluded (SessionStart must stay silent /
  failure-tolerant).
- **`observability/`** + **`tracing.py`** — opt-in Laminar CLI observability
  (`tracing` extra / `lmnr`, gated by `TOOLBENCH_TRACING=1`). `setup_tracing`
  initializes best-effort (returns `False` when the SDK or project key is
  absent); `run_traced` records a `toolbench.cli` root span with command tags
  and exit code, never argv, paths, prompts, or report contents. See
  [Optional Laminar tracing](#optional-laminar-tracing).
- **`complexity_gate.py`** — regression-aware cyclomatic-complexity gate
  (S22 / PR #95). Compares Ruff `C901` for changed `src/` and `tests/` Python
  files against a Git `--base` by `(path, qualified name)`. Not a console
  subcommand — invoke as `uv run python -m toolbench.complexity_gate`. See
  [Quality gate](#quality-gate).
- **`worktrees.py`** — linked git worktree inventory with a reclaim verdict per
  tree (S42). Reports only — never removes a tree, deletes a branch, or touches
  a ref. See [Worktree reclaim reporter](#worktree-reclaim-reporter).
- **`sources.py`** — multi-agent discovery plus the loaders. Either scans raw
  local transcript roots or pages the AgentsView CLI (`--index-source auto |
  agentsview | raw`). `auto` tries AgentsView first and falls back to raw
  scanning, recording the reason — including when a healthy probe is followed by
  a mid-listing nonzero exit, hang, or schema-invalid listing payload (TB-38;
  partial agentsview refs are discarded, never spliced). Every successful
  `session list` page is validated by `_decode_agentsview_list_payload`
  (`MalformedAgentsViewResponse`); the `auto` health probe runs that same check.
  Raw discovery stamps `SessionRef.is_subagent` for
  `<project>/<session-uuid>/subagents/*.jsonl` while keeping the owning project as the
  first path segment (S13). Exports that are not JSONL (e.g. a SQLite dump
  with a NUL in the header) raise `NonTranscriptExport` and are skipped by
  name (TB-10).
- **`hermes.py`** — direct read-only SQLite adapter for Hermes sessions
  (TB-11). Discovery still comes from AgentsView; only the read is redirected
  because `session export` returns the whole default-profile database.
  Per-call usage is `ABSENT_BY_SCHEMA`; session-row `cache_read_tokens` is
  surfaced on `ParseResult` for the Agent Breakdown caveat (S32 / TB-20),
  never attributed per call.
- **`passive.py`** — CLI and scan orchestration only. `main()` runs named
  phases `_plan_freeze` → `_resolve_corpus` (discovery / `--freeze` replay) →
  `_scan_refs` → render/write; also owns argparse, date-range filter, typed
  skips, and compatibility re-exports so historical
  `from toolbench.passive import …` imports keep working.
- **`reducer.py`** — incremental corpus aggregation (S11). Folds each
  session's `ParseResult` into per-agent / per-tool counters and discards the
  call list — never a whole-corpus `list[ToolCall]`. Schema-neutral: it only
  counts tags already stamped at parse time.
- **`report.py`** — five-section markdown render (S14) plus corpus fingerprint
  helpers (S36) and sampling disclosure (S41: `sampled` column, uneven-
  sampling apportionment). `render_report` orchestrates per-section
  `_render_*` helpers. Sections: agent breakdown (session-grain cache
  caveats + census fractions), tool leaderboard (`cache_assisted` as `yes` /
  `no` / `n/a` / `n/a*`), model breakdown, inefficiency callouts, summary
  (discovery reconcile, unjoinable records, S39 cache totals).
- **`freeze.py`** — write-once / replay corpus manifest for `--freeze`
  (S37). Round-trips `SessionRef` (including `is_subagent`) so replay bypasses
  live discovery without an import cycle on `passive`. On replay, a path under
  `…/subagents/…` still counts as a subagent even if a pre-TB-29 manifest stored
  `"is_subagent": false` — the path is ground truth (TB-29). Manifest format
  v2 (`toolbench-freeze-2`) optionally persists the freeze-time `AgentCensus`
  and its subagent-population filter so replay can disclose real historical
  fractions only for the population that denominator measured (TB-37).
  Missing census/filter metadata or a replay with the opposite filter marks
  fractions unavailable instead. Unreadable / malformed / non-UTF-8 inputs raise
  typed `MalformedFreezeManifest`; `passive` maps that (and write failures, or a
  directory at the freeze path) to exit 1 with `fatal freeze error` (S23 /
  PR #87).
- **`run_manifest.py`** — JSON reader for `--run-manifest` (S40). Defines a
  run's branch set (`branches` required; empty/missing is refused). Not
  `.lattice/orchestration/agents.md` — that file drops its Branch column when
  the run finishes.
- **`probe.py`** — scores matched tool-vs-Bash probe pairs from a dedicated
  session JSONL and emits a context-token + usage comparison table under
  `reports/`. Joins via `ClaudeParser(keep_raw_input=True, track_turns=True)`
  — one Claude walk, not a private duplicate. Tool arms match structurally
  (name + corpus target); bash arms match by sentinel. Usage is attributed
  only when the API response is isolable (one `tool_use`, no prose/reasoning —
  S26). Turns are keyed solely by `requestId` (S30); hermes-trace input is
  refused with `NonIsolableTurns`.
- **`complex.py` / `shell_safety.py` / `complex_runner.py`** — locate-then-fix
  library (no CLI yet). `complex.py` loads defects, scores trials, and renders
  the routing profile; `shell_safety.py` holds the bash tokenization /
  path-containment / gate-escape audits (`arm_violations`, `read_escapes`,
  `BANNED_TOOLS`) and is re-exported from `complex` so callers stay unchanged;
  `complex_runner.py` provisions worktrees and drives trials. See
  [Complex debug probe](#complex-debug-probe-library) below; design lives under
  [`docs/superpowers/specs/2026-07-12-complex-debug-probe-design.md`](docs/superpowers/specs/2026-07-12-complex-debug-probe-design.md).
  Selected complex probes are also packaged as Harbor tasks under
  [`benchmarks/harbor/toolbench-complex/`](benchmarks/harbor/toolbench-complex/).

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

## Complex debug probe (library)

The active probe (S16–S18) answers **cost per call**. The complex probe asks a
different question: **which toolset reaches a verified fix for the fewest
context tokens?** That changes the unit from tokens-per-call to tokens-to-
outcome, so the agent chooses its own path and step count dominates.

**Status:** library shipped (`src/toolbench/complex.py`,
`src/toolbench/shell_safety.py`, `src/toolbench/complex_runner.py`);
**no CLI yet**. Fixtures ship inside the package under
[`src/toolbench/probes/complex/`](src/toolbench/probes/complex/); the pinned
manifest is packaged at
[`src/toolbench/corpus/manifest.json`](src/toolbench/corpus/manifest.json), and
vendored corpora live under [`corpus/`](corpus/) (`vendor.sh` copies the
packaged manifest there so the vendored tree stays self-describing). Design:
[`docs/superpowers/specs/2026-07-12-complex-debug-probe-design.md`](docs/superpowers/specs/2026-07-12-complex-debug-probe-design.md).

| Piece | Role |
|---|---|
| `complex.py` | Load defects from fixtures, score a trial (`LOCATED:` + oracle), build/render a routing profile; re-exports shell-safety symbols |
| `shell_safety.py` | Bash tokenization, path-containment, and gate-escape audits (`arm_violations`, `read_escapes`, `BANNED_TOOLS`); re-exported by `complex` |
| `complex_runner.py` | Provision a hermetic worktree, shared deps cache, injectable `launch`/`oracle`, `run_trial` |
| `src/toolbench/probes/complex/<repo>-<id>-*/` | `defect.patch`, `truth.json`, `prediction.md`, `oracle.json`, `prompt.md` |
| `src/toolbench/corpus/manifest.json` | Pinned SHAs + dep/warmup/provision recipes for `wids`, `maltese`, `rich` |

**Operator constraints (verified in code):**

- Worktree provisioning and dependency setup default to the packaged manifest,
  not the generated `corpus/manifest.json` copy. Custom corpora must pass their
  manifest explicitly; a stale generated copy must never change a trial's SHA.
- `ensure_deps` builds npm caches and runs warmups from the **manifest SHA**,
  not corpus `HEAD` (#99). `package.json` / `package-lock.json` are read via
  `git show <sha>:…`; warmup commands run inside a short-lived `git archive` of
  that SHA. A shared corpus clone that advances without re-vendoring therefore
  cannot silently drift dependency caches away from the pinned trial source.
- Each repo cache leaf stamps `.manifest-sha` after a successful build (#102).
  A missing or drifted stamp wipes cached dep trees and rebuilds, so a packaged-
  SHA bump cannot leave oracles on stale `node_modules` while trials archive the
  new pin (legacy unstamped leaves rebuild once on the next `ensure_deps`).
- Prompt is always `PROMPT.md` from `provision_worktree` — never the defect
  rationale (that leaks the predicted winner). Missing `PROMPT.md` raises
  `UnprovisionedWorktree`.
- Dep cache defaults under `tempfile.gettempdir()/vendor-cache-<uid>` and must
  **diverge from the corpus at the filesystem root** (only common ancestor `/`).
  A walkable shared ancestor (e.g. both under `$HOME`) re-opens a pristine-
  source leak via `..` from a trial's `node_modules` symlink.
- The cache base must be a real, private directory owned by this uid — not a
  symlink (replaceable cache base → `UnsafeDepsCache`; the leaf is rejected
  *before* `resolve()`, including a dangling link), not world-accessible, not
  under a writable non-sticky ancestor. Contents are symlinked into every trial
  and executed by oracles.
- Arms are enforced by **transcript audit** (`arm_violations` + read-scope), not
  filesystem walls: any resolved read outside the trial tree voids the trial.
  Bash/control arms hold a full shell; the profile discloses that their
  read-scope audit is best-effort.

Call the library from tests or a future CLI; do not shell a real `claude` from
the hermetic suite — `launch` / `oracle` are injectable (S24 pattern).
Selected defects are also packaged as Harbor tasks — see
[`benchmarks/harbor/toolbench-complex/README.md`](benchmarks/harbor/toolbench-complex/README.md)
(WIDS D2 build canary; oracle/agent grading not verified yet).

## Worktree reclaim reporter

`toolbench worktrees` answers which linked checkouts hanging off this clone
could be reclaimed, and which are somebody's live tree. It **prints only** —
reclamation stays the hand-run procedure in `AGENTS.md` § Repository integrity
(`git worktree remove <path>` **then** `git branch -d <branch>`).

| Piece | Role |
|---|---|
| `classify()` | Parse `git worktree list --porcelain`, join `for-each-ref`, one verdict per **linked** tree (main checkout excluded) |
| `reclaimable()` | Narrow to `SAFE` + idle ≥ `IDLE_DAYS` (7); never DIRTY / LOCKED / UNIQUE-WORK / CLAIMED |
| `--reclaimable-only` | Print only reclaimable rows; **empty stdout** when there are none |
| `--hook` | SessionStart mode: one-line JSON context envelope, or silence; always exit 0 |
| `.claude/settings.json` | Tracked `SessionStart` registration (`uv run … toolbench worktrees --hook`, `--no-sync`, timeout 10s) |

**Verdict precedence** (`LOCKED > DIRTY > UNIQUE-WORK > CLAIMED > SAFE`): stop at
the first condition that disqualifies a tree and report it rather than force it.

- **LOCKED** — porcelain `locked`; needs `--force` twice to remove.
- **DIRTY** — modified or untracked entries (`git worktree remove` refuses).
- **UNIQUE-WORK** — tip is in no trunk/`refs/remotes/` ref; the only copy.
- **CLAIMED** — a live remote-tracking upstream still backs the branch
  (`%(upstream)` existence + `rev-parse --verify` liveness). A claim does **not**
  expire; `IDLE_DAYS` gates unclaimed trees only. Never reads
  `%(upstream:track)` (empty means both "in sync" and "no upstream" — the same
  class of bug that made `commit-commands:clean_gone` a silent no-op).
- **SAFE** — clean, unlocked, tip reachable from `refs/heads/main` or a live
  remote-tracking ref, and unclaimed.

Idle age comes from the admin `gitdir` mtime (git's own `--expire` signal), not
a commit date. Unknown idle/size degrade to `?` and fail the reclaimable
threshold rather than pass it. Verdict-bearing git failures raise
`WorktreeProbeFailed` on the terminal path; `--hook` swallows every failure and
exits 0 (a broken SessionStart reporter must not tax every session). `--hook`
and `--reclaimable-only` are mutually exclusive output modes. The hook speaks
only on `startup` / `resume` (`compact` is gated so long sessions do not re-nag).

## Status

**Implemented.** `src/toolbench/` ships all of tickets **T1–T6** in
[`BUILDPLAN.md`](BUILDPLAN.md): the scaffold, the transcript parser, the
multi-agent source layer, the passive analyzer, and the active probes.
Post-merge hardening covers **TB-8** (subagent `--project` filter), **TB-9**
(callout denominators), **TB-10** (non-UTF-8 / non-transcript exports),
**TB-11** (Hermes SQLite direct read — discovery still via AgentsView),
probe isolability (**S26** / TB-14–16), schema dispatch (**S27–S28** /
TB-13), usage provenance (**S29–S30** / TB-18: producer-aware
`UsageProvenance` on every `ToolCall`, and `probe.py` refusing corpora it
cannot key to the billing unit), the gate itself running every test
(**S31** / TB-19: the documented command is `pytest`, not `unittest
discover`, which silently missed 37 module-level tests), session-grain
Hermes cache surfacing (**S32** / TB-20: Agent Breakdown caveat, never
folded into the per-call `cache_assisted` column), the codex schema
(**S33** / TB-12: `CodexParser` joins three paired `response_item` shapes on
`payload.call_id`), typed skips + discovery reconcile (**S34–S35** / TB-21 /
TB-23), corpus fingerprint + `--freeze` (**S36–S37** / TB-22), unjoinable
records (**S38** / TB-24), Claude session-grain cache read+creation
(**S39** / TB-26), and per-run cache-token grouping via `--run-manifest`,
entry-grain by `gitBranch` (**S40** / TB-27). Follow-ons name the
detached-HEAD attribution blind spot (**TB-28**) and make
`--exclude-subagents` match the real nested
`<project>/<session-uuid>/subagents/` layout (**TB-29**). AgentsView hang
bounds + operator ceiling (**TB-32** / **TB-39**), mid-listing `auto` fallback
without splicing (**TB-38**), and per-agent sampling disclosure with
apportionment (**S41** / **TB-33** / **TB-35**) — including census on the
zero-match path (**TB-34**) and freeze-time census in manifest v2 with a
subagent-population filter guard (**TB-37**) — are shipped. The complex debug
probe library (`complex.py` / `shell_safety.py` / `complex_runner.py`) is
implemented as a library (fixtures under `src/toolbench/probes/complex/`; no
CLI yet). The linked-worktree reclaim reporter (`worktrees.py`, **S42** /
PR #90) ships as a third console subcommand — table, `--reclaimable-only`, and
SessionStart `--hook`. Opt-in Laminar CLI tracing (`observability/` +
`tracing.py`, **S20** / PR #97 / #104) wraps real console processes only when
`TOOLBENCH_TRACING=1` and the `tracing` extra / project key are present. CQ
follow-ons split passive into `reducer`/`report`,
fold probe into `ClaudeParser` (`keep_raw_input` / `track_turns`), and stamp
inefficiency tags at emit. The strict gate (`uv run ruff check .`,
`uv run python -m toolbench.complexity_gate --base origin/main`,
`uv run mypy --strict src/toolbench tests`, `uv run pytest -q`) is green —
**748** tests passing / **4** skipped on the default install, **749** / **3**
with `--extra tracing` (the observability skip becomes a pass). Each
coverage-oriented skip runs somewhere and the "somewhere" is recorded in
`AGENTS.md` so a skip is never mistaken for coverage: the optional-tracing
test runs in the `tracing` job of `ci.yml` (shallow checkout; no complexity
base); the corpus fixtures (`tests/test_complex_fixtures.py`) run in
[`.github/workflows/corpus.yml`](.github/workflows/corpus.yml) — weekly, on
demand, and on `pull_request` for corpus-touching paths (#119); and the Hermes
live-archive test in `tests/test_hermes.py::LiveArchive` is deliberately
operator-run before a release and whenever a Hermes upgrade changes the
`sessions`/`messages` schema, per `EVALUATION.md`. A fourth default skip — the
sidecar-less WAL classic-reject pin in `test_hermes.py` — is build-dependent
(modern SQLite may skip it) and is not a missing CI lane. `mypy --strict`
covers `tests` as well as
`src/toolbench`. A bare `uv run mypy` also mirrors that scope via
`[tool.mypy]` in `pyproject.toml` (it does not descend into `tools/`). The
same four gate commands run in the `gate` job of `.github/workflows/ci.yml`
on every PR and on pushes to `main`; the sibling `tracing` job re-runs
pytest with `--extra tracing`.

Source-of-truth documents:

- [`SPEC.md`](SPEC.md) — 42 numbered acceptance criteria (S1–S42).
- [`EVALUATION.md`](EVALUATION.md) — verification map for every criterion.
- [`BUILDPLAN.md`](BUILDPLAN.md) — decided architecture and the T1–T6 tickets
  plus post-merge TB/T rows.
- [`docs/2026-07-07-tool-benchmarks-design.md`](docs/2026-07-07-tool-benchmarks-design.md)
  — full v2 design spec.
- [`protocols/active-probes.md`](protocols/active-probes.md) — probe corpus,
  arm matching (S17), isolability (S26), and the seeded `#8376` baseline table.
- [`protocols/probe-run-sheet.md`](protocols/probe-run-sheet.md) — executable
  ten-turn operator run sheet for scoring a fresh probe session.
- [`docs/superpowers/specs/2026-07-12-complex-debug-probe-design.md`](docs/superpowers/specs/2026-07-12-complex-debug-probe-design.md)
  — locate-then-fix complex probe (library shipped; no CLI yet).
- [`benchmarks/harbor/toolbench-complex/README.md`](benchmarks/harbor/toolbench-complex/README.md)
  — Harbor packaging for selected complex probes (WIDS D2 canary).
- [`.claude/skills/cache-token-metrics/SKILL.md`](.claude/skills/cache-token-metrics/SKILL.md)
  — operator recipe for per-run cache-token diffs (S39).
- [`.claude/skills/laminar/SKILL.md`](.claude/skills/laminar/SKILL.md)
  — Laminar instrumentation / CLI / SQL guidance for the optional `tracing` extra.

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
  until this adapter landed (TB-11). Per-call usage is absent by schema; when
  the session row carries `cache_read_tokens`, that appears as an Agent
  Breakdown caveat only (S32).

A fourth path is content-detected, not selected by `--index-source`:
`hermes sessions export --format trace` JSONL is claimed by
`HermesTraceParser` (S29). Valid for `passive` (`cache_assisted` → `n/a`);
refused by `probe` (`NonIsolableTurns`, S30).

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
`uv.lock`, empty default runtime deps, and optional `tracing` extra). The `dev`
group installs only the gate tools (`ruff` / `mypy` / `pytest`); the shipped
package stays stdlib-only by default. Requires Python ≥3.13.

```sh
# Bootstrap (once per checkout; also runs implicitly under `uv run`)
uv sync

# Passive analyzer — default scope is every agent, every project
uv run python -m toolbench.passive --agent all --all

# Scope by project / time / index source
uv run python -m toolbench.passive --project my-repo --since 2026-06-01
uv run python -m toolbench.passive --all --index-source agentsview
uv run python -m toolbench.passive --all --date-from 2026-06-01 --date-to 2026-06-30
uv run python -m toolbench.passive --all --exclude-subagents --out reports/2026-07-08-tool-usage.md

# Reproducible before/after: freeze the corpus once, then replay it to compare.
# First run writes the manifest; every later run scans the frozen set and names
# what has vanished since (TB-22).
uv run python -m toolbench.passive --all --freeze reports/corpus.manifest   # writes
uv run python -m toolbench.passive --all --freeze reports/corpus.manifest   # replays

# Active tool-vs-Bash probes. Score a dedicated probe session; without
# --session every arm is seeded and the report is refused (SeededReportError).
# Operator run sheet: protocols/probe-run-sheet.md (ten arms, ten turns).
uv run python -m toolbench.probe --session /path/to/probe-session.jsonl --out reports/active-probe-comparison.md
uv run python -m toolbench.probe --allow-seeded   # baseline table only; measures nothing

# Per-run cache-token metrics (S40) — --run-manifest groups per-entry usage
# by gitBranch, folded into the passive analyzer itself (no separate module).
# Prefer from repo root via -m; from ~ use the path form in
# .claude/skills/cache-token-metrics/SKILL.md (module resolve fails outside
# the checkout).
uv run python -m toolbench.passive --agent claude --run-manifest run.json --tickets 12

# Linked git worktrees, with a reclaim verdict per tree (S42). Reports only —
# it never removes a tree, deletes a branch, or touches a ref (procedure:
# AGENTS.md § Repository integrity). `--reclaimable-only` prints nothing at
# all when nothing is reclaimable. `--hook` is the SessionStart mode (tracked
# in `.claude/settings.json`); mutually exclusive with `--reclaimable-only`.
uv run toolbench worktrees
uv run toolbench worktrees --reclaimable-only
# uv run toolbench worktrees --hook   # stdin = SessionStart payload; exit 0

# Tests
uv run pytest -q
```

### Optional Laminar tracing

[Laminar](https://laminar.sh/docs/tracing/integrations/overview) is an explicit
opt-in for real `toolbench` console commands. It can record one trace per
opted-in console command; normal `passive` / `probe` runs and library-style
calls such as `main([...])` stay untraced and do not import the optional SDK, so
unit tests and embedding applications do not send telemetry.
`toolbench worktrees --hook` is also untraced: SessionStart must stay silent
and failure-tolerant even when tracing is otherwise enabled.

Initialization lives in
`toolbench.observability.setup_tracing.setup_tracing`. It returns `False`
instead of interrupting the CLI when the optional SDK or project key is absent.

Set up a Laminar project locally from the repository root:

```sh
npx lmnr-cli setup
uv sync --extra tracing
```

`setup` writes the project link to `.lmnr/project.json` and the project API key
to `.env`; both are gitignored. Never commit or print that key. `.env.example`
documents only the variable name.

Run a representative traced command:

```sh
TOOLBENCH_TRACING=1 uv run --extra tracing toolbench probe \
  --session tests/fixtures/probe_session.jsonl \
  --out reports/active-probe-comparison.md
```

`TOOLBENCH_TRACING=1` is the opt-in boundary; a Laminar key alone does not
enable tracing. The normal strict gate uses `uv sync` without the `tracing`
extra, so the optional SDK is not part of the hermetic default environment.

Verify the newest trace:

```sh
npx lmnr-cli sql query \
  "SELECT * FROM traces ORDER BY start_time DESC LIMIT 1" \
  --json
```

The trace uses the stable root span `toolbench.cli`, command tags, metadata such
as `{"command": "probe"}`, and an exit-code output. It deliberately excludes
CLI arguments, transcript paths, session identifiers, prompts, report contents,
and parsed outputs. Laminar auto-instrumentation is disabled because Toolbench
has no LLM-provider client to capture and its inputs can contain private
transcripts.
Operation exceptions are re-raised only after the tracing span closes, so their
messages are not exported by this wrapper.

Open the project selected by `setup` in the Laminar dashboard to inspect its
traces.

### Probe scoring pitfalls

- **Fresh session only.** Mentions of sentinels, the run sheet, or
  `toolbench/probe.py` are discarded as contamination (`MENTION_MARKERS`).
- **One tool call per API response.** Usage is keyed by `requestId` (S26 /
  S30). Batching, prose, or reasoning in an arm turn blanks the usage column
  (`—`) while keeping real context tokens — it does **not** re-seed the cell.
  There is no timestamp fallback; a corpus without `requestId` (including
  `hermes sessions export --format trace`) raises `NonIsolableTurns`.
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
- **Native Claude transcript only.** Score a Claude Code probe session, not
  a hermes trace export — `passive` accepts both; `probe` does not (S30).

### `--index-source` policy

- `auto` (default) — tries AgentsView first; on failure, falls back to
  scanning the raw root directly and records the fallback reason in the
  report's Summary section.
- `agentsview` — AgentsView only; a source error is fatal.
- `raw` — raw local transcript roots only; a source error is fatal.

"Failure" means any of four things, not two (TB-32 / TB-38). AgentsView can be
**absent** (binary not on `PATH`), **broken** (nonzero exit), **hung** — a daemon
that accepts the connection and never answers — or **malformed** (zero-exit stdout
that fails the listing contract). Every `agentsview` call is bounded by
`AGENTSVIEW_TIMEOUT_S` (60s, `sources.py`) and a hang is raised as
`AgentsViewTimeout`. Successful `session list` pages are decoded by
`_decode_agentsview_list_payload`, which raises `MalformedAgentsViewResponse`
(`ValueError`) for invalid JSON, a non-object payload, `sessions` not a list, a
row missing required `id` / `agent` / `project`, non-empty `id` / `agent` (empty
`project` is valid — AgentsView emits `""` for projectless/global sessions), a
non-string `next_cursor`, or a bad `total`. Where that surfaces depends on when
the daemon fails the check:

- at the `auto` probe → fallback to raw, reason named in the Summary (timeout
  prose, or the schema-validation message when `--limit 1` returns a zero-exit
  but invalid payload);
- mid-listing, after a healthy probe, during pagination → `auto` still falls back to
  raw (TB-38): a nonzero exit, hang, or schema-invalid listing discards the
  partial agentsview listing and rescans the corpus wholesale from the filesystem
  — never spliced onto truncated agentsview refs (a mixed corpus would break the
  fingerprint identity TB-22 protects). Explicit `--index-source agentsview` stays
  fatal for the same mid-listing failures;
- mid-scan, on a per-session `export` → that session is skipped under the
  `export_timeout` reason and the scan continues (a sick daemon costs sessions, not
  the whole run);
- under `--index-source agentsview` → fatal, as any source error is there. No silent
  fallback: the operator asked for AgentsView explicitly.

A vanished binary mid-discovery (`FileNotFoundError`) keeps its narrower handling: a
named `MISSING_SOURCE` skip and an unavailable census, no raw rescan — a gone binary
is not evidence the raw root is healthier.

### `--agentsview-timeout SECONDS`

Overrides that ceiling (TB-39). Default `60.0` — omit the flag and behaviour is exactly
as above.

| value | meaning |
|---|---|
| `> 0` | bound each `agentsview` call at that many seconds |
| `0` | **unbounded** — `timeout=None`; a hung daemon blocks the run forever |
| `< 0` | rejected at parse |

Raise it when a large archive makes a *healthy* daemon exceed 60s (otherwise our own
default truncates the corpus and blames it on `export_timeout` skips); lower it when
debugging a daemon you already suspect is hung.

The Summary names the timeout **only when it changed what you are looking at** — if it
truncated the corpus (≥1 `export_timeout` skip), or if the run was unbounded. A clean
bounded run says nothing. The unbounded case is disclosed precisely because no skip can
ever record it: an unbounded call never times out, so a clean report from one is not
evidence of a healthy daemon — it may only be evidence of luck.

`--agent` filters AgentsView listing only. Under `--index-source raw` the
discovery root is Claude Code sessions, so `--agent` is a no-op there.

`--project` matches the **owning project directory** under the raw root
(first path segment after the root), not `path.parent.name`. Nested
subagent transcripts at `<project>/<session-uuid>/subagents/*.jsonl` are attributed to that
owning project with `SessionRef.is_subagent=True`, and are only dropped when
you pass `--exclude-subagents`.

### Corpus reproducibility (`Corpus fingerprint` + `--freeze`)

The corpus is a moving target: claude-mem observer transcripts age out of a
~30-day sliding window *mid-scan*, so its tail deletes itself at roughly re-run
cadence, and the live session appends calls while it is read. Two reports are
therefore not automatically diffable — a delta between them may be the corpus
moving, not your code (TB-22).

- **`Corpus fingerprint: <digest> (<N> sessions scanned)`** (always emitted in the
  Summary, S36) is a hash over the *scanned* set — the sessions that produced the
  numbers. It folds each session's identity **and** every content count the Summary
  renders (calls, malformed lines, unjoinable records), so a vanished tail (an id
  leaves the set) and any kind of append (a new call, a malformed line, or a
  `web_search_call`) all move it. **Two reports whose fingerprints match are
  diffable; if they differ, do not attribute the delta to code** until you know why
  the input set moved.
- **`--freeze <manifest>`** (S37) makes a before/after actually reproducible. The
  first run writes the discovered ref list to the manifest; every later run
  *replays* it — scanning exactly the frozen set instead of re-discovering — and
  reports `(<V> vanished since freeze)` for refs whose transcripts have since been
  deleted (`--verbose` names them). Over an unchanged corpus a replay is
  byte-identical; when the tail has moved, the vanished count names the mechanism
  instead of letting it masquerade as a code effect. Replay requires a **regular
  file** at the path (`Path.is_file()`); a directory, unreadable / non-UTF-8 /
  invalid JSON manifest, or a write failure is a hard stop (`fatal freeze error`,
  exit 1) — not a traceback and not a silent re-discover (S23 / PR #87).
  **Write-once includes an empty first write:** if discovery filters exclude every
  session (e.g. an overly narrow `--since`), the manifest is still pinned with
  `count: 0`, and later replays analyze nothing until you delete or rewrite the
  file on purpose. Confirm the first-write Summary session count before treating
  the path as a durable pin.
  - **Manifest v2 + freeze-time census (TB-37).** New freezes write
    `toolbench-freeze-2` and, when the freeze-time census succeeded, persist it
    under a `census` key together with its subagent-population filter. Replay then
    shows real per-agent `sampled` fractions only when that filter matches, with an
    explicit **Historical denominator** caveat — the archive size as of freeze
    time, not today's. A v1 manifest, a v2 write whose census itself failed, a
    legacy v2 census without filter metadata, or a replay using the opposite
    `--exclude-subagents` choice marks fractions unavailable instead.

### Sampling disclosure (`sampled` column + uneven line)

`--limit` truncates discovery in **recency order across the whole archive**, not
per agent (S41 / TB-33). Each Agent Breakdown row therefore rests on a different
fraction of that agent's history, and an agent whose work is all older than the
window can vanish at `sessions == 0`.

- The `sampled` cell is `reached / census_total (fraction)`. Agents present in the
  archive but never reached still get a row — `sessions == 0` means looked-and-
  found-none, not never-looked.
- When sampling is uneven, the report names causes from **observed signals only**
  and apportions the per-agent remainder (`total - sampled`) between truncation
  and attrition (TB-35). A `--limit` that was passed but never bit is not
  truncation; a negative remainder is flagged as census/scan drift.
- Cross-agent ratios are comparable only when no uneven-sampling line prints.
- A zero-match early return ("no sessions matched…") still appends the census the
  run already built (TB-34) — a narrow `--since` / `--date-*` window must not read
  as an empty archive.

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

The Tool Leaderboard's `cache_assisted` column is caveat-only (S19) and
uses four values (S29): `yes` (a hit was observed), `no` (usage was
measurable and zero hits), `n/a` (usage unavailable for every call in the
bucket — e.g. hermes SQLite or hermes-trace), `n/a*` (mixed). Neither
`n/a` form is a measured zero.

When Hermes sessions carry session-row `cache_read_tokens`, the Agent
Breakdown adds a caveat line such as `M of N sessions carry session-grain
cache_read_tokens > 0` (S32). Claude sessions contribute the Summary line
`Session-grain cache tokens: read=… creation=… (… measured sessions; S39
caveat, not ranked)` — read and creation together, because a prefix-sharing
change can trade one for the other while `TOTAL_BILLED` stays flat. Neither
signal is ever divided into a per-call rate or mixed into `cache_assisted`.

With `--run-manifest`, the Summary also emits a **Run cache tokens** block
(S40 caveat, never ranked):

```text
- Run cache tokens (run tb-27): read=… creation=… (N candidate sessions; S40 caveat, not ranked)
  - per ticket (T): read=… creation=…
  - unattributed: read=… creation=… (same-session work off the run's branches)
  - detached-HEAD (unattributable): read=… creation=… input=… output=…
    (M sessions; may include run delegators -- run total may be low)
  - matched no entries: feat/missing-branch
```

Attribution is **per entry** by `gitBranch`, not per session. `unattributed`
is spillover inside candidate sessions (sessions with ≥1 entry on a run
branch) — not corpus-wide `main`. `detached-HEAD` is usage stamped
`gitBranch="HEAD"` (a detached checkout): it cannot match any manifest
branch, so it is **named and never folded into the run total** (TB-28).
Input/output appear on that line because an uncached detached turn can have
zero cache and still be real billed work. A large detached or unattributed
line means the run headline may understate what the orchestration spent.

### Troubleshooting / common pitfalls

| Symptom | Likely cause | What to do |
|---|---|---|
| Summary `scanned` is far below the corpus size | Not a bug: many discovered sessions skip (dead index entries, parser gaps) | Read the `Sessions discovered: D / scanned: M / skipped: K` line and the `Skipped by reason` histogram — they reconcile the gap by typed reason (S34/S35). `scanned` was never the corpus size. |
| Run dies with `UnicodeDecodeError` | Pre-TB-10 behavior, or a custom strict-decode runner | In-tree readers use `errors="replace"`. One bad session should be counted under **Skipped by reason** as `decode_error`, not abort the corpus. |
| Summary counts a `non_transcript` skip for a non-Hermes session | AgentsView `session export` returned binary with returncode 0 | Expected for off-contract exports; the NUL sniff rejects them before parse (TB-10). Run with `--verbose` to name the session. Hermes sessions should not hit this path — they route through `hermes.py`. |
| `--project X` silently omits every subagent | Pre-TB-8 filter matched `path.parent.name` (`subagents`) | Current code matches the owning project dir. Re-run on current `main`. |
| Callouts are bare integers (`Failures: 865`) | Pre-TB-9 report formatting | Current callouts include denominators and a top offender. |
| `--agent hermes` yields far fewer sessions than expected | AgentsView `session list` under-counts Hermes vs `stats` (~89 vs ~789) | Known upstream limit ([#1048](https://github.com/kenn-io/agentsview/issues/1048)); discovery is intentionally not forked into the Hermes adapter (S9b). |
| Hermes session skipped / archive not found | `$HERMES_HOME` / `~/.hermes` missing, or session only in an unread profile DB | Confirm `HERMES_HOME`; it counts under the `non_transcript` reason and `--verbose` names the session. Profile DBs under `profiles/*/state.db` are searched. |
| `Malformed lines` explodes into the hundreds of thousands | Binary export absorbed as text (would happen without the NUL sniff) | Should not occur on current code — binary payloads are rejected before parse. |
| Empty selection message | No sessions matched filters, or all matched sessions were skipped | Check `--project` / `--since` / `--date-*`, and the `(skipped K: reason=count)` suffix on the message; the census disclosure that follows (TB-34) distinguishes a narrow window from a truly empty archive. `--verbose` names each session. |
| `toolbench.probe` without `--session` refuses to write | Seeded-only report is blocked (`SeededReportError`) | Pass `--session PATH`, or `--allow-seeded` for the baseline table only. |
| Probe usage column shows `—` but context tokens are real | Arm matched, but the API response was not isolable (prose, thinking, or batched `tool_use` — S26) | Re-run from [`protocols/probe-run-sheet.md`](protocols/probe-run-sheet.md); one tool call per turn, no surrounding prose. |
| Bash usage looks ~15–20 tokens higher than the tool arm | Sentinel + optional Bash `description` are billed into bash `output_tokens` only (TB-17) | Expected until TB-17. Compare context-token columns; treat usage as non-comparable. |
| `cache_assisted` shows `n/a` / `n/a*` for hermes (or hermes-trace) | Per-call usage is absent by schema or dropped by the trace export (S29) | Expected. Do not read `n/a` as "no cache hits". Session-grain cache, when present, appears as an Agent Breakdown caveat (S32), not in this column. |
| `toolbench.probe` raises `NonIsolableTurns` on a hermes trace file | Trace export has no `requestId`; probe keys turns only by that field (S30) | Score a native Claude Code probe session instead. Trace remains valid input to `passive`. |
| `cursor` sessions appear only under the `unknown_schema` skip reason | No parser claims cursor's schema yet (`UnknownSchema`, S28) | Expected until a `CursorParser` lands. It must not appear as a healthy zero-call agent; `tally_skips`/`--verbose` surface the count and ids (S34). |
| Sessions skipped under the `export_timeout` reason | The AgentsView daemon stopped answering **mid-scan**; each `export` is bounded at `AGENTSVIEW_TIMEOUT_S` (TB-32) | Not a bad session — a sick daemon. The probe passed, so the hang began later; the scan degrades to skips rather than dying. Restart AgentsView and re-run, or use `--index-source raw`. A run where *many* sessions carry this reason is not a corpus to trust. |
| `--index-source auto` used to exit 1 after a healthy probe | Mid-listing failure (nonzero exit, hang, or schema-invalid listing — bad JSON **or** missing/`sessions`/row/`next_cursor`/`total` contract) used to be fatal; now falls back to raw and discards the partial listing (TB-38) | Expected on current `main`. Explicit `--index-source agentsview` still exits 1 — that is the strict path. A zero-exit but schema-invalid health probe also falls back under `auto`. |
| AgentsView listing drops Reasonix / other projectless sessions as malformed | Pre-#80 validator required non-empty `project` on every row; AgentsView emits `""` for global/archive sessions | Current code requires `project` as a string but allows empty. `id` / `agent` stay non-empty. Re-run on current `main`. |
| `cache_assisted` shows `n/a` for every `codex` tool | codex has no per-call usage channel; it bills per turn via `token_count` events (`ABSENT_BY_SCHEMA`, S33) | Expected. Do not read `n/a` as "no cache hits". |
| `codex` reports 0 errors no matter what failed | codex encodes exit status in the output text and sets `status: completed` even for failed tools (S33) | Expected. `error` is never inferred from output prose. Use `output_chars` / the raw transcript to inspect failures. |
| `codex` web searches never appear in the leaderboard | `web_search_call` carries no `call_id` and emits no output record, so it cannot be joined (S33 / TB-24) | Expected. They are not joinable calls, so leaderboard/ratio counts exclude them. The count is not lost: the Summary's `Unjoinable tool records (seen, not joined)` line names it as `codex/web_search_call` (S38). |
| Fingerprints differ between two "same" runs | Corpus moved: vanished observer tail and/or live append (S36) | Do not attribute the delta to code. Re-run with `--freeze` (S37) or compare only when digests match. |
| `--freeze` replay reports vanished sessions | Frozen refs' transcripts aged out or AgentsView `source file not found` | Expected when the sliding window deletes mid-corpus. `--verbose` names them; rewrite the manifest only when you intentionally want a new pin. |
| `--freeze` replay shows "Historical denominator" | Manifest v2 carried a freeze-time census (TB-37) | Expected. Fractions are archive size at freeze time, not today. Do not treat them as a live census. |
| `--freeze` replay still says fractions unavailable | Manifest has no usable `census` (v1, freeze-time census failure, legacy v2 without population metadata, or replay changed `--exclude-subagents`) | Expected. Use the same subagent filter as the freeze; rewrite a legacy freeze on current `main` if you want historical fractions. |
| `--freeze` exits 1 with `fatal freeze error` / traceback used to escape | Path is a directory, unreadable, non-UTF-8, or invalid JSON; or the first-write could not create the file | Point `--freeze` at a JSON *file* path (create parent dirs if needed). Same contract as a bad `--run-manifest` (S23 / PR #87). |
| `--freeze` replay always analyzes zero sessions after a "successful" first write | First write happened while filters excluded every session (`count: 0`); write-once then permanently pins the empty set | Delete or intentionally rewrite the manifest after widening `--since` / `--project` / other filters. Check the first-write Summary session count before reusing the path. |
| Complexity gate fails a function you only moved / renamed | Identity is `(path, qualified name)`; a rename looks like a new function | Reduce it under 10, or land the move with a real simplification. `# noqa: C901` will not hide it. |
| Complexity gate is silent on a hotspot you expected to fail | Only `src/` and `tests/` `*.py` changed vs `--base` are measured; files outside that scope, or unchanged files, are ignored | Diff against the intended base (`origin/main` locally; CI uses the PR base / pre-push SHA). Confirm the path is under `src/` or `tests/`. |
| Local complexity gate cannot find `--base` | Shallow clone or missing remote-tracking ref | `git fetch origin main` (or deepen the clone). The `gate` job in `ci.yml` sets `fetch-depth: 0` for the same reason; the `tracing` job stays shallow. |
| Agent Breakdown ratios look incomparable across agents | `--limit` truncates in whole-archive recency order (S41) | Read the `sampled` column and the uneven-sampling line. Compare across agents only when that line is absent. |
| `toolbench` / `-m toolbench.passive` fails from `~` with a system python | The checkout's venv (with the editable install) isn't active | Use `uv run --project ~/tool-benchmarks toolbench passive ...` from any cwd; inside the repo, `uv run toolbench ...` or `uv run python -m toolbench.passive` both work. |
| `corpus/manifest.json` disappeared after pulling the src-layout change | The manifest now ships inside the package (`src/toolbench/corpus/manifest.json`); the corpus copy is generated / gitignored | Re-run `corpus/vendor.sh` (idempotent — skips existing clones); it copies the packaged manifest back into `corpus/`. Default trial provisioning already uses the packaged pin (#78), so a missing or stale corpus copy no longer changes trial SHAs unless you pass a custom `manifest_path`. |
| `tests/test_complex_fixtures.py` always skips locally | Gated behind `TOOLBENCH_CORPUS_TESTS` (needs vendored corpus + `npm`/`cargo`/venv) | Run `corpus/vendor.sh`, then `TOOLBENCH_CORPUS_TESTS=1 uv run pytest -q tests/test_complex_fixtures.py`. CI coverage is `.github/workflows/corpus.yml`, not the `gate` job — see [`EVALUATION.md`](EVALUATION.md). |
| Complex deps cache looks rebuilt from a newer corpus commit than the trial | Pre-#99 `ensure_deps` copied npm manifests / ran warmups at corpus `HEAD` while the trial tree stayed on the packaged SHA | Current code pins both paths to the manifest SHA (`git show` / archived warmup tree). Clear the affected `vendor-cache-<uid>/<repo>/` leaf and re-run `ensure_deps` on current `main`. |
| Complex oracles look wrong after a packaged-manifest SHA bump (pre-#102) | Existence-based `target.exists()` skip left stale `node_modules` while trials archived the new SHA | Current code stamps `.manifest-sha` and rebuilds on drift/missing stamp (#102). Re-run `ensure_deps` on current `main`; legacy unstamped leaves rebuild once automatically. |
| Summary cache read ↓ but creation ↑ by ~the same | Prefix-sharing moved cost between buckets (S39/S40) | Not a win. Compare read **and** creation together; read alone misleads. |
| `--run-manifest` run total looks too low vs wall-clock spend | Detached-HEAD usage (`gitBranch="HEAD"`) cannot match any branch set (TB-28) | Read the `detached-HEAD (unattributable)` line (includes input/output). Do not fold it into the run — a detached delegator is indistinguishable from unrelated detached work. |
| `--run-manifest` shows a large `unattributed` line | Candidate sessions also ran on non-run branches (straddle spillover, S40) | Expected. The run total is only the in-set entry slice; do not treat session totals as run-owned. |
| `--run-manifest path.md` (or empty `branches`) exits 1 | Manifest must be JSON with a non-empty `branches` list (S40) | Use a dispatch-time JSON like `.lattice/orchestration/run-tb21-23.json`; `agents.md` cannot serve (Branch column is discarded on completion). |
| `--exclude-subagents` still includes nested subagents / freeze replay ignores the flag | Pre-TB-29 discovery checked `rel.parts[1] == "subagents"` (flat layout that does not exist on disk); freeze manifests could pin stale `"is_subagent": false` | Current code matches `"subagents" in rel.parts[1:-1]` and ORs path re-derivation on freeze replay. Re-run on current `main`; rewrite the freeze manifest only if you intentionally want a new pin. |
| Complex trial raises `UnsafeDepsCache` | Dep cache shares a walkable ancestor with the corpus, is a symlink (including dangling — checked before `resolve()`), or is not private to this uid | Pass `deps_base=` (or set `$TMPDIR`) so cache and corpus diverge at `/`; never point the cache at a replaceable symlink. |
| Complex trial raises `UnprovisionedWorktree` | `run_trial` was called without `provision_worktree` (no `PROMPT.md`) | Call `provision_worktree` first. There is no fallback prompt — the rationale would leak the predicted winner. |
| `toolbench worktrees` flags a long-idle tree you meant to keep | The branch has no live remote-tracking upstream, so it is not `CLAIMED` | Push/set an upstream, or leave it — reclaimable requires `SAFE` + idle ≥7d. A live upstream is a standing exemption at any age. |
| `toolbench worktrees` raises `WorktreeProbeFailed` | A verdict-bearing git call failed (listing, refs, status, reachability) | Fix the git/mount problem and re-run. `--hook` never raises — it exits 0 silently so a broken reporter does not disable SessionStart. |
| SessionStart never mentions reclaimable trees | Zero reclaimable candidates, gated `source` (`compact`/`clear`/`fork`), or a swallowed probe failure | Run `uv run toolbench worktrees` for the full table. Silence is the answer when nothing is reclaimable. |
| `commit-commands:clean_gone` reports success but removes nothing | It greps `git branch -v` for literal `[gone]`; real output is `[origin/<name>: gone]` | Use `uv run toolbench worktrees --reclaimable-only`, then the AGENTS.md reclaim procedure. Do not trust `%(upstream:track)` emptiness either. |
| Stale linked worktrees under `.claude/worktrees/` fill the disk and block `git branch -d` | Nested agent worktrees keep their branches checked out; `git worktree prune` / `commit-commands:clean_gone` are no-ops while directories exist | `git worktree remove <path>` **then** `git branch -d <branch>`. Select with `uv run toolbench worktrees --reclaimable-only`. The ignore boundary is tracked in `.gitignore` (`.claude/worktrees/`). |

## Quality gate

Before any PR, run:

```bash
uv run ruff check .
uv run python -m toolbench.complexity_gate --base origin/main
uv run mypy --strict src/toolbench tests
uv run pytest -q
```

The complexity command uses Ruff's `C901` measurement and compares changed
Python functions under `src/` and `tests/` by file plus qualified function name
(untracked `*.py` under those trees are included). Defaults are
`toolbench.complexity_gate.DEFAULT_THRESHOLD = 10` and `--warning-delta 2`.
There is deliberately no `[tool.ruff.lint.mccabe]` block in `pyproject.toml` —
`ruff check .` does not select `C901`, so a `max-complexity` key there would be
inert. The gate reads its threshold from `complexity_gate.py` and runs Ruff
with `--isolated` so the base snapshot is measured under the same config as the
working tree. Change the budget in `complexity_gate.py`, not in `pyproject.toml`:

- a new function above 10 or an existing function crossing 10 fails;
- an already-baselined function above 10 passes when unchanged or reduced, but
  fails if it gets worse;
- an increase of 2 or more that remains at or below 10 emits a review warning;
- `# noqa: C901` does not hide a function from the regression comparison
  (`ruff check --ignore-noqa`).

Exit code is 1 only when there are errors; warnings still exit 0 and print
GitHub Actions annotations (`::error` / `::warning`). Override locally with
`--threshold` / `--warning-delta` if you need to reproduce a narrower check —
CI uses the defaults.

The Ruff rule set is also pinned explicitly in `pyproject.toml` rather than
inherited from Ruff's defaults (#120). Ruff 0.16 expanded its default set from
four families to 413 rules, which turned a clean `ruff check .` into 122
findings across a routine `uv lock --upgrade`. Every family and individual rule
in `[tool.ruff.lint]` was adopted deliberately after triaging its findings on
this tree; widening the list is the same deliberate act (see the comments in
`pyproject.toml`).

This keeps legacy hotspots visible without making old debt an unrelated PR
failure. Renaming or moving a function changes its comparison identity, so a
moved hotspot above 10 is treated as new and should be reduced or deliberately
reviewed. For an optional ranked report, run
`uvx radon cc src --show-complexity --average --min C`; Radon is diagnostic only
and is not part of the lockfile or merge gate.

The pytest command must collect every test, including module-level `test_*`
functions that `unittest discover` silently misses (S31).

GitHub Actions runs that same gate on every pull request and every push to
`main` via the `gate` job in `.github/workflows/ci.yml` (`uv sync --frozen
--python 3.13`, then Ruff / complexity regression / mypy strict / pytest). PRs
compare with the pull request's base SHA; pushes compare with the pre-push
SHA. Only `gate` fetches full Git history (`fetch-depth: 0`) so the complexity
base resolves; the sibling `tracing` job uses a shallow checkout, installs
`--extra tracing`, asserts `lmnr` is importable, and re-runs pytest so the
optional-tracing skip is not the only coverage. Corpus fixture acceptance
lives in a separate workflow
([`.github/workflows/corpus.yml`](.github/workflows/corpus.yml): weekly, on
demand, and on corpus-touching PRs). For the local opt-in command and the exact
path filter, see [`EVALUATION.md`](EVALUATION.md) (`TOOLBENCH_CORPUS_TESTS=1`).
The workflows remain least-privilege
(`permissions: contents: read`) and do not broaden the gate (no
`ruff format --check`, no mypy over `tools/`). `[tool.mypy]` in
`pyproject.toml` pins `files` + `strict` to the same scope, so a bare local
`uv run mypy` mirrors CI. Design:
[`docs/superpowers/specs/2026-07-15-tech-debt-cicd-routine-design.md`](docs/superpowers/specs/2026-07-15-tech-debt-cicd-routine-design.md).
The periodic *assessment* half of that routine (marker/suppression census)
is a separate local tool under `~/tech-debt-work/` — it is not in this repo
and is not a second CI job.
