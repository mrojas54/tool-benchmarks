Own-reviewer verdict: APPROVE.

Diff scope: toolbench/probe.py (implementation, replaces stub), protocols/active-probes.md (new),
tests/test_probe.py + tests/fixtures/probe_session.jsonl (new), .gitignore (+reports/). Confirmed
zero diff against origin/tb-3-parse in tools/, toolbench/transcript.py, toolbench/sources.py,
toolbench/passive.py (guardrail: no modification to TB-3 parser or sibling modules).

S16: PROBE_SPECS names the 5 real vendored tools/*.py paths (verified on-disk by test);
protocols/active-probes.md lists all 5 relative paths and never lists a reports/ path;
probe output (render_report/main) writes only under reports/ (gitignored, created at run time,
default overridable via --out so tests never touch the real dir).

S17: 10 sentinels (TB_PROBE_<01..05>_{TOOL,BASH}_V2) verified pairwise non-substring + globally
unique by test. find_probe_calls requires BOTH sentinel-in-input AND exact expected tool name
(explicit AND, not merged) -- covered by dedicated near-miss fixtures/tests: right-tool-wrong-
sentinel (toolu_c1) and right-sentinel-wrong-tool (toolu_d1) both correctly produce no match.

S18: build_comparison_table emits one row per probe (5 rows) with tool_tokens/bash_tokens from
ToolCall.tokens when matched, tool_usage_tokens/bash_usage_tokens from ToolCall.usage only when
the assistant turn is isolable (exactly one tool_use block at that timestamp -- usage is per-turn,
not per-call), and falls back to the exact #8376 seeds (search 723/794, find 68/89) per (task, arm)
when an arm is absent. Fixture exercises both the isolable-with-usage case and the non-isolable-
same-usage-object-but-omitted case explicitly.

Deviation (flagged, not a defect): find_probe_calls does its own single-pass raw JSONL scan for
sentinel/tool-name verification, independent of ToolCall -- because ToolCall intentionally drops
raw input/output text in favor of char counts (result_len), so a sentinel has nowhere to live on a
ToolCall. It still calls parse_session/ParseResult for the actual token/usage numbers, joining by
(ts, name). This is a design consequence of TB-3's ToolCall shape, not a TB-3 bug; documented in
the plan file rather than filed against TB-3.

No Critical/Major findings. Minor: (ts, name) join assumes at most one call per (timestamp, tool
name) pair per turn, true for the curated probe fixture/real probe-runner sessions but not a
universal invariant of arbitrary transcripts -- acceptable for this ticket's scope (probe sessions
are purpose-recorded, not arbitrary agent traffic).