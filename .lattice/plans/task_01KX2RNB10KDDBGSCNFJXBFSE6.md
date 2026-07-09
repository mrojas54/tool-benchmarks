# TB-16: Probe isolability check ignores prose in an arm turn

find_probe_calls computes isolable = turn_call_counts[ts] == 1, and turn_call_counts is built only from tool_use blocks (_scan_tool_use_blocks skips every other block type). An assistant turn holding one tool_use block plus prose therefore looks isolable, so _usage_output_tokens returns the message's full output_tokens -- prose included -- and attributes it to the arm.

Observed live: in session edcea84f the probe 01 tool arm reported 561 usage tokens against 93-105 for the other four tool arms, because the operator wrote a three-sentence preamble in the same turn as the find_file call.

Same attribution bug as the batched-two-calls case the check already defends against: output_tokens is per-message, so anything emitted in that message is pooled.

Fix: isolable must additionally require that the turn carry no non-empty text block. A contaminated arm then yields usage=None and the cell renders the seeded baseline with a visible '*' rather than a silently inflated number. Plus a rule 5 in protocols/probe-run-sheet.md forbidding prose in an arm turn.

Present on main (toolbench/probe.py:201) -- predates TB-15. Stacked on tb-15 branch because the run sheet only exists there.
