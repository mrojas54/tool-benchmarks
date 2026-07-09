## Validation — TB-4 (sources.py)

=== ruff check . ===
All checks passed!

=== mypy --strict toolbench tests ===
Success: no issues found in 8 source files

=== python -m unittest discover tests ===
..........................
----------------------------------------------------------------------
Ran 26 tests in 0.008s

OK

All green. Fully hermetic (fake agentsview runner, no daemon or ~/.claude access).