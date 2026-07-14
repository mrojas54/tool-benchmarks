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

### Design question — SETTLED (implemented in `d16c4a3`, `1af2a7e`; refined per roborev #103)

The options weighed were: **(a)** drain `refs_iter` past the limit and count what it yields;
**(b)** render an unnamed "unaccounted" bucket and apportion nothing; **(c)** subtract and call the
residual truncation. **(c)** was rejected outright (see above). **(a)** was withdrawn: a full drain
buys a per-agent truncation *count* nobody needs, since the per-agent remainder is already available
without it. **(b)** gives up the ticket.

**Adopted — (d): remainder from the census, truncation from a boundary probe.**

- **Per-agent remainder** is `census.totals[agent] - sampled_by_agent[agent]`, where `sampled_by_agent`
  is counted AFTER `filter_subagents` so numerator and denominator describe one population (the TB-33
  Finding 1 invariant). Never `total - reached - skipped`: a ref that parses to zero calls yields
  neither a reducer session nor a `SkipRecord`, so that form silently bills real sessions to
  truncation (off by 30 in the live archive).
- **Whether the limit truncated at all** is OBSERVED, never read off the flag: when the ref loop
  breaks on the limit, discovery asks the listing for one more ref. `limit is not None` is not the
  signal — `--limit 9000` over an 8778-session archive passes the flag and cuts nothing, and blaming
  it would be the same inference-from-absence this ticket exists to delete (roborev #98/#101).
- **The probe asks about the reported population, and cannot be fatal** (roborev #103). It skips refs
  `--exclude-subagents` would drop, since the listing always yields children (`_ALL_INCLUDES`) and a
  left-behind child is not a session the report counts. And because it runs only after the run holds
  every ref it asked for, a failed page returns `None` — *unobserved* — rather than crashing the run,
  fabricating a `MISSING_SOURCE` skip, or decaying to `False`, which would print "the limit truncated
  nothing" off a measurement nobody took.
- Without a truncation bite, the remainder is named as drift between the census call and the listing —
  never as truncation.

## Done when

With both causes live, the uneven-sampling note apportions the spread per agent, every number rests
on its own observed signal, and the note names no cause it did not measure.
