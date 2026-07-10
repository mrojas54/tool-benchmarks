# TB-18 Own-Reviewer Code Review (Tasks 3-6, run-2)

**Verdict: PASS**

Scope: full PR diff, origin/main (28b6187) -> HEAD (5c74901). Focused scrutiny
on Tasks 3-6 (this run's work: toolbench/passive.py, toolbench/probe.py,
tests/test_passive.py, tests/test_probe.py, 5 probe fixtures, README.md);
spot-checked Tasks 0-2 (toolbench/hermes.py, parsers.py, adapters.py,
transcript.py) already landed and previously reviewed.

## Findings

No Critical or Major findings.

**Minor (1):**
- `toolbench/passive.py` render_report cache_note branch: trailing-space
  alignment on the inline comments (`# never measurable` etc.) is cosmetic
  only; ruff does not flag it and it does not affect behavior. No action.

**Verified correct, called out because they are easy to get wrong:**
- `probe.py::_scan_tool_use_blocks` now routes through `detect_parser(handle)`
  before iterating. Confirmed no import cycle empirically (full suite green,
  no ImportError) and by inspection: probe -> adapters -> parsers ->
  transcript, nothing under toolbench/ imports probe.
- `HermesTraceParser._provenance` is reached via polymorphic dispatch
  (`self._provenance(usage)` inside `ClaudeParser.parse`), not a direct call —
  confirmed `type(self)` resolution is correct via
  `test_hermes_trace_parses_cleanly_and_stamps_absent_by_export`.
- `ToolStats.usage_missing == calls` (all-missing -> "n/a") only evaluates on
  rows that exist, and rows are only created in `absorb()` after at least one
  call, so the `calls == 0` degenerate case (which would also read "n/a")
  never renders in practice.
- S19 (cache flag never affects ranking) holds: `ranked` sorts strictly by
  `output_tokens`; `usage_missing`/`cache_hits` are display-only.
- `_turn_key`'s `NonIsolableTurns` message contains "trace" only on the
  dispatch-level guard, not the invariant guard — matches
  `test_scan_refuses_a_trace_export_at_dispatch`'s assertion, which targets
  the dispatch guard specifically.

## Deviations from the plan (already flagged in commit messages / ticket comments)

1. `render_report()` gained 5 required kwargs after this plan was drafted;
   Task 3's test helper was updated to pass them (RED commit 5d20a2f).
2. SPEC.md S29/S30 already landed pre-Task-6 (commit 305d7d8, Phase 0
   intake); Task 6 Step 1 was a no-op, skipped (plan amendment 4cfd5d1).
3. Test-count baseline drifted from the plan's stated cumulative figures
   (232 passed/1 skipped at Task 2 close, not 231) due to the
   hermetic-suite fix (5baeca1, predates this plan). Actual counts reported
   at each gate instead.

## Gate status at review time

ruff: clean. mypy --strict: 38 errors, exactly the stated baseline, zero new.
pytest: 247 passed, 1 skipped, 3 subtests passed.