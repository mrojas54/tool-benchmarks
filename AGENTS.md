# AGENTS.md

`toolbench` is an offline, standard-library-only Python CLI harness. There is no
server or database. Use the `passive` and `probe` CLIs; treat the hermetic test
suite plus strict gate as end-to-end coverage. README and `pyproject.toml` are
the source of truth for routine commands.

## Toolchain and gate

- The project is `uv`-managed and requires Python >=3.13. Run tools from this
  repository root via `uv run`; use `uv sync` when explicit provisioning is
  needed.
- Before a PR, run `uv run ruff check .`,
  `uv run mypy --strict src/toolbench tests`, and `uv run pytest -q`. Do not
  substitute `unittest discover`: it misses module-level pytest tests and
  executes module-level report code.
- Optional live dependencies (`agentsview`, Claude/Codex archives, Hermes) are
  not required for the gate; skips for absent live archives are expected (the
  hermetic suite is ~617 passing, with 3 skips when live paths are missing).

## Repository integrity

- Install the clone-local Lattice guard once with
  `ln -sf ../../.githooks/pre-commit .git/hooks/pre-commit`. Never set
  `core.hooksPath` because existing post-commit/post-rewrite hooks must remain.
- Create tasks only with `lattice create`; never write `.lattice/tasks/*.json`
  directly. Snapshots without a `task_created`-headed event log cannot be
  rebuilt reliably.
- `uv run toolbench worktrees` reports every linked worktree with a verdict and
  its idle age; `--reclaimable-only` narrows it to trees that are clean,
  unlocked, fully reachable, unclaimed by a live upstream, and idle past the
  threshold. A `SessionStart` hook (`.claude/settings.json`) runs it and stays
  silent unless something is reclaimable. It never deletes anything — reclaim
  with the procedure below.
- Reclaim a stale worktree with `git worktree remove <path>` **then**
  `git branch -d <branch>` — the order is required, since git refuses to delete
  a branch a linked worktree holds checked out. Select candidates with
  `git for-each-ref --format='%(refname:short)|%(upstream:track)|%(worktreepath)' refs/heads/`.
  Do not rely on `commit-commands:clean_gone`: it greps `git branch -v` for the
  literal `[gone]`, but real output is `[origin/<name>: gone]`, so it matches
  nothing here and reports success having removed nothing.
- Reports are generated under gitignored `reports/`.

## Analyzer and probe constraints

- Raw passive scans default to `~/.claude/projects`; the first path segment is
  the project. Subagents live at
  `<project>/<session-uuid>/subagents/*.jsonl`, retain the owning project, and
  can be dropped with `--exclude-subagents`.
- `toolbench.probe` requires `--session <probe.jsonl>` before writing a report;
  use `--allow-seeded` only for a baseline-only table. Fixtures are valid smoke
  inputs. Hermes `--format trace` is not turn-isolable; use native Claude
  transcripts for probes.
- `--run-manifest <run.json>` belongs to `toolbench.passive`. Outside the
  checkout, invoke the installed console script with
  `uv run --project ~/tool-benchmarks toolbench passive --run-manifest run.json`.
  Detached checkouts remain separate `HEAD` runs rather than joining totals.
- `--limit` is a global recency cap, so per-agent sample fractions may differ or
  vanish. Treat cross-agent ratios as comparable only when no uneven-sampling
  warning is emitted. Attribution must use observed truncation/skip signals;
  flag negative remainders as drift.
- Freeze replay (manifest v2, TB-37) restores the freeze-time census as a
  **historical** denominator only when `census_includes_subagents` is present
  and matches the replay's `--exclude-subagents` choice. A v1 manifest, a v2
  write without a census, a legacy v2 census missing that key, or a mismatched
  subagent filter each leave fractions unavailable and name the reason.
- `ensure_deps` / `provision_worktree` default to the **packaged** manifest
  (`src/toolbench/corpus/manifest.json`); custom corpora must pass their own
  manifest explicitly so a stale generated `corpus/manifest.json` cannot change
  a trial SHA.
- `complex_runner._assert_deps_base_safe` rejects a replaceable deps-cache leaf
  (including a dangling symlink) *before* `resolve()`, then requires FS-root
  divergence from the corpus, sticky-safe ancestors, and a private uid-owned
  directory under `gettempdir()/vendor-cache-<uid>`.

## Module ownership

Aggregation is in `reducer.py`; markdown/fingerprints in `report.py`; freeze I/O
in `freeze.py`; run-manifest I/O in `run_manifest.py`. `passive.py` owns CLI
orchestration and compatibility re-exports. The complex debug probe is
`complex.py` (defects, scoring, profile render) plus `complex_runner.py`
(worktree, deps cache, trial driver) — library only, no CLI yet.
