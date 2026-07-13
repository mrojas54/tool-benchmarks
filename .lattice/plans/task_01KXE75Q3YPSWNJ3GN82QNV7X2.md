# TB-33: Agent Breakdown compares agents across undisclosed sampling fractions, and silently omits agents the window never reached

TB-30 fixed the HIDDEN per-agent sampling skew (agentsview's default exclusions). A second, independent skew remains, from a different cause, and the report still does not disclose it.

WITH `--agent all` AND A `--limit`, sessions are taken in RECENCY order across the whole
archive. Agents do not produce sessions at the same rate, so the window's per-agent
composition has no relation to each agent's share of the archive -- and each agent ends up
sampled at a wildly different fraction of its own history.

MEASURED (live archive, all three --include-* flags, 2026-07-13):

  archive totals: claude 8591 | hermes 978 | codex 175 | cursor 73

  --agent all --limit 200  ->  fraction of that agent's OWN sessions in the window:
    claude   141 scanned   1.64%
    hermes    19 scanned   1.94%
    codex     33 scanned  18.86%   <- 11.5x claude's fraction
    cursor     0 scanned   0.00%   <- never appears at ANY limit tested (50/200/500)

THE WORSE HALF: AN AGENT CAN VANISH WITH NO NOTE. The rendered Agent Breakdown for
`--agent all --all --limit 200` is:

    | agent  | sessions | calls | output_tokens | ... |
    | claude |      141 |  1401 |        587033 |     |
    | codex  |       33 |   369 |        721451 |     |
    | cowork |        6 |   240 |        191185 |     |
    | hermes |       19 |   115 |          1897 |     |

cursor is simply ABSENT. A reader sees a four-agent comparison and has no way to learn that
a fifth agent exists in the archive with 73 sessions, none of which the window reached.
Nothing in the report distinguishes 'this agent has no sessions' from 'this agent has
sessions we did not look at'.

WHY THE TABLE IS NOT COMPARABLE AS RENDERED: rows sit side by side as if they were like for
like, but each is a different sampling fraction of a different-sized population. codex's 369
calls come from 18.9% of its archive; claude's 1401 from 1.6% of its. Any ratio a reader
forms across rows -- calls/session, tokens/call, error rate -- silently mixes sampling
depth into the comparison. Same failure MODE as TB-30, different cause: TB-30 was a hidden
default exclusion, this is visible truncation that is nonetheless never surfaced.

DISTINCT FROM TB-30 (which is fixed, PR #53): every agent is now DISCOVERABLE at 100% of its
archive. This ticket is purely about what `--limit` then does to that population, and about
the report's silence on it.

FIX (proposed):
  1. Carry per-agent discovered/total through discovery. agentsview's session-list payload
     already returns a `total` key per query, so the denominator is one extra scoped call
     per agent -- no extra pagination.
  2. Add a per-agent sampling column or Summary block, e.g.
       - claude: 141 of 8591 sessions (1.6% of archive)
       - codex:   33 of  175 sessions (18.9% of archive)
       - cursor:   0 of   73 sessions (0% -- present in the archive, not reached by --limit)
  3. Name agents that exist in the archive but scanned zero sessions, rather than dropping
     their row. A silently missing row is the TB-30 lesson repeating: absence must be stated,
     never inferred.
  4. Consider warning when the spread in sampling fraction across agents exceeds some factor,
     since that is the condition under which cross-agent numbers stop being comparable.

Found while fixing TB-30/TB-31 (PR #53); split out rather than folded in, to keep that PR to
the corpus-restoration + subagent-filter change the tickets described.
