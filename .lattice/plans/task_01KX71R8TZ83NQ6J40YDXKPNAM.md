# TB-25: _apply_date_range drops session_cache_read_tokens, undercounting the S32 cache caveat under --date-from/--date-to

Found during TB-24 code review. `_apply_date_range` in toolbench/passive.py rebuilds ParseResult by enumerating fields (calls, malformed, unjoinable) and omits `session_cache_read_tokens`, so it silently resets to None whenever a date range is active.

IMPACT: under --date-from/--date-to, every session's session-grain cache signal (S32) is dropped, so the Agent Breakdown cache caveat undercounts sessions_with_cache_data / sessions_with_cache_hit. Pre-existing; not introduced by TB-24.

FIX: use dataclasses.replace(result, calls=kept) so all non-filtered fields survive automatically, rather than re-listing fields that must be kept in sync by hand.

DECIDE: also settle whether a session whose calls are all filtered out should still contribute its cache stat (arguably yes -- the session was still measured).
