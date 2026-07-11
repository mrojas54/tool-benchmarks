# AGENTS.md

## Cursor Cloud specific instructions

`toolbench` is an offline, standard-library-only Python CLI harness (no web
server, no database daemon, no long-running services). "Running the app" means
invoking the CLI entry points (`passive`, `probe`, `cache_tokens`); "testing
end to end" means the hermetic `pytest` suite plus the strict gate. Standard
commands live in `README.md` (Usage + Quality gate) and `pyproject.toml`;
don't duplicate them here.

Non-obvious notes for this environment:

- **Toolchain:** the project is `uv`-managed and pins `requires-python = ">=3.13"`.
  The startup update script runs `uv sync`, which provisions the interpreter
  (currently CPython 3.14, the newest `>=3.13`) into `.venv`. Always invoke tools
  via `uv run …` (e.g. `uv run python -m toolbench.passive`); the system
  `/usr/bin/python3` is 3.12 and will not satisfy the version pin.
- **Quality gate before any PR** (from `README.md`): `uv run ruff check .`,
  `uv run mypy --strict toolbench tests`, and `uv run pytest -q`. Do not use
  `uv run python -m unittest discover tests` as the gate — it silently misses
  module-level `test_*` functions (37 of 220 as of TB-19) and executes
  module-level code, printing report tables to stdout mid-run.
- **Running the passive analyzer on real data:** there is no `--root` CLI flag;
  raw scanning defaults to `~/.claude/projects` and treats the first path segment
  under the root as the project (subagent files at `<project>/subagents/*.jsonl`
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
- **Cache-token façade:** `uv run python -m toolbench.cache_tokens` works from
  the repo root. From `~` (cwd hygiene for measuring `~/.claude`), invoke by
  file path as in `.claude/skills/cache-token-metrics/SKILL.md` — `-m` fails
  outside the checkout because the package is not installed into the venv.
- **Module split:** aggregation is `reducer.py`, markdown/fingerprint is
  `report.py`, freeze I/O is `freeze.py`; `passive.py` is CLI + orchestration
  and re-exports the historical public symbols.
- **Generated output:** reports land in `reports/`, which is gitignored.
- **Optional external dependencies are not present here and are not needed for the
  gate:** the `agentsview` CLI, real `~/.claude`/Codex transcript roots, and the
  Hermes archive (`~/.hermes` / `$HERMES_HOME`). Fast-suite skips for absent live
  archives are expected (currently 2 skips when hermes/live paths are missing).
