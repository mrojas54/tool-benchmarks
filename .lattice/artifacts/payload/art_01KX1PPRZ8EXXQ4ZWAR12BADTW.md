## Strict gate — all green

```
=== uv run ruff check . ===
All checks passed!

=== uv run mypy --strict toolbench tests ===
Success: no issues found in 6 source files

=== uv run python -m unittest discover tests ===
.......
----------------------------------------------------------------------
Ran 7 tests in 0.000s

OK

=== uv run python -m toolbench.passive ===
toolbench.passive: not yet implemented
exit=0

=== uv run python -m toolbench.probe ===
toolbench.probe: not yet implemented
exit=0
```