# AGENTS.md

## Cursor Cloud specific instructions

`toolbench` is an offline, standard-library-only Python CLI harness (no web
server, no database daemon, no long-running services). "Running the app" means
invoking the two CLI entry points; "testing end to end" means the hermetic
`unittest` suite plus the strict gate. Standard commands live in `README.md`
(Usage + Quality gate) and `pyproject.toml`; don't duplicate them here.

Non-obvious notes for this environment:

- **Toolchain:** the project is `uv`-managed and pins `requires-python = ">=3.13"`.
  The startup update script runs `uv sync`, which provisions the interpreter
  (currently CPython 3.14, the newest `>=3.13`) into `.venv`. Always invoke tools
  via `uv run …` (e.g. `uv run python -m toolbench.passive`); the system
  `/usr/bin/python3` is 3.12 and will not satisfy the version pin.
- **Quality gate before any PR** (from `README.md`): `uv run ruff check .`,
  `uv run mypy --strict toolbench tests`, and `uv run python -m unittest discover tests`.
  The unittest run's OK/FAIL summary is printed to **stderr**, and some tests emit
  report tables to stdout — redirect stdout to `/dev/null` if you only want the
  pass/fail summary.
- **Running the passive analyzer on real data:** there is no `--root` CLI flag;
  raw scanning defaults to `~/.claude/projects` and treats the first path segment
  under the root as the project. To exercise it, drop a `*.jsonl` transcript at
  `~/.claude/projects/<project>/session.jsonl` and run
  `uv run python -m toolbench.passive --agent all --all --index-source raw`.
- **Running the active probe:** `toolbench.probe` refuses to write a report
  (`SeededReportError`) unless you pass `--session <probe.jsonl>` (or
  `--allow-seeded` for the baseline-only table). Fixture sessions under
  `tests/fixtures/*.jsonl` are valid inputs for a quick real run.
- **Generated output:** reports land in `reports/`, which is gitignored.
- **Optional external dependencies are not present here and are not needed for the
  gate:** the `agentsview` CLI, real `~/.claude`/Codex transcript roots, and the
  Hermes archive (`~/.hermes` / `$HERMES_HOME`). One `tests/test_hermes.py` test
  skips when the Hermes archive is absent (that's the expected 1 skip).
