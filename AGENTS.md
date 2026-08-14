# AGENTS.md

`toolbench` is an offline, stdlib-by-default Python CLI harness. There is no
server or database. Use the `passive`, `probe`, and `worktrees` CLIs; treat the
hermetic test suite plus strict gate as end-to-end coverage. README and
`pyproject.toml` are the source of truth for routine commands.

## Toolchain and gate

- The project is `uv`-managed and requires Python >=3.13. Run tools from this
  repository root via `uv run`; use `uv sync` when explicit provisioning is
  needed. Runtime deps stay empty (stdlib-only by default); the optional
  `tracing` extra adds Laminar (`lmnr`) without changing the default install.
  The `dev` group adds the gate tools plus optional parallel-run tooling
  (`logfire`).
- Before a PR, run `uv run ruff check .`,
  `uv run python -m toolbench.complexity_gate --base origin/main`,
  `uv run mypy --strict src/toolbench tests`, and `uv run pytest -q`. Do not
  substitute `unittest discover`: it misses module-level pytest tests and
  executes module-level report code. A bare `uv run mypy` also mirrors that
  scope via `[tool.mypy]` in `pyproject.toml` (does not descend into `tools/`).
- Optional live dependencies (`agentsview`, Claude/Codex archives, Hermes) are
  not required for the gate; skips for absent live archives are expected (the
  hermetic suite is ~748 passing, with 4 skips when live paths / optional
  tracing deps are missing).
## Repository integrity

- Install the clone-local Lattice guard once with
  `ln -sf ../../.githooks/pre-commit .git/hooks/pre-commit`. Never set
  `core.hooksPath` because existing post-commit/post-rewrite hooks must remain.
- Create tasks only with `lattice create`; never write `.lattice/tasks/*.json`
  directly. Snapshots without a `task_created`-headed event log cannot be
  rebuilt reliably.
- `uv run toolbench worktrees` reports every linked worktree with a verdict and
  its idle age (precedence `LOCKED > DIRTY > UNIQUE-WORK > CLAIMED > SAFE`).
  `--reclaimable-only` narrows it to `SAFE` trees that are idle ≥`IDLE_DAYS`
  (7); a live upstream is a standing `CLAIMED` exemption that never expires.
  `--hook` is a mutually exclusive SessionStart mode (registered in tracked
  `.claude/settings.json`): silent unless something is reclaimable, always
  exit 0, never deletes. Reclaim with the procedure below.
- Reclaim a stale worktree with `git worktree remove <path>` **then**
  `git branch -d <branch>` — the order is required, since git refuses to delete
  a branch a linked worktree holds checked out. Select candidates with
  `uv run toolbench worktrees --reclaimable-only` (full table:
  `uv run toolbench worktrees`). Do not select by grepping
  `%(upstream:track)` emptiness — empty means both "in sync" and "no upstream".
  Do not rely on `commit-commands:clean_gone`: it greps `git branch -v` for the
  literal `[gone]`, but real output is `[origin/<name>: gone]`, so it matches
  nothing here and reports success having removed nothing. Nested agent
  worktrees land under gitignored `.claude/worktrees/` (tracked in
  `.gitignore`, not only `.git/info/exclude`).
- Reports are generated under gitignored `reports/`. Parallel-run artifacts
  under `.bmad-loop/` and `.humanlayer/tasks/` (plus
  `.humanlayer/workspace.local.json`) are also gitignored;
  `.humanlayer/workspace.json` stays committable.

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
  Unreadable / malformed / non-UTF-8 freeze paths, a directory at the path, or
  a write failure are hard stops (`fatal freeze error`, exit 1) — not
  tracebacks (S23 / PR #87).
- `ensure_deps` / `provision_worktree` default to the **packaged** manifest
  (`src/toolbench/corpus/manifest.json`); custom corpora must pass their own
  manifest explicitly so a stale generated `corpus/manifest.json` cannot change
  a trial SHA. `ensure_deps` also pins npm manifest copies and warmup cwd to
  that entry's SHA via `git show` / `git archive` — never corpus `HEAD` (#99).
  After a successful build it stamps `.manifest-sha`; on the next call a
  missing or drifted stamp wipes cached dep trees and rebuilds (#102).
- `complex_runner._assert_deps_base_safe` rejects a replaceable deps-cache leaf
  (including a dangling symlink) *before* `resolve()`, then requires FS-root
  divergence from the corpus, sticky-safe ancestors, and a private uid-owned
  directory under `gettempdir()/vendor-cache-<uid>`.

## Module ownership

Aggregation is in `reducer.py`; markdown/fingerprints in `report.py`
(per-section `_render_*` helpers); freeze I/O in `freeze.py`; run-manifest I/O
in `run_manifest.py`. `passive.py` owns CLI orchestration (`_resolve_corpus`
for replay-vs-discover) and compatibility re-exports. The complex debug probe
is `complex.py` (defects, scoring, profile render) plus `shell_safety.py`
(arm / read-scope audits; re-exported from `complex`) plus `complex_runner.py`
(worktree, deps cache, trial driver) — library only, no CLI yet.
`worktrees.py` owns the linked-worktree inventory CLI (`classify` /
`reclaimable` / `--hook`); it prints only and never removes a tree or ref.
`complexity_gate.py` owns the cyclomatic-complexity regression check
(`compare_complexity` / `evaluate_repository`); invoke via
`python -m toolbench.complexity_gate`, not the console script.
Opt-in Laminar observability lives in `observability/setup_tracing.py`
(`setup_tracing` / `load_laminar`) and `tracing.py` (`run_traced`); `cli.py`
wraps real console processes only when `argv is None` and
`TOOLBENCH_TRACING=1`, and skips tracing for `worktrees --hook`.
