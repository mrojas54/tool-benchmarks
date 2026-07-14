# TB-35: Uneven-sampling warning names its causes but cannot apportion the spread across them per agent

## Status of the original scope

This ticket was rewritten on 2026-07-13. Its first draft was built around a blocker that does not
exist, and carried two findings that have since been fixed. Do not work from the old text.

| Original claim | Verdict |
|---|---|
| `tally_skips` collapses `list[SkipRecord]` -> `dict[SkipReason, int]`, destroying agent identity on the path into the note | **False on this path.** `_sampling_notes` takes the RAW `list[SkipRecord]` and never calls `tally_skips`. `tally_skips` only feeds the rendered "Skipped by reason" table. See the comment at `report.py:102-104`. |
| `SkipRecord` must gain an `agent` field; the discovery path must stamp it on the ref | **False.** `SkipRecord` has carried `agent` since TB-23 (`sources.py:98-101`). `Counter(s.agent for s in skips)` shipped at `report.py:105`. |
| roborev #95 (false `--limit` attribution) and #66-F2 (all-skipped agents mislabelled "not reached") are blocked on this ticket | **False.** Both were fixed in `24d9c0f` with no re-keying. Both reviews are still OPEN in roborev and should be closed against that commit. |

Net: the part the ticket called "upstream data-model work" is cheap and local. The real upstream
work is something the ticket never mentioned — see step 2.

## What remains: quantitative apportionment

With the spread over `SPREAD_THRESHOLD` and both causes live, `report.py:155-162` can only say that
both are live — "`--limit N` was applied AND 40 sessions were skipped." It cannot say how much of the
spread each cause owns, per agent. The sentence still out of reach:

> agent X lost 38 of its 40 candidate sessions to attrition, which accounts for the spread; the limit
> accounts for the rest.

## Step 1 — attrition half: re-key the tally to `(agent, reason)`

Cheap, report-layer. The raw records already carry both fields, so this is
`Counter((s.agent, s.reason) for s in skips)` — not a data-model change.

`reason` must stay in the key. The remedy differs by reason, and that is the point of apportioning:

- `UNKNOWN_SCHEMA` attrition is **fixable** — write a parser and the spread closes.
- `MISSING_SOURCE` attrition is **permanent** — the transcripts are gone; the spread never closes.

"Agent X lost 38 sessions" without the reason cannot tell the reader which world they are in.

The existing collapsed "Skipped by reason" tally can keep rendering as-is; this is an additional
view, not a replacement.

## Step 2 — truncation half: no counter exists, and subtraction is forbidden

`passive.py:267-270` `break`s out of `refs_iter` as soon as the limit is hit, so the sessions the
limit cut are never enumerated. Nobody counts them.

**Do not derive it as `census.totals[X] - reached(X) - skipped_by_agent[X]`.** That residual is
*unaccounted*, not *truncated* — the report itself names other channels that land in the same bucket
(a `--since` cutoff, drift between discovery and the census; `report.py:176-181`). Calling that
bucket "limit truncation" reintroduces the exact anti-pattern `24d9c0f` removed from this function:
inferring one cause from the absence of the others. Every claim in this note fires on its own signal,
and apportionment must hold that line.

### Open design question — settle before implementing

- **(a) Count it at the source. RECOMMENDED.** Keep draining `refs_iter` past the limit, counting
  only (discovery-grain metadata, no exports), so per-agent truncation becomes an *observed* number.
  The only option that gives apportionment a real signal, and the drain is cheap.
- **(b) Render a third "unaccounted" bucket and refuse to name it.** Honest and cheaper, but delivers
  no apportionment — it abandons this ticket's remaining value.
- **(c) Subtract and call it truncation.** Rejected; see above.

## Done when

With both causes live, the uneven-sampling note apportions the spread per agent, every number rests
on its own observed signal, and the note names no cause it did not measure.
