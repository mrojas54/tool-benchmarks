# TB-17: Sentinel + description tax makes the bash arm's output_tokens non-comparable to the tool arm's

DEFECT. The `bash usage` column of the S18 active-probe report is not comparable to the `tool usage` column. output_tokens is billed against the whole emitted tool_use block -- tool name plus serialized input -- and the bash arm is structurally required to carry instrumentation that the tool arm cannot carry. The instrumentation is the same size as the effect being measured.

EVIDENCE. Clean ten-arm run, session ca1a80df, zero seeded cells, all ten usage numbers present (first fully-isolable run since TB-16 landed).

  probe | tool name+input chars -> out_tok | bash name+input chars -> out_tok | gap
  01    |  90 ->  94                       | 115 -> 112                       | 18
  02    | 105 -> 102                       | 104 -> 110                       |  8
  03    |  86 ->  93                       | 107 -> 110                       | 17
  04    | 116 -> 105                       | 126 -> 116                       | 11
  05    |  92 ->  95                       | 119 -> 114                       | 19

Mean bash penalty: 14.6 output tokens. The bash arm carries two payload items the tool arm does not:

1. The sentinel comment `  # TB_PROBE_<NN>_BASH_V2` (23 chars). It fragments heavily under BPE (TB / _ / PROBE / _ / 01 / _ / BASH / _ / V / 2), so ~10-12 tokens.
2. The Bash tool's `description` field, e.g. `,"description":"Find regex_check.py"` (~36 chars incl. key), ~8-10 tokens.

Together ~20 output tokens, charged to the bash arm on every probe. That exceeds the measured 14.6-token gap in the mean and exceeds the gap in 5/5 individual probes. The reported result -- "the MCP tool arm is cheaper on output tokens" -- is therefore not established by this data. Removing the instrumentation could plausibly flip the sign.

NOT A CONFOUND (do not "fix" this). Serena's tool name is 36-45 chars (`mcp__plugin_serena_serena__search_for_pattern`) against Bash's 4, and the name lives inside the billed tool_use block. That ~10 tokens/call is a real cost of MCP namespacing and belongs in the comparison. The bug is narrower: one measurement artifact lands on one arm only.

WHY IT IS STRUCTURAL. TB-15 established that the tool arms cannot carry a sentinel at all -- serena's schemas have no free-text field -- which is why the matcher identifies them structurally by tool_name + corpus target. So the mechanism that makes the bash arm *identifiable* is the same mechanism that makes it look *more expensive*. Symmetry is unavailable by construction; the sentinel cannot simply be moved onto the tool arm.

The `description` half is weaker: the run sheet does not mandate it, the operator supplied it. Bash's schema wants it. It is avoidable-ish, the sentinel is not.

SCOPE. The context-token columns (`tool tokens` / `bash tokens`) are unaffected -- they measure the returned tool_result, not the emitted call -- and remain the trustworthy half of the table. Only the two usage columns are implicated.

CANDIDATE FIXES (not yet chosen; a spike may be warranted):
  a. Measure the instrumentation constant directly -- emit the same bash arm with and without sentinel+description in a throwaway session, subtract, and publish the corrected column with the correction stated.
  b. Give the tool arms a dead-weight payload of matched token cost so the tax is symmetric (e.g. a no-op field serena ignores), making the raw columns directly comparable.
  c. Drop the usage columns from the report and state that output-token cost is not measurable under the current sentinel design; report context tokens only.
  d. Reconstruct output_tokens analytically from the tool_use block by tokenizing name+input, so the sentinel can be subtracted exactly rather than estimated.

Fix (a) is the cheapest and least invasive. Fix (b) risks perturbing the tool arm's own cost. Fix (c) discards a real signal. Fix (d) needs a tokenizer that matches the server's, which we do not have.

ACCEPTANCE. Either the usage columns carry a stated, measured correction for the sentinel + description tax, or the report explicitly declares them non-comparable and says why. A reader must not be able to conclude "MCP is cheaper on output tokens" from an artifact of the sentinel channel.
