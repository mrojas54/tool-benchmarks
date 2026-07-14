"""Markdown report rendering and corpus fingerprinting (S14, S15, S36, TB-22)."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from toolbench.reducer import OVERSIZED_OUTPUT_TOKENS, AgentStats, Reducer
from toolbench.sources import AgentCensus, SkipReason, SkipRecord

# Ratio of the largest per-agent sampling fraction to the smallest, above which
# cross-agent numbers stop being comparable and the report says so. Arbitrary but
# STATED -- and it does NOT only fire when a `--limit` actually truncated the corpus.
# There are two independent causes. (1) `--limit` truncation, applied unevenly across
# agents because it slices the archive in recency order. (2) Per-agent SKIP ATTRITION:
# `stats.sessions` (the numerator, from `reducer.agents`) counts sessions that were
# DISCOVERED *and* PARSED, so an agent that lost a disproportionate share to any
# `SkipReason` bucket (dead index entries, non-transcript/binary exports, unknown
# schemas) moves its own fraction with no `--limit` in play at all. The census now
# inheriting the SAME --include-* population filters the numerator does (TB-33
# Finding 1) rules out ONE historical false-positive source (an agent's own
# child/parent ratio skewing the denominator) -- it does not make attrition
# impossible. Both causes are real, and each has its OWN observable signal, so
# `_sampling_notes` is handed both and names only what it can see (TB-33 Finding 4):
# `skips` is non-empty iff attrition happened, and `limit` is not None iff a `--limit`
# was actually applied. That is a 2x2, not a 1x2. Deriving the limit from `skips` alone
# -- "no skips, therefore --limit" -- was a false cause AND a false remedy in the case
# that matters most: an `--all` run (no limit, no skips) with a spread was told to
# "re-run without `--limit`", i.e. to remove a flag it never passed. A cause is only
# named here when its own signal says so; when neither signal fires, the spread is real
# and the line says the window itself is uneven rather than inventing a remedy.
SPREAD_THRESHOLD = 4.0


def _sampled_cell(scanned: int, total: int | None, unavailable: bool) -> str:
    """One agent's `sampled` cell: the numerator, its denominator, and the fraction."""
    if unavailable:
        return "unknown"
    if total is None:
        # Scanned, but absent from the census universe -- i.e. an agent the child-excluded
        # probe never saw. `residual` names it in aggregate; this names it in place.
        return f"{scanned} of unknown"
    if total == 0:
        # `scanned` is NOT dropped here: an agent can be seen in the probe listing and
        # then report total=0 from the later scoped call (the two are separate,
        # non-atomic agentsview invocations). Printing "0 of 0" over a nonzero
        # `scanned` would be the exact silent zero this ticket exists to close.
        return f"{scanned} of 0"
    # NOT clamped to 100%: `scanned` and `total` come from separate, non-atomic
    # agentsview calls (the full listing vs. the `--limit 1` census probe), so a session
    # created between the two calls can leave scanned > total and print e.g. "5 of 2
    # (250.0%)". That is a visible bug report -- something drifted between the calls --
    # and clamping it to 100% would silently launder that signal into a lie.
    return f"{scanned} of {total} ({scanned / total * 100:.1f}%)"


def _sampling_spread(reducer: Reducer, census: AgentCensus) -> float | None:
    """max/min sampling fraction across agents with >= 1 scanned session.

    Agents with zero scanned sessions are excluded: their fraction is 0, which would send
    the ratio to infinity and drown the real signal. They are disclosed by name instead.
    """
    fractions = [
        stats.sessions / total
        for agent, stats in reducer.agents.items()
        if stats.sessions and (total := census.totals.get(agent))
    ]
    if len(fractions) < 2:
        return None
    return max(fractions) / min(fractions)


