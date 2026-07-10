# TB-24: codex web_search_call has no call_id and no output record, so CodexParser cannot join it

Found during TB-12 review. The live codex archive (114 rollouts) contains 138 `web_search_call` response_item records.

Unlike the three shapes CodexParser joins, web_search_call:
  - carries NO `call_id` (verified: has_call_id={False} across all 138)
  - has NO matching `web_search_output` record at all

So it cannot be joined on payload.call_id, which is CodexParser's only key. Claiming it would either fabricate a join key or emit 138 permanent no_result orphans; both are worse than reporting none. S33 documents the omission explicitly.

IMPACT: codex's reported call count understates its true tool usage by 138 calls (~4%). Web search is invisible in the corpus for codex.

DECIDE: either (a) emit them as zero-output calls with a new provenance/no_result meaning, or (b) leave unclaimed and note the gap in the report Summary so it is not silently absent. (b) is cheaper and matches the project's no-silent-zeros stance.

RELATED: TB-21 (Summary misreports corpus size) — the right home for surfacing 'calls a parser saw but could not join'.
