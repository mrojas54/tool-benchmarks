# TB-18: hermes --format trace parses as claude but carries no usage or requestId; cache-hit signal silently fabricated

HAZARD. `hermes sessions export --format trace` (v0.18.2, upstream daedf4f6) emits Claude Code-shaped JSONL. toolbench's TB-13 schema dispatch detects the claude schema by `"sessionId" in entry` (parsers.py:83). Hermes trace records carry `sessionId`. Therefore hermes trace exports dropped into the corpus parse CLEANLY as ClaudeParser, yield real ToolCall rows, and raise no error -- while silently carrying `usage=None` on every call.

This is not TB-12's silent zero (calls dropped). It is the inverse: calls are KEPT, and the usage channel is silently null.

EVIDENCE. Exported 47 live sessions (`--min-tool-calls 3 --newer-than 2d`) to scratch, 650 records total:

  lines with message.usage : 0   (0.0%)
  lines with requestId     : 0   (0.0%)
  tool_use blocks          : 280

Ran the real detector and parser against one trace file (20260709_122716_09e9bb):

  detect_parser(...)  -> ClaudeParser        # not an error, not a skip
  parser.parse(...)   -> 13 tool calls, 0 malformed
  first call: name='skill_view' input_chars=24 output_chars=53679
              usage=None  duration_ms=None  no_result=False
              model='zai/GLM-5.1:US'  result_source='block_local'

Record envelope is genuinely Claude-shaped: parentUuid, isSidechain, sessionId, uuid, cwd, gitBranch, version, userType, timestamp, type. Only `message.usage` and `requestId` are absent.

BLAST RADIUS (narrow, but real -- verified, not assumed):

1. Cache-hit false negatives. `_is_cache_hit(usage)` (passive.py:174) returns False on `not usage`. Every hermes-trace call is therefore silently counted as a cache MISS. S19 says this signal is caveat-only and never used for ranking, which caps the damage -- but the denominator is still quietly contaminated.

2. requestId absent -> TB-16 arm isolability is IMPOSSIBLE on trace exports. TB-16 established that batches must be grouped by requestId, not timestamp. Trace exports carry neither. Any probe run over trace data silently regresses to the pre-TB-16 defect.

3. `tokens` is NOT poisoned. `ToolCall.tokens` = `output_chars // 4` (transcript.py:62), a char proxy independent of `usage`. The token leaderboard does not dilute. This is the one thing that survives.

NON-FINDING (do not "fix"). Payload-based dispatch classifying hermes traces as `claude` is CORRECT by design -- ClaudeParser's own docstring says "detection is by payload, not by producer: one parser, two agents". Schema and agent are separate axes. The bug is not the dispatch; it is that nothing marks the resulting rows as usage-less.

DO NOT MIGRATE hermes.py TO TRACE. toolbench/hermes.py already sets `usage=None` deliberately, with a documented honesty rationale (hermes.py:116): hermes records `token_count` per MESSAGE, not per tool call. The SQLite adapter therefore has strictly MORE information than the trace export (message-level token_count exists in the DB; trace drops it entirely). Trace is a viewer/sharing format, not a measurement format. Migrating would lose data and gain nothing.

SPEC SHEET CORRECTIONS (the pasted summary overstates):
  - "Six formats" is FIVE: `--format {jsonl,md,qmd,html,trace}`. PROMPTS is not a format; it is `--only {user-prompts}`, a filter flag layered on jsonl/md.
  - "+15 more" filters undercounts; there are 23 filter flags.
  - Confirmed accurate: shared filter engine w/ prune+archive (both subcommands exist); trace redacts by default (`--no-redact` is opt-out, trace-only); `--lineage {single,logical}`; `--delete-after-verified`; `--upload` to HF.

CANDIDATE FIXES (not chosen):
  a. Give ParseResult/ToolCall an explicit `usage_available: bool` (or make `usage` a tri-state) so a usage-less corpus is loud rather than null-by-omission. Cheapest, matches the "skip loudly" precedent from TB-13.
  b. Have detect_parser emit a provenance warning when a claude-schema file has 0/N `message.usage` across the detect window. Detects the hazard at ingest, not at report time.
  c. Refuse trace-shaped input in probe.py specifically (requestId is mandatory there), while allowing it in passive.py.
  d. Do nothing; document that trace exports are passive-only input.

ACCEPTANCE. Either (i) a usage-less transcript is structurally distinguishable from a usage-bearing one at parse time -- so `_is_cache_hit` cannot silently fabricate misses and probe.py cannot silently regress past TB-16 -- or (ii) SPEC.md states that `--format trace` output is out-of-corpus and the loader rejects it. A hermes trace export must not be able to enter the corpus and quietly report 280 tool calls whose cache-hit rate is a fiction.