def _apportionment(
    reducer: Reducer,
    census: AgentCensus,
    skips: list[SkipRecord],
    sampled_by_agent: dict[str, int],
    limit: int | None,
) -> list[str]:
    """Split each agent's archive into pulled, never-pulled, and lost-in-the-parser (TB-35).

    Naming both causes is not apportioning between them. "`--limit` was applied AND 40
    sessions were skipped" leaves the reader unable to act: is codex's 21.9% an artifact of
    the limit, or did its archive mostly die in the parser? Those have different remedies,
    and only a per-agent split can tell them apart.

    Every number rests on its OWN observed signal -- `census.totals` (a scoped `--limit 1`
    count read) for the archive, the POST-`filter_subagents` ref count for what this run
    pulled, `SkipRecord` for attrition. In particular the remainder is `total - sampled`,
    never `total - reached - skipped`: a ref that parses to zero calls produces neither a
    reducer session nor a `SkipRecord`, so that second form would quietly bill real sessions
    to truncation. A ref was either pulled or it was not, and that is what we count.

    The remainder is named as `--limit` truncation ONLY when a limit was actually passed.
    Without one, `_agentsview_pages` drains its cursor to exhaustion, so a remainder cannot
    BE truncation -- it is drift between the census call and the listing. Calling it
    truncation there would be the same inference-from-absence this module keeps deleting.

    Keyed by (agent, reason), not by reason alone: the remedy differs by reason.
    UNKNOWN_SCHEMA attrition closes the day someone writes a parser; MISSING_SOURCE
    attrition never closes, because the transcripts are gone. `tally_skips` collapses that
    distinction away, which is exactly why it is not used here.
    """
    by_agent_reason: Counter[tuple[str, SkipReason]] = Counter((s.agent, s.reason) for s in skips)
    lines = [
        "  - Apportionment (each number observed on its own signal; none inferred from the "
        "absence of another):"
    ]
    for agent in sorted(census.totals):
        total = census.totals[agent]
        if total <= 0:
            continue
        sampled = sampled_by_agent.get(agent, 0)
        remainder = total - sampled
        if limit is not None and remainder > 0:
            pulled = f"{sampled} sampled, so {remainder} never pulled (`--limit {limit}` truncation)"
        elif remainder != 0:
            # No limit, or a NEGATIVE remainder (the listing outran the census). Either way
            # truncation is ruled out by its own signal, so the gap is named, not blamed.
            pulled = (
                f"{sampled} sampled, {abs(remainder)} unaccounted (drift between the census "
                "call and the listing, NOT truncation"
                + ("" if limit is not None else " -- no `--limit` was applied")
                + ")"
            )
        else:
            pulled = f"{sampled} sampled, none lost to the window"
        clauses = [f"{agent}: {total} in archive; {pulled}"]

        reasons = [(r, n) for (a, r), n in by_agent_reason.items() if a == agent]
        if reasons:
            named = ", ".join(
                f"{n} {r.value}" for r, n in sorted(reasons, key=lambda kv: (-kv[1], kv[0].value))
            )
            lost = sum(n for _, n in reasons)
            clauses.append(f"of the {sampled} sampled, {lost} lost to attrition ({named})")

        reached = reducer.agents.get(agent, AgentStats()).sessions
        clauses.append(f"{reached} reached the table")
        lines.append("    - " + "; ".join(clauses) + ".")
    return lines


