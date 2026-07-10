# TB-20: hermes sessions report 100% cache-miss: session-grain cache_read_tokens is never consulted

Filed out of TB-18, which named this as an out-of-scope opportunity. Distinct from TB-18: that ticket makes the absent per-call usage channel *legible* (renders `n/a` instead of `no`). This ticket asks whether a real, populated cache signal at session grain should be used at all.

`_is_cache_hit(usage)` (passive.py:174) consults only per-call `message.usage`. hermes has none — not in the SQLite adapter (ABSENT_BY_SCHEMA) and not in `--format trace` (ABSENT_BY_EXPORT). So every hermes call counts as a cache MISS today, from both paths.

But hermes does record cache data, at SESSION grain.

MEASURED against the live archive (2026-07-10, read-only, all four databases — including `aphrodite-mood`, which was unreadable until the WAL fix in TB-18 Task 0 landed):

  profile              sessions  cache_read>0   messages  token_count non-null
  .hermes                   795           721       6781                     0
  aphrodite-mood             34            29       2006                     0
  light-mood                 28            20        746                     0
  tech-interviewing           6             6        644                     0
  TOTAL                     863           776      10177                     0

776 / 863 sessions (90%) carry `cache_read_tokens > 0`. `messages.token_count` is null in all 10,177 rows, confirming the grain is session, not message.

THE HARD PART is attribution, not availability. A session-grain figure cannot be attributed to an individual tool call without inventing a denominator. Do NOT divide by tool_call_count and report a per-call rate — that fabricates precision the data does not have, which is the same class of error TB-18 exists to fix.

CANDIDATE SHAPES (not chosen):
  a. Report cache as a session-grain caveat line for hermes, alongside the per-call flag for claude, and never mix the two in one column.
  b. Extend UsageProvenance with a PRESENT_AT_SESSION_GRAIN member so the render rule can say "measured, but not at this grain".
  c. Leave `n/a` (TB-18 outcome) and document that hermes cache data exists but is out-of-grain for a per-call report.

Depends on TB-18 landing first: UsageProvenance and the four-case render rule are the seam this would extend.

ACCEPTANCE. Either hermes cache data reaches the report at a grain it actually has, or the report states that session-grain cache data exists and is deliberately not attributed per call. What must not survive: a report implying hermes achieved a 0% cache-hit rate.
