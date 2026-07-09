## Own-reviewer review — TB-4 (sources.py)

**Scope:** origin/tb-2-scaffold..HEAD (1 commit, 2 new files: toolbench/sources.py, tests/test_sources.py)

**Verdict: APPROVE**

### Coverage vs SPEC
- S7 (iter_session_files): raises FileNotFoundError on missing root; filters by parent-dir-name substring and mtime; tested (4 cases). ✓
- S8 (iter_agentsview_sessions): cursor pagination confirmed against real CLI shape (`agentsview session list --json --limit N` — fields sessions/next_cursor/total sampled live); loop terminates on empty-string or absent next_cursor; argv assembly (agent/project/date-from/limit/cursor) asserted exactly. ✓
- S9 (open_session_jsonl): filesystem path vs `agentsview session export <id>` (confirmed live: export streams raw JSONL directly, no --json wrapping needed). ✓
- S10 (index-source policy): raw/agentsview/strict, auto-with-fallback-and-recorded-reason on both missing-binary and nonzero-exit; unknown value raises ValueError. ✓
- S24 (fake-runner seam): FakeRunner injected via `runner` param at iter_agentsview_sessions/open_session_jsonl boundary — no subprocess monkeypatching, no ~/.claude or daemon access in the fast suite. ✓

### Findings
- **Minor (non-blocking):** `iter_sessions(index_source="auto", ...)` issues a throwaway probe call (`session list --limit 1`) to decide availability, then `iter_agentsview_sessions` re-issues the real first-page call from scratch — one extra live CLI round-trip per "auto" invocation when AgentsView is healthy. This was a deliberate plan tradeoff (probe-before-commit, since the fallback decision must be made before returning the iterator+reason tuple) and doesn't affect correctness or the hermetic test suite. Flagging for T4/T6 awareness in case a future perf pass wants to fold the probe's first page into the real listing.

### Not touched (as scoped)
- toolbench/__init__.py — no edits, avoiding the flagged shared-file collision with TB-3.
- transcript.py, passive.py, probe.py — untouched.

No Critical or Major findings. Proceeding to validate.