def _sampling_notes(
    reducer: Reducer,
    census: AgentCensus,
    skips: list[SkipRecord],
    limit: int | None,
    sampled_by_agent: dict[str, int] | None = None,
) -> list[str]:
    """Disclosure that belongs BESIDE the table, not forty lines below it (TB-33).

    A reader forming a calls/session ratio across two rows never scrolls to the Summary,
    so the qualification has to sit where the comparison is made.

    `skips` and `limit` are the two independent signals behind every claim made here, and
    NEITHER is inferred from the other. `skips` carries per-record `agent` identity, which
    is what lets a zero-session row say WHY it is zero: an agent nobody looked at and an
    agent whose every sampled session failed to parse both land at `sessions == 0`, and
    calling the second one "not reached" tells the reader we never looked when in fact we
    looked and everything we opened broke (TB-33 Finding 2). `limit` is what lets the
    uneven-sampling line name `--limit` as a cause only when a `--limit` was actually
    passed, instead of concluding it from an empty skip list (TB-33 Finding 4).
    """
    if census.unavailable_reason is not None:
        return [
            f"- Sampling fractions unavailable: {census.unavailable_reason}. Each row above "
            "may rest on a different fraction of its agent's archive; this run cannot say."
        ]

    notes: list[str] = []
    # Per-agent, straight off the raw records -- `tally_skips` would collapse these to
    # dict[SkipReason, int] and destroy exactly the agent identity this needs, so it is
    # deliberately not used here.
    skipped_by_agent = Counter(s.agent for s in skips)
    zero_session = sorted(
        agent
        for agent, total in census.totals.items()
        if total > 0 and reducer.agents.get(agent, AgentStats()).sessions == 0
    )
    never_looked = [a for a in zero_session if not skipped_by_agent[a]]
    all_skipped = [a for a in zero_session if skipped_by_agent[a]]
    if never_looked:
        named = ", ".join(f"{a} ({census.totals[a]} sessions)" for a in never_looked)
        notes.append(
            f"- Present in the archive, not reached by this window: {named}. Their rows are "
            "zeros because we did not look, not because they did no work."
        )
    if all_skipped:
        # `all_skipped` is non-empty only when `skips` is, and `render_report` renders the
        # tally `if skips:` -- so this pointer always resolves to something on the page.
        named = ", ".join(
            f"{a} ({skipped_by_agent[a]} skipped of {census.totals[a]} in archive)"
            for a in all_skipped
        )
        notes.append(
            f"- Reached, but every session sampled from them was skipped: {named}. Their rows "
            "are zeros because everything this window opened failed to parse or export, NOT "
            'because we did not look -- see the "Skipped by reason" tally below.'
        )

    spread = _sampling_spread(reducer, census)
    if spread is not None and spread >= SPREAD_THRESHOLD:
        preamble = (
            f"- **Sampling is uneven ({spread:.1f}x spread).** Each row is a different "
            "fraction of a different-sized population, so any ratio formed ACROSS rows "
            "(calls/session, tokens/call, error rate) mixes sampling depth into the "
            "comparison and is not comparable."
        )
        # Each cause is named ONLY on its own signal: `limit is not None` for truncation,
        # a non-empty `skips` for attrition. Four cases, and the report earns every word
        # of each (TB-33 Finding 4).
        n = len(skips)
        were = f"{n} session{'s' if n != 1 else ''} {'were' if n != 1 else 'was'} skipped"
        tally = 'see the Summary\'s "Skipped by reason" tally below'
        if limit is not None and not skips:
            # Attrition ruled out by the empty skip list, truncation confirmed by the
            # flag itself. Both halves observed, so the remedy is real: drop the limit.
            notes.append(
                preamble + f" No sessions were skipped this run, which rules out skip "
                f"attrition, and `--limit {limit}` was applied: the spread comes from that "
                "limit truncating the corpus unevenly across agents. Re-run without "
                "`--limit` for a like-for-like table."
            )
        elif limit is not None:
            # Both signals fire. Name both, promise neither remedy alone -- dropping the
            # limit would not budge the attrition half.
            notes.append(
                preamble + f" Both causes are live: `--limit {limit}` was applied AND {were} "
                f"this run ({tally}). Re-running without `--limit` is not guaranteed to give "
                "a like-for-like table, since the attrition would survive it."
            )
        elif skips:
            # No limit was passed, so truncation is ruled out by its own signal rather
            # than assumed away. Attrition is what is left, and it is observed.
            notes.append(
                preamble + f" No `--limit` was applied, which rules out limit truncation, and "
                f"{were} this run: the spread comes from per-agent skip attrition ({tally})."
            )
        else:
            # Neither signal fired, so neither named cause is available -- and the honest
            # move is to say the spread is real rather than pin it on the nearest flag.
            # This is the case the old one-armed branch got wrong: it reached here with an
            # empty skip list and told an `--all` run to "re-run without `--limit`".
            notes.append(
                preamble + " Neither of the causes this report can name explains it: no "
                "`--limit` was applied and no sessions were skipped. The spread is in the "
                "window itself -- the sessions it reached are a different fraction of each "
                "agent's archive (a `--since` cutoff, say, or drift between discovery and "
                "the census). There is no flag to drop; the unevenness is real."
            )
        # Naming the causes is not splitting the spread between them (TB-35). `None` is the
        # typed ABSENCE of per-agent ref counts -- a caller that did not record them gets no
        # apportionment rather than one reconstructed from reached+skipped, which would bill
        # every zero-call session to truncation.
        if sampled_by_agent is not None:
            notes.extend(_apportionment(reducer, census, skips, sampled_by_agent, limit))

    if census.residual > 0:
        notes.append(
            f"- Reconciliation: {census.residual} archive sessions belong to no agent we "
            "enumerated. The census universe comes from the child-excluded probe listing, "
            "so an agent whose sessions are ALL children is invisible to it -- the "
            "denominators above are incomplete."
        )
    return notes


