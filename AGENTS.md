# AGENTS.md

## Cursor Cloud specific instructions

`toolbench` is an offline, standard-library-only Python CLI harness (no web
server, no database daemon, no long-running services). "Running the app" means
invoking the CLI entry points (`passive`, `probe`); "testing
end to end" means the hermetic `pytest` suite plus the strict gate. Standard
commands live in `README.md` (Usage + Quality gate) and `pyproject.toml`;
don't duplicate them here.

Non-obvious notes for this environment:

- **Toolchain:** the project is `uv`-managed and pins `requires-python = ">=3.13"`.
  Run `uv sync` (or let `uv run` sync implicitly) to provision a ≥3.13
  interpreter into `.venv`. Always invoke tools via `uv run …` (e.g.
  `uv run python -m toolbench.passive`); a system `python3` older than 3.13
  will not satisfy the version pin.
- **Quality gate before any PR** (from `README.md`): `uv run ruff check .`,
  `uv run mypy --strict toolbench tests`, and `uv run pytest -q`. Do not use
  `uv run python -m unittest discover tests` as the gate — it silently misses
  module-level `test_*` functions (37 of 220 as of TB-19) and executes
  module-level code, printing report tables to stdout mid-run. The same three
  commands run in CI (`.github/workflows/ci.yml`) on every PR and push to
  `main` (`uv sync --frozen --python 3.13`); the Lattice pre-commit hook is
  orthogonal (event-log integrity, not the gate). Periodic tech-debt
  *assessment* reports live under `~/tech-debt-work/` (local tool; not CI).
  Design: `docs/superpowers/specs/2026-07-15-tech-debt-cicd-routine-design.md`.
- **Lattice board integrity (install the pre-commit hook once per clone):**
  `ln -sf ../../.githooks/pre-commit .git/hooks/pre-commit`. `.git/hooks/` is not
  versioned, so a fresh clone gets the script but not the wiring; linked worktrees
  share the main checkout's hooks and need no separate install. The hook rejects a
  commit whose index holds a `.lattice/tasks/*.json` snapshot with no
  `task_created`-headed log in `.lattice/events/`. **Never create a task by writing
  `.lattice/tasks/*.json` directly — always use `lattice create`.** A hand-written
  snapshot has no event log, and the next ordinary `lattice` mutation appends to the
  missing file, producing a headless log that `lattice rebuild` can never replay and
  `lattice doctor` does not flag; the snapshot silently becomes the task's only copy.
  That happened to TB-15 on 2026-07-09 and went undetected for four days. Do not set
  `core.hooksPath` — it would disable the existing `post-commit`/`post-rewrite` hooks.
- **Running the passive analyzer on real data:** there is no `--root` CLI flag;
  raw scanning defaults to `~/.claude/projects` and treats the first path segment
  under the root as the project (subagent files at `<project>/<session-uuid>/subagents/*.jsonl`
  keep that owning project and set `is_subagent`). To exercise it, drop a
  `*.jsonl` transcript at `~/.claude/projects/<project>/session.jsonl` and run
  `uv run python -m toolbench.passive --agent all --all --index-source raw`.
- **Running the active probe:** `toolbench.probe` refuses to write a report
  (`SeededReportError`) unless you pass `--session <probe.jsonl>` (or
  `--allow-seeded` for the baseline-only table). Fixture sessions under
  `tests/fixtures/*.jsonl` are valid inputs for a quick real run. A hermes
  `--format trace` export raises `NonIsolableTurns` (S30) — use a native
  Claude Code transcript for probes. Probe joins via `ClaudeParser` with
  `keep_raw_input` / `track_turns` (no private Claude walker).
- **Per-run cache-token grouping:** `--run-manifest <run.json>` is a flag on
  `toolbench.passive` (S40), not a separate module. Reader lives in
  `run_manifest.py`. `uv run python -m toolbench.passive --run-manifest
  run.json` works from the repo root. From `~` (cwd hygiene for measuring
  `~/.claude`), invoke by file path as in
  `.claude/skills/cache-token-metrics/SKILL.md` — `-m` fails outside the
  checkout because the package is not installed into the venv. Detached
  checkouts stamp `gitBranch="HEAD"` and are named in the run section, never
  folded into the run total (TB-28).
- **Module split:** aggregation is `reducer.py`, markdown/fingerprint is
  `report.py`, freeze I/O is `freeze.py`, run-manifest I/O is
  `run_manifest.py`; `passive.py` is CLI + orchestration and re-exports the
  historical public symbols. The complex debug probe library is `complex.py`
  (defects, scoring, profile render) + `complex_runner.py` (worktree /
  deps-cache / trial driver) — library only, no CLI yet; fixtures under
  `probes/complex/`, pinned corpora under `corpus/`.
- **Subagent paths:** real layout is
  `<project>/<session-uuid>/subagents/*.jsonl` (TB-29). `--exclude-subagents`
  drops those refs; freeze replay re-derives the flag from the path so a
  stale pre-fix `"is_subagent": false` cannot keep them included.
- **Generated output:** reports land in `reports/`, which is gitignored.
- **Optional external dependencies are not present here and are not needed for the
  gate:** the `agentsview` CLI, real `~/.claude`/Codex transcript roots, and the
  Hermes archive (`~/.hermes` / `$HERMES_HOME`). Fast-suite skips for absent live
  archives are expected (currently 3 skips when hermes/optional live paths are
  missing; hermetic suite is ~606 passing).
- **Complex deps cache (`UnsafeDepsCache`):** `complex_runner._assert_deps_base_safe`
  rejects a replaceable cache leaf *before* `resolve()` (including a dangling
  symlink), then requires FS-root divergence from the corpus, sticky-safe
  ancestors, and a private uid-owned directory under
  `gettempdir()/vendor-cache-<uid>`. Operator notes live in `README.md`
  (Complex debug probe section + troubleshooting).
- **`--index-source auto` mid-listing fallback (TB-38):** a daemon that answers
  the `--limit 1` probe and then fails during pagination (nonzero exit,
  `AgentsViewTimeout`, or schema-invalid listing → `MalformedAgentsViewResponse`
  / `ValueError`) still degrades to raw — the partial agentsview listing is
  discarded and rescanned wholesale, never spliced. "Schema-invalid" covers bad
  JSON and contract failures (`sessions` not a list, row missing non-empty
  `id`/`agent`/`project`, bad `next_cursor`/`total`). The health probe validates
  that same shape. Explicit `agentsview` stays strict for those mid-listing
  failure modes (and for a vanished binary).
- **Sampling disclosure (S41 / TB-33 / TB-35 / TB-34 / TB-37):** `--limit` caps
  total refs in RECENCY order across the whole archive, so each agent lands at a
  different fraction of its own history and an agent whose work is all older than
  the window vanishes entirely. The Agent Breakdown's `sampled` column carries
  each agent's denominator; agents present in the archive but never scanned still
  get a row. The uneven-sampling line apportions the per-agent remainder
  (`total - sampled`) between truncation and attrition from *observed* signals
  only — `limit_truncated` for the window cutting the listing, `SkipRecord`s for
  skips — rather than merely asserting both happened (TB-35): a `--limit` passed
  without biting is not truncation, and a negative remainder is flagged as drift.
  Cross-agent ratios are only comparable when the report emits no uneven-sampling
  line. A zero-match early return still prints the census the run already built
  (TB-34) — a narrow window must not read as an empty archive. Freeze replay
  (manifest v2, TB-37) restores the freeze-time census when present and labels it
  a **historical** denominator; a v1 manifest or a v2 write without a census still
  marks fractions unavailable and names the manifest version.
