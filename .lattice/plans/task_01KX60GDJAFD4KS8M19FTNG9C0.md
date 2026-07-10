# TB-22: Corpus is non-reproducible between runs: observer transcripts age out of a 30-day window mid-scan

Two `passive --agent all --all` runs 18 minutes apart, same code, same flags, disagreed:

  run 1 (08:00)   sessions 1752   calls 26401   claude sessions 1289
  run 2 (08:18)   sessions 1746   calls 26411   claude sessions 1283

`cowork` and `hermes` reproduced byte-for-byte. Only `claude` moved. Root cause, established by diffing the two skip sets:

  claude scanned            1289 -> 1283   (-6)
  claude skipped            519  -> 526    (+7)
  claude discovered         1808 -> 1809   (+1)
  -7 + 1 = -6.  Zero sessions moved the other way.

The seven newly-skipped sessions all failed identically:

  agentsview session export failed (1): fatal: source file not found:
  /Users/.../.claude/projects/-Users-michellerojas--claude-mem-observer-sessions/<id>.jsonl

All seven are claude-mem OBSERVER transcripts, and all seven are now absent from disk. That directory holds 3195 files whose oldest surviving mtime is 2026-06-10T08:17:35 -- 30.009 days old at time of measurement. Six more files were already past the cutoff, awaiting the next prune. A ~30-day sliding retention window deletes observer transcripts while AgentsView keeps indexing them; the export then fails on a path that no longer exists.

So the corpus is not merely appended-to while we scan it (the live session's own +10 calls). Its TAIL IS BEING DELETED, on a sliding window, at roughly the cadence of a re-run.

CONSEQUENCE. Two reports are not diffable. A delta between them cannot be attributed to a code change, because the corpus underneath moved. This silently undermines any before/after evaluation of a harness change -- exactly the use case the harness exists for.

NOT A BUG IN DISCOVERY. Paging was verified sound: 3386 refs, zero duplicates. The cursor is keyset (`k: "recent"`, `d: true`, `v` = last ended_at, `i` = id tiebreak), not an offset, so insertion-during-paging drift cannot occur.

CANDIDATE SHAPES (not chosen):
  a. Document that comparable runs require `--date-to <past date>`, and say so in the report header.
  b. Emit a corpus fingerprint (sorted session-id hash + count) into the Summary so two reports can be checked for identical inputs before their numbers are compared.
  c. A `--freeze <manifest>` flag: write the discovered ref list once, replay it on later runs, and report refs that have since disappeared.

ACCEPTANCE. Either the harness can produce two byte-identical reports over an unchanged corpus, or the report states plainly that its input set is non-reproducible and names the mechanism. What must not survive: a reader diffing two reports and attributing the delta to code.
