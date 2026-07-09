Validation gate — all green.

$ uv run ruff check .
All checks passed!

$ uv run mypy --strict toolbench tests
Success: no issues found in 9 source files

$ uv run python -m unittest discover tests
Ran 79 tests in 0.014s
OK
(40 pre-existing from TB-3/TB-4 + 39 new for TB-5's toolbench/passive.py)

$ uv run python -m toolbench.passive --help
exit code 0, usage text shows all S12 flags: --agent, --all/--project,
--since, --date-from, --date-to, --out, --limit, --exclude-subagents,
--index-source {auto,agentsview,raw}, --verbose.

No deviations from the strict gate. Full suite remains hermetic (no real
~/.claude access, no daemon) per BUILDPLAN.md's test split.