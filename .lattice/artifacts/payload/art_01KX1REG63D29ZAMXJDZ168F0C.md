VERDICT: APPROVE (no Critical/Major findings)

Scope reviewed: toolbench/passive.py (new, 387 lines) + tests/test_passive.py
(new, 79 tests) against origin/integration/substrate..HEAD (2 commits).

S11 no-corpus-list invariant (special attention per plan): CONFIRMED.
Reducer/AgentStats/ToolStats/InefficiencyCounters fields are all scalar
counters or dict[key, Stats] — none typed list[ToolCall]. Reducer.absorb()
iterates the per-session ParseResult.calls list (unavoidable — that's what
parse_session already returns per session) purely to fold into counters;
the list is a local loop variable, never assigned to self. Verified both
by reading passive.py:63-124 and structurally via
tests/test_passive.py::ReducerNoCorpusListTests, which introspects
dataclasses.fields() on all four aggregate types and asserts "ToolCall"
never appears in any field's type annotation — this fails loudly if a
future edit reintroduces a corpus-wide list.

Four report sections in order (S14): CONFIRMED via render_report() reading
top-to-bottom (Agent Breakdown -> Tool Leaderboard -> Inefficiency
Callouts -> Summary) and RenderReportTests::test_four_sections_present_in_order
asserting header index() calls are already sorted.

Provenance (S15): CONFIRMED — index source, sessions scanned, calls
joined, malformed count, subagents-included flag, AgentsView fallback
reason, skipped roots, and the fixed "--since is file-mtime based" note
all present in the Summary section, each tested individually.

Ranking on context-cost (S19): CONFIRMED — Tool Leaderboard sort key is
strictly ToolStats.output_tokens (passive.py:302); cache_hits is tracked
and displayed but never enters the sort key; failures/no_result/churn
land only in InefficiencyCounters + the per-tool errors column.
test_leaderboard_ranked_by_output_tokens_not_call_count_or_cache exercises
a case where the higher-call-count, no-cache tool would sort first under
a wrong metric and confirms it doesn't.

Exit-code contract (S23): all three branches covered by tests — strict
raw missing root -> 1, strict agentsview nonzero exit -> 1 (added during
this review pass to close a gap), empty selection -> 0, and auto with
agentsview unavailable + raw root missing -> 0 with skipped-root note.

Findings (non-blocking):
- Minor/nit: `skipped_roots` (list[str]) is reused in main() to also record
  per-session parse failures ("{session_id}: {exc}"), not just root-level
  discovery skips. Naming is slightly overloaded but behavior is correct
  and tested; not fixing since a rename would touch several call sites for
  zero behavior change.
- Minor: CLI's `--all` flag is accepted (for the documented mutually-
  exclusive-with---project shape SPEC lists) but its value isn't read
  directly — all_projects is derived from `project is None`. Intentional;
  simplest correct implementation of "default scope --agent all --all".
- Known, documented (not a bug): `--agent` filtering is a no-op under
  `--index-source raw` because sources._raw_session_refs doesn't accept an
  agent parameter at all (pre-existing in sources.py, out of scope per
  guardrails — noted in the plan file's Deviations section).

No changes required before validate/PR.