# TB-19: unittest discover, the documented test command, silently skips 37 of 220 tests

EVALUATION.md defines the fast harness command as `uv run python -m unittest discover tests`, and BUILDPLAN calls it the delegators inner-loop clock. It collects only unittest.TestCase methods.

MEASURED (ast walk over tests/test_*.py, 2026-07-10):

  in-TestCase methods : 183   <- what `unittest discover` runs
  module-level test fns:  37   <- invisible to it; pytest collects them
  total               : 220

Breakdown of the 37 invisible tests:
  test_adapters.py : 14
  test_sources.py  :  7
  test_parsers.py  :  6
  test_registry.py :  6
  test_passive.py  :  4

Confirmed by running both: `unittest discover` reports "Ran 183 tests"; `pytest -q` reports 220.

WHY IT MATTERS. 17% of the suite never runs under the documented gate. The 14 in test_adapters.py are precisely the module-level assertions the TB-18 design flags as needing to be tightened from `isinstance(parser, ClaudeParser)` to `type(parser) is ClaudeParser` once HermesTraceParser subclasses ClaudeParser. A weakened assertion in a test the gate does not execute fails silently twice over.

FIX (pick one, do not do both silently):
  a. Change the documented `test` command to `uv run pytest -q`. Cheapest; pytest is already a dev dependency.
  b. Convert the 37 module-level functions into TestCase methods, keeping unittest as the gate.

Option (a) also fixes the TOOLBENCH_LIVE gate uniformly. Option (b) preserves the "pure stdlib runtime" posture of S20 for the test harness too.

ACCEPTANCE. The documented harness command executes all 220 tests, and a test added as a module-level function cannot silently escape the gate.
