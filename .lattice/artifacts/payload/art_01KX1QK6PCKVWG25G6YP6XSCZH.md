ruff check .: All checks passed! (exit 0)

mypy --strict toolbench tests: Success: no issues found in 6 source files (exit 0)

python -m unittest discover tests: Ran 21 tests in 0.001s — OK (exit 0)
21/21 passing: 4 ResultLenTests + 3 ToolCallTests (TB-2, unmodified) + 7
ResultIdPayloadTests + 7 ParseSessionTests (TB-3, new).

Commits validated: 0139717, dbec668, 77e91c4 (on top of origin/tb-2-scaffold @ dd96d91).