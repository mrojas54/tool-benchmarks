# TB-12: toolbench parses only Claude's tool_use schema, so all 60 codex sessions yield zero tool calls

Found while running the passive analyzer on 2026-07-09. Codex sessions are
discovered, exported cleanly (rc=0, valid JSONL), and parsed without error --
and still contribute ZERO tool calls to every report. Same silent-zero
signature as TB-11 (hermes), different root cause: there the export was
off-contract, here the export is fine and the PARSER is Claude-only.

REPRO:
  uv run python -m toolbench.passive --agent all --all
  -> Agent Breakdown:  codex | 60 sessions | 0 calls | 0 tokens | 0 errors

  agentsview session export codex:019f3c64-8506-74c2-a7ac-d4c03f06bef6 \
    | python3 -c 'import sys,json,collections;
      c=collections.Counter(json.loads(l)["payload"]["type"]
        for l in sys.stdin if json.loads(l).get("type")=="response_item");
      print(c)'
  -> function_call: 116, custom_tool_call: 23, message: 73, reasoning: 59

EXPECTED: 139 tool calls from that one session. Observed: 0.

ROOT CAUSE -- toolbench/transcript.py:161

    if tool_use_block.get("type") != "tool_use":
        continue

The parser recognizes exactly one schema: Claude's assistant-message
`tool_use` blocks joined to user-message `tool_result` blocks by
`tool_use_id`. Codex emits a different, equally well-formed shape:

    {"type": "response_item",
     "payload": {"type": "function_call",
                 "name": "exec_command",
                 "arguments": "{...}",
                 "call_id": "call_3SBoxKNH2ON6H5Oo9bsEppSK"}}

    {"type": "response_item",
     "payload": {"type": "function_call_output",
                 "call_id": "call_3SBoxKNH2ON6H5Oo9bsEppSK",
                 "output": "..."}}

Join key is `payload.call_id`, not `tool_use_id`. Nothing matches
`type == "tool_use"`, so the loop skips every line, `pending` stays empty,
and the session yields no calls. No error is raised and `malformed_lines`
stays 0 -- the run looks perfectly healthy.

EVIDENCE (2026-07-09, live archive, all 60 codex sessions swept):

  sessions exported ok   60
  sessions failed         0
  function_call        1969   function_call_output      1969
  custom_tool_call      120   custom_tool_call_output    120
  ------------------------------------------------------------
  TOTAL CALLS DROPPED  2089   outputs present            2089

Every call has a matching output. The join is 1:1 and lossless -- the data is
sitting there fully paired, waiting for an adapter.

  top tool names: exec_command 1484, write_stdin 437, apply_patch 120,
                  shell_command 21, update_plan 15, spawn_agent 5,
                  wait_agent 4, request_user_input 1

IMPACT:
- Correctness: 2089 calls (+8.7% of the 23960-call corpus) are missing. Codex
  would rank 3rd by call volume, ahead of hermes (1373).
- Every cross-agent RATE is skewed. Codex's 60 sessions sit in the denominator
  of the corpus-wide callouts (6.3% failure, 3.2% oversized, 3.4% deferral
  tax) while contributing nothing to any numerator.
- Codex is the only agent in the corpus with `spawn_agent`/`wait_agent` calls,
  so the subagent fan-out callout (1.4%) is measured with its most relevant
  agent's data entirely absent.
- Silent. A zero row is indistinguishable from "this agent did no tool work".
  TB-11 took a crash to surface; this one produces no signal at all.

IN SCOPE:
- Add `toolbench/codex.py`: a source adapter that parses codex `response_item`
  lines, joining `function_call` -> `function_call_output` and
  `custom_tool_call` -> `custom_tool_call_output` on `payload.call_id`.
- Wire it in by agent, the way hermes.py is (discovery stays with AgentsView;
  only the parse is redirected).
- Payload normalization must match the existing contract: `chars / 4` context
  tokens, `input_chars` from `arguments`, result chars from `output`.
- Unmatched `function_call` at EOF keeps the existing no_result semantics.
- Tests: hermetic fixture JSONL in the codex shape, mirroring tests/test_hermes.py.

OUT OF SCOPE:
- Cursor's zero row (47 sessions). It is a THIRD schema again -- most lines
  carry no top-level `type` key at all, only `turn_ended` markers appear.
  Same class of defect, separate ticket, needs its own repro.
- The 941 skipped claude-ai sessions. Web chat is a declared non-goal in
  README ("No web-chat benchmarking"); those skips are correct behavior.
- The 31 warp + 2 antigravity skipped sessions. Local agents, in scope for the
  project, but blocked on export failing -- a discovery problem, not a parse
  problem.
- Shrinking the 280KB `Skipped roots:` line that is 84% of the report file.
  Cosmetic, filed separately if it bothers anyone.

NOTE ON GENERALIZING: three agents now need three parsers (Claude tool_use,
hermes SQLite, codex response_item), with cursor pending as a fourth. Worth
deciding in this ticket whether transcript.py grows a schema-dispatch seam or
whether each adapter stays a sibling module. Do not refactor blind -- pick one
and say why in the PR.
