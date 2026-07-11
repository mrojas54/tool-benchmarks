# TB-25: _apply_date_range drops session_cache_read_tokens, undercounting the S32 cache caveat under --date-from/--date-to

Found during TB-24 code review. `_apply_date_range` in toolbench/passive.py rebuilds ParseResult by enumerating fields (calls, malformed, unjoinable) and omits `session_cache_read_tokens`, so it silently resets to None whenever a date range is active.

IMPACT: under --date-from/--date-to, every session's session-grain cache signal (S32) is dropped, so the Agent Breakdown cache caveat undercounts sessions_with_cache_data / sessions_with_cache_hit. Pre-existing; not introduced by TB-24.

FIX: use dataclasses.replace(result, calls=kept) so all non-filtered fields survive automatically, rather than re-listing fields that must be kept in sync by hand.

DECIDE: also settle whether a session whose calls are all filtered out should still contribute its cache stat (arguably yes -- the session was still measured).

## Resolution (PR #32)

- FIX applied as specified: `_apply_date_range` returns `replace(result, calls=kept)`.
- DECIDE settled **yes**: `dataclasses.replace` preserves `session_cache_read_tokens`
  even when `kept` is empty, so an all-filtered session still contributes its cache
  stat. The new test pins exactly this case.
- RED -> GREEN -> DOCS, one commit each. New test
  `test_session_cache_read_tokens_survives_date_filtering`. 329 pass / 1 skip
  (was 328); ruff + mypy --strict clean.
- Docs: SPEC S32 gains the date-range survival invariant; BUILDPLAN T15 row added.