@dataclass(frozen=True)
class CorpusFingerprint:
    """Identity of the scanned corpus (TB-22, S36).

    A `digest` over a per-session *signature* for every scanned session -- the
    sessions that actually produced the report's numbers -- plus their `count`.
    Two runs whose fingerprints match scanned the same sessions with the same
    content, so a numeric delta between their reports is attributable to code,
    not to the corpus moving underneath.

    The signature carries both mechanisms the corpus drifts by (see the ticket):
    a session's identity catches the sliding-window TAIL DELETION (a transcript
    ages out and its id leaves the set), and its call and malformed-line counts
    catch the live session's APPEND (transcripts are append-only, so both counts
    are exact proxies for content growth -- including an append that lands as a
    malformed line rather than a new valid call). An id-only digest, or one
    folding calls alone, would match across an append while a rendered number
    moved and falsely reassure a reader diffing the two reports -- the one outcome
    the ticket says must not survive.

    The scanned set, not the discovered set, is the basis: a discovered-set
    digest could match while transcripts slid scanned->skipped. The count travels
    alongside so a hash collision cannot hide a size change.
    """

    digest: str
    count: int


def corpus_fingerprint(signatures: Iterable[str]) -> CorpusFingerprint:
    """Order-independent fingerprint of a set of per-session signatures (S36).

    Sorted before hashing so discovery/paging order can never move the digest --
    only the membership or content of the scanned set can. `session_signature`
    builds the per-session strings; this stays a pure set-hash so its callers
    decide what a signature contains (the manifest freezes identity alone).
    """
    items = sorted(signatures)
    h = hashlib.sha256()
    for sig in items:
        h.update(sig.encode("utf-8"))
        h.update(b"\n")
    return CorpusFingerprint(digest=h.hexdigest()[:16], count=len(items))


def session_signature(
    session_id: str, call_count: int, malformed: int, unjoinable: int = 0
) -> str:
    """One scanned session's fingerprint contribution: identity + content (S36).

    Tab-joins the id with every number the Summary renders for this session's
    content -- its call count, its malformed-line count, and its unjoinable-record
    count -- so a session that grows moves the corpus fingerprint even though its id
    is unchanged (append-only transcripts -> every count is exact). Folding
    `call_count` alone would miss an append that lands as a malformed line;
    likewise, folding only calls and malformed would miss an appended
    `web_search_call`, which moves "Unjoinable tool records" while `len(calls)` and
    "Malformed lines" stay put -- and the digest would falsely match while a rendered
    number differs, the one outcome S36 forbids (TB-24).
    """
    return f"{session_id}\t{call_count}\t{malformed}\t{unjoinable}"


def tally_skips(skips: Iterable[SkipRecord]) -> dict[SkipReason, int]:
    """Count skips per reason. Answers "how many have no parser?" from typed data."""
    return dict(Counter(s.reason for s in skips))


def _top_offender(by_tool: dict[str, int]) -> tuple[str, int] | None:
    """Highest count, ties broken alphabetically so the report is deterministic."""
    if not by_tool:
        return None
    return min(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))


def _callout(label: str, count: int, total_calls: int, by_tool: dict[str, int]) -> str:
    """Render one callout as `N of M calls (P%)`, naming the worst tool."""
    share = (count / total_calls * 100) if total_calls else 0.0
    line = f"- {label}: {count} of {total_calls} calls ({share:.1f}%)"
    top = _top_offender(by_tool)
    if count and top is not None:
        line += f"; top: {top[0]} ({top[1]})"
    return line


