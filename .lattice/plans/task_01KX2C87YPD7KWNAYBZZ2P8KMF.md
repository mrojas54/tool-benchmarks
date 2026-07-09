# TB-9 — Inefficiency callouts lack denominators and attribution

## Approach
TDD, RED -> GREEN -> DOCS, one commit per phase.

1. RED — assert in `tests/test_passive.py` that:
   - `InefficiencyCounters` carries `failures_by_tool`, `oversized_by_tool`,
     `churn_by_tool`, `subagent_by_tool`, accumulating across sessions;
   - `render_report` emits `N of M calls (P%)` per callout;
   - the worst tool is named as `; top: <tool> (n)`;
   - a zero count omits the top-offender clause;
   - ties break alphabetically (determinism).
2. GREEN — add the `*_by_tool` dicts, `_bump`, `_top_offender`, `_callout`;
   rewrite the Inefficiency Callouts block in `render_report`.
3. DOCS — promote CLOSEOUT smoke row 4 PARTIAL -> PASS, record TB-9.

## Constraints
- No SPEC change: S14 fixes *which* callouts appear, not their formatting.
- Verify against the live CLI (`--index-source raw`), not fixtures alone —
  per the retrospective finding that fixture-only tests missed TB-8.
- Gates: ruff check, mypy --strict, full pytest.

## Out of scope
- `ruff format` drift in 6 pre-existing files (untouched by this ticket).
- The unhandled AgentsView-daemon traceback seen under `--index-source auto`.
