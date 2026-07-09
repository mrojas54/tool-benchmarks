Strict gate (S22) -- run over full assembled tree, base origin/integration/full @ 51f7f72

$ uv run ruff check .
All checks passed!
EXIT: 0

$ uv run mypy --strict toolbench tests
Success: no issues found in 10 source files
EXIT: 0

$ uv run python -m unittest discover tests
Ran 93 tests in 0.018s
OK
EXIT: 0

All three green. Known nit (not a failure, no fix applied -- out of scope for docs+gate ticket): one probe test prints the S18 comparison table to stdout during the run.