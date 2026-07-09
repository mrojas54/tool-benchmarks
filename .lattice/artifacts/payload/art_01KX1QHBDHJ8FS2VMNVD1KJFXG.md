Verdict: PASS-WITH-NITS (nit fixed before landing)

Scope: git log origin/tb-2-scaffold..HEAD (2 commits: 0139717, dbec668) touching
toolbench/transcript.py, tests/test_transcript.py, tests/fixtures/sample.jsonl.

Checked:
- S1 join-key: _result_id checks block-local tool_use_id first, falls back to
  top-level toolUseID. Both locations exercised by distinct fixtures (fixture 1
  top-level-only via toolu_001/Bash; fixtures 2-3 block-local via toolu_002/mcp
  and toolu_003/Read).
- S2 payload precedence: _result_payload prefers block-local content over
  top-level toolUseResult. Fixture 3 (Read) is the flagged de-risking case:
  top-level toolUseResult carries a deliberately different/stale payload than
  block-local content for the SAME id, so the precedence test cannot pass by
  accident. Verified block_local wins and result_source is recorded correctly.
- S5 malformed: truncated JSON line in fixture increments malformed=1, never
  raises, no ToolCall produced. Blank-line handling also present (skipped,
  uncounted).
- S6 interrupted: Write tool_use (toolu_004) has no matching result anywhere
  in the fixture; confirmed kept with output_chars=0, no_result=True, not
  dropped.
- ToolCall additive-only: no_result/result_source both default so TB-2 pre-
  existing ToolCallTests (unmodified) still pass — verified in full suite run.
- Scope discipline: sources.py, passive.py, probe.py, toolbench/__init__.py
  untouched.

Nit (file:line) — toolbench/transcript.py:116-121: duration_ms hardcoded None
in two ToolCall construction sites with no explanation of why. FIXED in
dbec668 by adding one sentence to the parse_session docstring (raw Claude
Code JSONL carries no per-tool-call duration field).

No Critical/Major findings. Proceeding to validate.