def _reasons_by_count(skips: list[SkipRecord]) -> list[tuple[SkipReason, int]]:
    """Skip reasons highest-count-first; ties break on the reason's value so the
    histogram is deterministic."""
    tally = tally_skips(skips)
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0].value))


def render_report(
    reducer: Reducer,
    *,
    index_source: str,
    fallback_reason: str | None,
    skips: list[SkipRecord],
    include_subagents: bool,
    subagents_found: int,
    sessions_discovered: int,
    since_note: str | None,
    census: AgentCensus,
    verbose: bool = False,
    fingerprint: CorpusFingerprint | None = None,
    freeze_note: str | None = None,
    run_tickets: int | None = None,
    limit: int | None = None,
    sampled_by_agent: dict[str, int] | None = None,
) -> str:
    """Render the five-section report (S14) with provenance (S15).

    `sampled_by_agent` counts the refs this run actually pulled, per agent, taken AFTER
    `filter_subagents` so it describes the same population the census's `includes` do
    (TB-33 Finding 1). It is what lets the uneven-sampling note apportion a spread between
    truncation and attrition (TB-35); `None` means the caller did not record it, and the
    apportionment is then withheld rather than reconstructed.
    """
    lines: list[str] = ["# Tool Usage Report", ""]

    lines.append("## Agent Breakdown")
    lines.append("")
    lines.append(
        "| agent | sampled | sessions | calls | output_tokens | input_tokens | errors | no_result |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    cache_caveats: list[str] = []
    # Union, not `reducer.agents`: an agent the window never reached has no AgentStats at
    # all, and dropping its row is the headline bug (TB-33).
    for agent in sorted(set(reducer.agents) | set(census.totals)):
        s = reducer.agents.get(agent, AgentStats())
        sampled = _sampled_cell(
            s.sessions, census.totals.get(agent), census.unavailable_reason is not None
        )
        lines.append(
            f"| {agent} | {sampled} | {s.sessions} | {s.calls} | {s.output_tokens} | "
            f"{s.input_tokens} | {s.errors} | {s.no_result} |"
        )
        if s.sessions_with_cache_data > 0:
            # S32: session-grain only, orthogonal to the per-call `cache_assisted`
            # column below -- never mixed into that column, never a sixth section.
            cache_caveats.append(
                f"- {agent}: {s.sessions_with_cache_hit} of {s.sessions_with_cache_data} "
                "sessions carry session-grain `cache_read_tokens` > 0 "
                "(S32: session grain only — not attributable to individual tool calls)."
            )
    lines.extend(cache_caveats)
    lines.extend(_sampling_notes(reducer, census, skips, limit, sampled_by_agent))
    lines.append("")

    lines.append("## Tool Leaderboard")
    lines.append("")
    lines.append("| agent | tool | calls | context_tokens | input_tokens | errors | cache_assisted |")
    lines.append("|---|---|---|---|---|---|---|")
    ranked = sorted(reducer.tools.items(), key=lambda kv: kv[1].output_tokens, reverse=True)
    for (agent, tool), stats in ranked:
        if stats.cache_hits > 0:
            cache_note = "yes"  # a hit was observed; blindness elsewhere is irrelevant
        elif stats.usage_missing == 0:
            cache_note = "no"  # measured, and it was zero
        elif stats.usage_missing == stats.calls:
            cache_note = "n/a"  # never measurable
        else:
            cache_note = "n/a*"  # partially measurable; some rows blind
        lines.append(
            f"| {agent} | {tool} | {stats.calls} | {stats.output_tokens} | "
            f"{stats.input_tokens} | {stats.errors} | {cache_note} |"
        )
    lines.append("")
    lines.append(
        "`n/a` = usage channel unavailable for every call (S29); "
        "`n/a*` = unavailable for some. Neither is a measured zero. "
        "Per S19 this flag is caveat-only and never affects ranking."
    )
    lines.append("")

    lines.append("## Model Breakdown")
    lines.append("")
    lines.append("| agent | model | tool | calls | context_tokens | input_tokens | errors |")
    lines.append("|---|---|---|---|---|---|---|")
    # Descending by context tokens; key breaks ties so the table is deterministic.
    ranked_by_model = sorted(
        reducer.tools_by_model.items(), key=lambda kv: (-kv[1].output_tokens, kv[0])
    )
    for (agent, model, tool), stats in ranked_by_model:
        lines.append(
            f"| {agent} | {model} | {tool} | {stats.calls} | {stats.output_tokens} | "
            f"{stats.input_tokens} | {stats.errors} |"
        )
    lines.append("")

    lines.append("## Inefficiency Callouts")
    lines.append("")
    ineff = reducer.inefficiency
    total = reducer.calls_joined
    share = (ineff.tool_search_calls / total * 100) if total else 0.0
    lines.append(
        f"- ToolSearch/deferral tax: {ineff.tool_search_calls} of {total} calls "
        f"({share:.1f}%), {ineff.tool_search_tokens} tokens"
    )
    lines.append(_callout("Failures", ineff.failures, total, ineff.failures_by_tool))
    lines.append(
        _callout(
            f"Oversized outputs (>= {OVERSIZED_OUTPUT_TOKENS} tokens)",
            ineff.oversized_outputs,
            total,
            ineff.oversized_by_tool,
        )
    )
    lines.append(
        _callout("Subagent fan-out calls", ineff.subagent_fanout, total, ineff.subagent_by_tool)
    )
    lines.append(
        _callout(
            "Churn (consecutive-repeat retries)", ineff.churn_retries, total, ineff.churn_by_tool
        )
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Index source: {index_source}")
    # Reconcile discovery so `scanned` is never mistaken for the corpus size: a
    # discovered session either scanned or skipped, and every skip is one SkipRecord
    # (TB-21). `discovered` is derived, not a separate count that could drift.
    scanned = reducer.sessions_scanned
    skipped = len(skips)
    lines.append(
        f"- Sessions discovered: {scanned + skipped} / scanned: {scanned} / skipped: {skipped}"
    )
    if census.unavailable_reason is None and census.totals:
        lines.append("- Sampling (scanned of each agent's own archive):")
        for agent in sorted(census.totals):
            agent_total = census.totals[agent]
            scanned_agent = reducer.agents.get(agent, AgentStats()).sessions
            pct = f"{scanned_agent / agent_total * 100:.1f}%" if agent_total else "n/a"
            tail = " — not reached by this window" if scanned_agent == 0 else ""
            lines.append(f"  - {agent}: {scanned_agent} of {agent_total} ({pct}){tail}")
    if fingerprint is not None:
        # Identity of the set that produced the numbers above: two reports whose
        # fingerprints match are diffable; a delta between them is code, not the
        # corpus moving underneath (TB-22, S36).
        lines.append(
            f"- Corpus fingerprint: {fingerprint.digest} ({fingerprint.count} sessions scanned)"
        )
    if freeze_note is not None:
        lines.append(f"- {freeze_note}")
    if skips:
        # Keyed on the typed SkipReason (S34), not a substring scan of prose. A dead
        # index entry (missing_source) and a parser gap (unknown_schema) are counted
        # in separate buckets so the actionable one is never buried under the rest.
        lines.append("- Skipped by reason:")
        for reason, count in _reasons_by_count(skips):
            lines.append(f"  - {reason.value}: {count}")
    lines.append(f"- Tool calls joined: {reducer.calls_joined}")
    lines.append(f"- Malformed lines: {reducer.malformed_total}")
    cache_read_total = sum(s.cache_read_tokens_total for s in reducer.agents.values())
    cache_creation_total = sum(s.cache_creation_tokens_total for s in reducer.agents.values())
    measured_cache_sessions = sum(s.sessions_with_cache_data for s in reducer.agents.values())
    if measured_cache_sessions > 0:
        # S39: read + creation together — a prefix-sharing change trades one for the
        # other, so a read delta alone misleads. Caveat only; never ranks.
        lines.append(
            f"- Session-grain cache tokens: read={cache_read_total} "
            f"creation={cache_creation_total} "
            f"({measured_cache_sessions} measured sessions; S39 caveat, not ranked)"
        )
    if reducer.run is not None:
        # S40: per-run cache cost. Caveat only -- never ranked, never folded into an
        # inefficiency ratio (S19). Read and creation always together (S39).
        run_stats = reducer.run_stats
        lines.append(
            f"- Run cache tokens (run {reducer.run.run}): "
            f"read={run_stats.read} creation={run_stats.creation} "
            f"({run_stats.candidate_sessions} candidate session"
            f"{'' if run_stats.candidate_sessions == 1 else 's'}; S40 caveat, not ranked)"
        )
        tickets = run_tickets if run_tickets is not None else reducer.run.ticket_count
        if tickets > 0:
            norm = run_stats.per_ticket(tickets)
            lines.append(
                f"  - per ticket ({tickets}): "
                f"read={norm['cache_read']:.1f} creation={norm['cache_creation']:.1f}"
            )
        if run_stats.unattributed_read or run_stats.unattributed_creation:
            # Straddle spillover: same-session work on branches outside the run. A
            # large value means the run total is a narrow slice of what was spent.
            lines.append(
                f"  - unattributed: read={run_stats.unattributed_read} "
                f"creation={run_stats.unattributed_creation} "
                f"(same-session work off the run's branches)"
            )
        if run_stats.detached_sessions:
            # TB-28: usage from DETACHED checkouts (gitBranch="HEAD"). Unattributable
            # by construction -- "HEAD" matches no manifest branch -- so it is neither
            # counted in the run nor discardable: a detached delegator and unrelated
            # detached work look identical. Name it so the run total is never read as
            # complete when it may not be (S23/S38).
            # input/output are shown HERE though the run headline is cache-only (S40):
            # an uncached detached turn has zero cache and real input/output, and a
            # bare "read=0 creation=0" would read as "nothing to see" -- a lie by
            # omission on the one line whose whole job is to disclose what was missed.
            lines.append(
                f"  - detached-HEAD (unattributable): "
                f"read={run_stats.detached_read} "
                f"creation={run_stats.detached_creation} "
                f"input={run_stats.detached_input} "
                f"output={run_stats.detached_output} "
                f"({run_stats.detached_sessions} session"
                f"{'' if run_stats.detached_sessions == 1 else 's'}; "
                f"may include run delegators -- run total may be low)"
            )
        missing = run_stats.missing_branches(reducer.run)
        if missing:
            lines.append(f"  - matched no entries: {', '.join(missing)}")
    if reducer.unjoinable:
        # Records a parser saw but structurally could not join (TB-24): named here so
        # codex's ~4% web-search undercount is never a silent zero. Attributed by
        # agent/kind (TB-23's typed-bucket ethos), sorted for a stable diff. Absent
        # entirely when there is nothing to report.
        unjoinable_total = sum(reducer.unjoinable.values())
        lines.append(f"- Unjoinable tool records (seen, not joined): {unjoinable_total}")
        for (agent_name, kind), count in sorted(reducer.unjoinable.items()):
            lines.append(f"  - {agent_name}/{kind}: {count}")
    # Earned, not asserted (TB-31). This line used to render straight from the CLI flag,
    # so it printed "Subagents included: no" on the AgentsView path -- which never listed
    # a subagent and therefore could not have excluded one. Reporting the count actually
    # stamped at discovery means the claim is falsifiable: a `no` beside `0 of N` says
    # the index found no subagents, not that the filter did its job.
    subagent_note = f"{subagents_found} of {sessions_discovered} discovered"
    if include_subagents:
        lines.append(f"- Subagents included: yes ({subagent_note} are subagent sessions)")
    else:
        lines.append(f"- Subagents included: no ({subagent_note} excluded)")
    lines.append(f"- AgentsView fallback reason: {fallback_reason if fallback_reason else 'none'}")
    lines.append("- Note: --since is file-mtime based.")
    if since_note:
        lines.append(f"- --since value used: {since_note}")

    if verbose and skips:
        # Individual ids live here, never in the default report -- 1600 ids on one
        # line is what made the pre-TB-21 report impossible to tally (TB-21).
        lines.append("")
        lines.append("### Skipped sessions (detail)")
        lines.append("")
        for skip in skips:
            ident = skip.session_id or "(root)"
            lines.append(f"- {ident} [{skip.agent}] {skip.reason.value}: {skip.detail}")

    return "\n".join(lines) + "\n"
