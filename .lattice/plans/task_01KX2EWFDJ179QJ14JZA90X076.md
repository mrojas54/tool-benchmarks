# TB-11: agentsview session export returns the whole SQLite archive for every hermes session

Found while fixing TB-10. Upstream AgentsView defect; toolbench already defends against it (NonTranscriptExport), but every hermes session yields ZERO tool calls as a result.

REPRO:
  agentsview session export hermes:cron_2d647784731c_20260708_150044 | head -c 16
  -> 'SQLite format 3\0'   (returncode 0, empty stderr)

EXPECTED: JSONL transcript for that one session, as every other agent adapter returns.

EVIDENCE (2026-07-08, live archive, 500-session page):

  agent       n    export rc   payload
  claude      294  0           JSONL      (1.3 MB)
  codex       12   0           JSONL      (3.3 MB)
  cowork      79   0           JSONL      (0.3 MB)
  cursor      1    0           JSONL      (17 KB)
  hermes      29   0           SQLite DB  (37,175,296 bytes)
  claude-ai   85   1           empty      (fails cleanly; separate issue, not this ticket)

So the defect is scoped to the hermes adapter, and to ALL 29 hermes sessions -- not just the cron ones, as TB-10 originally assumed.

THE ID IS VALIDATED, THEN IGNORED. Three different hermes session ids return
byte-identical payloads:

  hermes:cron_2d647784731c_20260708_150044  37175296 bytes  sha256=53ac5769ad225157
  hermes:cron_2d647784731c_20260708_050027  37175296 bytes  sha256=53ac5769ad225157
  hermes:cron_1ba0e70d34fd_20260707_114622  37175296 bytes  sha256=53ac5769ad225157

while a bogus id is rejected properly:
  agentsview session export hermes:does_not_exist_at_all
  -> rc=1, 'fatal: session not in local archive: ...'

The export path therefore resolves the session, then streams the backing store
instead of the resolved session's messages. Tables in the dump (messages,
messages_fts_*, compression_locks) confirm it is hermes's whole message archive.

IMPACT:
- Correctness: hermes tool-call data is entirely unreachable. 29 of 500 sessions
  contribute 0 calls to every report; hermes never appears in the agent breakdown.
- Cost: a full run reads and discards 29 x 37 MB = ~1.08 GB, the SAME database 29
  times over.
- Blast radius beyond toolbench: any consumer trusting rc=0 + the documented JSONL
  contract gets a database. A strict UTF-8 reader crashes on it (that WAS TB-10).

WHY rc=0 IS THE REAL BUG: a nonzero exit would have degraded gracefully in every
consumer. Returning success with an off-contract payload is what turned a missing
adapter feature into a crash in ours.

WHAT TOOLBENCH ALREADY DOES (TB-10, PR #10): sniffs the first 8192 bytes for a NUL
(impossible in JSONL, present at offset 15 of the SQLite header) and raises
NonTranscriptExport, demoting the session to skipped_roots. The run completes and
names the skipped sessions. This ticket is NOT about that guard.

IN SCOPE:
- Report upstream to AgentsView with the repro above.
- Decide whether toolbench should read hermes sessions directly from the SQLite
  archive (messages table) rather than via , since the data plainly
  exists and is currently 100% unreachable.

OUT OF SCOPE:
- The claude-ai rc=1 empty export. It fails cleanly and is a separate question.
- Removing the NUL sniff. It should stay regardless of any upstream fix.
