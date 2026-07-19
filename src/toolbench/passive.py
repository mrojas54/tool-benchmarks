"""Passive analyzer CLI (S11-S15, S19, S23): discovery, scan loop, freeze.

Aggregation lives in `reducer.py`; markdown rendering in `report.py`. This module
owns argparse, session discovery, per-ref orchestration, and re-exports the
public symbols tests and docs historically imported from `toolbench.passive`.
"""

from __future__ import annotations

import argparse
import functools
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from toolbench.adapters import UnknownSchema
from toolbench.freeze import read_manifest, write_manifest
from toolbench.reducer import (
    OVERSIZED_OUTPUT_TOKENS,
    UNKNOWN_MODEL,
    AgentStats,
    Reducer,
    ToolStats,
)
from toolbench.registry import pick_adapter
from toolbench.report import (
    CorpusFingerprint,
    _sampling_notes,
    corpus_fingerprint,
    render_report,
    session_signature,
    tally_skips,
)
from toolbench.run_manifest import MalformedRunManifest, RunManifest, read_run_manifest
from toolbench.sources import (
    AGENTSVIEW_TIMEOUT_S,
    AgentCensus,
    AgentsViewTimeout,
    IndexSource,
    MissingSourceExport,
    NonTranscriptExport,
    Runner,
    SessionRef,
    SkipReason,
    SkipRecord,
    _run_agentsview,
    iter_sessions,
)
from toolbench.transcript import ParseResult

# Re-exports for `from toolbench.passive import …` callers.
__all__ = [
    "OVERSIZED_OUTPUT_TOKENS",
    "UNKNOWN_MODEL",
    "AgentStats",
    "CliArgs",
    "CorpusFingerprint",
    "Reducer",
    "ToolStats",
    "classify_skip",
    "corpus_fingerprint",
    "filter_subagents",
    "main",
    "parse_args",
    "render_report",
    "session_signature",
    "skip_record_for",
    "tally_skips",
]


def _apply_date_range(
    result: ParseResult, date_from: str | None, date_to: str | None
) -> ParseResult:
    """Filter a session's calls to `--date-from`/`--date-to` (per ToolCall.ts)."""
    if date_from is None and date_to is None:
        return result
    kept = [call for call in result.calls if _call_in_range(call.ts, date_from, date_to)]
    # Only `calls` is date-filterable. Every other field is session-grain and passes
    # through intact: `malformed` and `unjoinable` are counts of seen records (TB-24),
    # and `session_cache_read_tokens` is the S32 cache stat -- the session was still
    # measured even when all its calls fall outside the range (TB-25). `replace` keeps
    # all non-overridden fields automatically, so a field added later cannot be
    # silently dropped by a hand-listed reconstruction.
    return replace(result, calls=kept)


def _call_in_range(ts: str, date_from: str | None, date_to: str | None) -> bool:
    if not ts:
        return True
    date_part = ts[:10]
    if date_from is not None and date_part < date_from:
        return False
    if date_to is not None and date_part > date_to:
        return False
    return True


def filter_subagents(refs: list[SessionRef]) -> list[SessionRef]:
    """Remove sessions stamped `is_subagent` at discovery (S13, CQ 3.2)."""
    return [ref for ref in refs if not ref.is_subagent]


def classify_skip(exc: BaseException) -> SkipReason:
    """Map a caught skip exception to its typed reason (TB-23).

    The type information exists at the raise site; this reads it one frame later
    instead of destroying it into prose. `MissingSourceExport` and
    `NonTranscriptExport` are flat siblings, so their order here is irrelevant --
    only the bare-`RuntimeError`/`OSError` fallback must come last.
    """
    if isinstance(exc, MissingSourceExport):
        return SkipReason.MISSING_SOURCE
    if isinstance(exc, UnknownSchema):
        return SkipReason.UNKNOWN_SCHEMA
    if isinstance(exc, NonTranscriptExport):
        return SkipReason.NON_TRANSCRIPT
    if isinstance(exc, AgentsViewTimeout):
        # A daemon healthy at probe time can still hang on export #4000 of 8591. Typed
        # apart from EXPORT_FAILED because the remedy differs: a nonzero exit is a bad
        # session, a timeout is a sick daemon, and folding them together would hide a
        # scan-wide fault inside a per-session bucket (TB-32).
        return SkipReason.EXPORT_TIMEOUT
    if isinstance(exc, UnicodeDecodeError):
        return SkipReason.DECODE_ERROR
    return SkipReason.EXPORT_FAILED


def skip_record_for(ref: SessionRef, exc: BaseException) -> SkipRecord:
    """Stamp a skipped session with its identity and typed reason (TB-23)."""
    return SkipRecord(
        session_id=ref.session_id,
        agent=ref.agent,
        reason=classify_skip(exc),
        detail=str(exc),
    )


@dataclass
class CliArgs:
    """Parsed CLI flags (S12)."""

    agent: str
    all_projects: bool
    project: str | None
    since: str | None
    date_from: str | None
    date_to: str | None
    out: str | None
    limit: int | None
    exclude_subagents: bool
    index_source: IndexSource
    verbose: bool
    freeze: str | None
    run_manifest: str | None
    tickets: int | None
    agentsview_timeout: float


def _positive_int(raw: str) -> int:
    """`--tickets 0` cannot normalize (S39). Reject at parse rather than silently
    dropping the per-ticket line from the report."""
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("--tickets must be > 0 to normalize per ticket")
    return value


def _nonnegative_float(raw: str) -> float:
    """`--agentsview-timeout` accepts 0 but not a negative (TB-39).

    0 is meaningful -- it is the unbounded escape hatch, `timeout=None` -- so unlike
    `_positive_int` it is admitted. A NEGATIVE ceiling is not a policy choice but nonsense,
    and silently coercing it would leave the operator believing a bound they never got.
    """
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(
            "--agentsview-timeout must be >= 0 (0 means unbounded; negative is meaningless)"
        )
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise TypeError(f"expected str | None, got {type(value).__name__}")


def _cli_args_from_namespace(ns: argparse.Namespace) -> CliArgs:
    agent = ns.agent
    if not isinstance(agent, str):
        raise TypeError(f"--agent must be str, got {type(agent).__name__}")

    index_source = ns.index_source
    if index_source not in ("auto", "agentsview", "raw"):
        raise TypeError(f"--index-source must be an IndexSource, got {index_source!r}")

    limit = ns.limit
    if limit is not None and not isinstance(limit, int):
        raise TypeError(f"--limit must be int | None, got {type(limit).__name__}")

    exclude_subagents = ns.exclude_subagents
    if not isinstance(exclude_subagents, bool):
        raise TypeError(
            f"--exclude-subagents must be bool, got {type(exclude_subagents).__name__}"
        )

    verbose = ns.verbose
    if not isinstance(verbose, bool):
        raise TypeError(f"--verbose must be bool, got {type(verbose).__name__}")

    agentsview_timeout = ns.agentsview_timeout
    if not isinstance(agentsview_timeout, float):
        raise TypeError(
            f"--agentsview-timeout must be float, got {type(agentsview_timeout).__name__}"
        )

    project = _optional_str(ns.project)
    return CliArgs(
        agent=agent,
        all_projects=project is None,
        project=project,
        since=_optional_str(ns.since),
        date_from=_optional_str(ns.date_from),
        date_to=_optional_str(ns.date_to),
        out=_optional_str(ns.out),
        limit=limit,
        exclude_subagents=exclude_subagents,
        index_source=index_source,
        verbose=verbose,
        freeze=_optional_str(ns.freeze),
        run_manifest=_optional_str(ns.run_manifest),
        tickets=ns.tickets,
        agentsview_timeout=agentsview_timeout,
    )


def parse_args(argv: list[str] | None) -> CliArgs:
    parser = argparse.ArgumentParser(prog="toolbench.passive", description="Passive tool-usage analyzer.")
    parser.add_argument("--agent", default="all")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", default=False)
    scope.add_argument("--project", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--date-from", default=None)
    parser.add_argument("--date-to", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--exclude-subagents", action="store_true", default=False)
    parser.add_argument("--index-source", choices=("auto", "agentsview", "raw"), default="auto")
    parser.add_argument(
        "--agentsview-timeout",
        type=_nonnegative_float,
        default=AGENTSVIEW_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            "Per-call ceiling on every `agentsview` subprocess (probe, listing, census, "
            f"export). Default {AGENTSVIEW_TIMEOUT_S}s. 0 means unbounded: a hung daemon "
            "will block the run forever (TB-32), and the report says so."
        ),
    )
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument(
        "--freeze",
        default=None,
        metavar="MANIFEST",
        help="Pin the discovered corpus: write the ref list once, replay it on "
        "later runs, and name refs that have since vanished (TB-22).",
    )
    parser.add_argument("--run-manifest", default=None)
    parser.add_argument("--tickets", type=_positive_int, default=None)
    return _cli_args_from_namespace(parser.parse_args(argv))


def _probe_truncation(refs_iter: Iterator[SessionRef], *, exclude_subagents: bool) -> bool | None:
    """Did the limit leave a REPORTED session behind? True / False / cannot say (roborev #103).

    Asked only once the ref loop has already broken on `--limit`, so everything this run
    was asked for is in hand and this call is pure diagnostic. Two things follow from that,
    and both are the whole point of the function.

    IT ASKS ABOUT THE RIGHT POPULATION. The listing always yields children -- they must be
    discovered before `filter_subagents` can drop them (`_ALL_INCLUDES`, sources.py) -- so
    under `--exclude-subagents` the ref just past the limit may be one the report never
    counts. Stopping at it would blame `--limit` for cutting a population it did not cut,
    which is the inference-from-absence TB-33 exists to delete, wearing a new hat. So the
    probe skips exactly what the report skips and walks on to the first ref that COUNTS.
    Cost is one ref in the ordinary case; the pathological case (every remaining ref a
    child) walks the tail, and that is the price of an answer about the real population.

    IT CANNOT BE FATAL. A fresh page may fail -- a dead daemon, a source that vanished --
    and a run that already holds all of its refs must not die on, or be rewritten by, its
    own diagnostic. Previously a `RuntimeError` here killed a complete run outright, and a
    `FileNotFoundError` was caught by the guard meant for a source vanishing DURING
    discovery, which fabricated a MISSING_SOURCE skip and zeroed the census of a sample
    that was never in doubt. A failed probe returns `None`: not truncated, not un-truncated,
    NOT OBSERVED. `False` would be a measurement nobody took, and the report renders it as
    "`--limit N` truncated nothing".
    """
    try:
        for ref in refs_iter:
            if exclude_subagents and ref.is_subagent:
                continue
            return True
        return False
    except (OSError, RuntimeError, ValueError):
        # Exactly what the discovery layer raises when a page cannot be read: OSError
        # (FileNotFoundError, a vanished root), RuntimeError (agentsview exited non-zero),
        # ValueError (json.JSONDecodeError on a garbled page). Scoped to the probe -- a
        # failure DURING ref collection still raises, because that sample is incomplete.
        return None


def _collect_refs(
    refs_iter: Iterator[SessionRef], limit: int | None, exclude_subagents: bool
) -> tuple[list[SessionRef], bool | None]:
    """Drain `refs_iter` into a list, observing `--limit` truncation (S23, roborev #103).

    Its own local `refs` list is the mechanism, not an incidental detail: if `refs_iter`
    raises partway through, this function never returns, so whatever the caller had
    assigned before the call is left untouched rather than half-updated. `_discover_refs`
    leans on exactly that to discard a partial agentsview listing on a mid-listing
    failure (TB-38) without writing a separate reset.
    """
    refs: list[SessionRef] = []
    limit_truncated: bool | None = False
    for ref in refs_iter:
        refs.append(ref)
        if limit is not None and len(refs) >= limit:
            # Truncation is OBSERVED here, never inferred from the flag (roborev
            # #98/#101). `--limit 9000` over an 8778-session archive stops the loop and
            # cuts nothing, so `limit is not None` cannot license the report to blame
            # the limit for anything. Asking the listing for one more ref that the
            # report would COUNT settles it -- see `_probe_truncation`, which owns both
            # the population question and the promise that this diagnostic can never
            # take down the complete run it is describing (roborev #103).
            limit_truncated = _probe_truncation(refs_iter, exclude_subagents=exclude_subagents)
            break
    return refs, limit_truncated


def _discover_refs(
    args: CliArgs, root: str, runner: Runner | None
) -> tuple[list[SessionRef], str | None, list[SkipRecord], AgentCensus, bool | None]:
    """Resolve the index-source policy into a bounded list of refs (S10, S23, TB-38).

    The `iter_sessions(...)` CALL itself belongs inside the try block, not just the
    ref-iteration loop that follows it. `discover_agentsview` (TB-33) runs its
    parent-probe pass and per-agent census EAGERLY -- before it hands back a single
    ref -- because the caller can break out of the ref loop early on `--limit`, and a
    lazily-gathered census would then be missing exactly when it is needed. That
    eagerness means a source failure from a mid-run agentsview disappearance can
    surface from the `iter_sessions(...)` call itself, not only from iterating its
    result -- both `except` clauses below cover the call and the loop together for
    exactly that reason.

    `FileNotFoundError` (the source vanished outright -- binary removed, root gone)
    and `RuntimeError`/`AgentsViewTimeout`/`ValueError` (the daemon answered
    `_probe_agentsview`'s health check and then broke -- nonzero exit, a hang, or a
    malformed JSON payload -- somewhere in the pagination that followed, TB-38) get
    different treatment on purpose: a vanished
    source has no raw root to fall back to any more reliably than agentsview itself,
    so it degrades to a named `MISSING_SOURCE` skip with no denominator. A daemon that
    was merely unhealthy mid-listing has no such excuse -- the raw filesystem is still
    right there -- so `auto` mode discards whatever partial agentsview refs this
    attempt collected (TB-22: a spliced partial listing has no coherent identity) and
    rescans wholesale from `raw`, via the very `index_source="raw"` semantics
    `iter_sessions` already implements, rather than a second hand-rolled raw path.
    """
    project = None if args.all_projects else args.project
    page_limit = args.limit if args.limit is not None else 500

    refs: list[SessionRef] = []
    skips: list[SkipRecord] = []
    fallback_reason: str | None = None
    limit_truncated: bool | None = False
    census: AgentCensus
    try:
        refs_iter, fallback_reason, census = iter_sessions(
            index_source=args.index_source,
            agent=args.agent,
            project=project,
            since=args.since,
            limit=page_limit,
            root=root,
            runner=runner,
            include_subagents=not args.exclude_subagents,
        )
        refs, limit_truncated = _collect_refs(refs_iter, args.limit, args.exclude_subagents)
    except FileNotFoundError as exc:
        if args.index_source == "auto":
            # A root-level failure has no per-session ref; the absent raw fallback
            # root -- or, eagerly (TB-33), an agentsview that answered the initial
            # availability probe but vanished during the parent-probe/census calls
            # that now run inside `iter_sessions(...)` -- is itself a missing source
            # (TB-23). Discovery never completed, so there is no census either --
            # never a fabricated measured zero, always a named reason (TB-33).
            skips.append(
                SkipRecord(
                    session_id="",
                    agent=args.agent,
                    reason=SkipReason.MISSING_SOURCE,
                    detail=str(exc),
                )
            )
            census = AgentCensus(
                totals={},
                archive_total=0,
                unavailable_reason=f"discovery source vanished mid-run: {exc}",
            )
        else:
            raise
    except (RuntimeError, ValueError) as exc:
        if args.index_source != "auto":
            # An explicit `--index-source agentsview` is a demand, not a preference
            # (mirrors `test_explicit_agentsview_does_not_swallow_a_timeout`): the
            # failure must surface, for `main`'s outer guard to report fatally.
            raise
        # TB-38: the probe passed but the daemon broke somewhere in the pagination
        # that followed -- inside `discover_agentsview`'s eager parent-probe pass
        # (part of the `iter_sessions(...)` call above) or lazily while `_yield_refs`
        # was iterated (part of the loop `_collect_refs` just ran). Either way this
        # attempt's refs are discarded (see `_collect_refs`'s docstring) and typed
        # with the real cause -- `classify_skip` already maps `AgentsViewTimeout` to
        # `EXPORT_TIMEOUT` and a bare nonzero-exit `RuntimeError` to `EXPORT_FAILED`,
        # never the `MISSING_SOURCE` a vanished source gets above.
        skips.append(
            SkipRecord(
                session_id="",
                agent=args.agent,
                reason=classify_skip(exc),
                detail=str(exc),
            )
        )
        raw_refs_iter, _raw_reason, census = iter_sessions(
            index_source="raw",
            agent=args.agent,
            project=project,
            since=args.since,
            limit=page_limit,
            root=root,
            runner=runner,
            include_subagents=not args.exclude_subagents,
        )
        refs, limit_truncated = _collect_refs(raw_refs_iter, args.limit, args.exclude_subagents)
        fallback_reason = f"agentsview failed mid-listing, degraded to raw: {exc}"
    return refs, fallback_reason, skips, census, limit_truncated


def _parse_ref(ref: SessionRef, runner: Runner | None) -> ParseResult:
    """Uniformly parse any session (S11 wiring).

    Every branch this function used to own now lives in the registry: hermes
    claims by source, everything else is content-detected. An unrecognized
    schema raises `UnknownSchema` (a RuntimeError), which `main`'s per-session
    guard demotes to `skipped_roots` -- so an unparseable agent is named in the
    Summary instead of reported as an agent that did no tool work (TB-12).
    """
    return pick_adapter(ref, runner).parse(ref)


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
    root: str = "~/.claude/projects",
) -> int:
    args = parse_args(argv)

    # `--freeze` pins the discovered set: absent manifest -> discover and write it
    # once; present manifest -> replay it, bypassing live discovery so the input
    # set cannot drift between runs (TB-22, S37).
    freeze_path = args.freeze
    replaying = freeze_path is not None and Path(freeze_path).expanduser().exists()

    # Bind --agentsview-timeout to the DEFAULT runner, once, here (TB-39). This is the sole
    # place the default is chosen: both consumers (iter_sessions, and AgentsViewLoader via
    # pick_adapter) fall back to `_run_agentsview` independently when `runner is None`, so
    # binding it here reaches all four call sites with no new plumbing. `partial` still
    # satisfies `Runner = Callable[[list[str]], CompletedProcess[str]]`.
    #
    # An EXPLICITLY injected runner is never wrapped: the flag configures the default, it
    # does not override the seam. Every test in this suite injects one, and wrapping those
    # would quietly change what they exercise.
    if runner is None:
        runner = functools.partial(
            _run_agentsview,
            timeout=args.agentsview_timeout if args.agentsview_timeout > 0 else None,
        )

    refs: list[SessionRef]
    fallback_reason: str | None
    skips: list[SkipRecord]
    census: AgentCensus
    # A replay pulls its refs from the manifest, not from a limited listing, so nothing was
    # truncated by a limit on this path. It has no census either, which suppresses the
    # sampling notes outright -- but the flag is set honestly rather than left to a default.
    # `False` (not `None`) is the earned value: replay TRULY truncates nothing, whereas
    # `None` would claim a probe was attempted and could not answer (roborev #103).
    limit_truncated: bool | None = False
    # Set only on a v2 replay whose manifest carried a real census (TB-37): the caveat
    # that the fractions below are HISTORICAL, not live. `render_report` renders it
    # right beside the sampling notes it qualifies -- never left implicit alongside a
    # number that could otherwise read as "current" (see the else-branch comment below
    # for why a v1/censusless replay does not set this).
    frozen_census_note: str | None = None
    if replaying:
        assert freeze_path is not None
        manifest = read_manifest(freeze_path)
        refs, fallback_reason, skips = manifest.refs, None, []
        if manifest.census is None:
            # A freeze pins the REF LIST, not the archive it was drawn from (TB-22): a v1
            # manifest never had a census to lose, and a v2 manifest can still be written
            # without one (e.g. discovery's own census attempt failed at freeze time, see
            # below). Named by the MANIFEST VERSION specifically, not by "freezing" in
            # general (TB-37), so a future format gap reads as its own gap and not this
            # one's.
            census = AgentCensus(
                totals={},
                archive_total=0,
                unavailable_reason=(
                    f"frozen corpus replay ({freeze_path}): manifest format "
                    f"{manifest.version} recorded no archive census; no denominator was "
                    "recorded at freeze time"
                ),
            )
        elif manifest.census.unavailable_reason is not None:
            # The census itself failed AT FREEZE TIME (e.g. discovery's own census call
            # errored). Propagated, not laundered into the generic "no denominator" text
            # above -- that would misname a measurement that was ATTEMPTED AND FAILED as
            # one that was never attempted.
            census = AgentCensus(
                totals={},
                archive_total=0,
                unavailable_reason=(
                    f"frozen corpus replay ({freeze_path}): the census recorded at "
                    f"freeze time was itself unavailable: {manifest.census.unavailable_reason}"
                ),
            )
        elif manifest.census_includes_subagents is None:
            census = AgentCensus(
                totals={},
                archive_total=0,
                unavailable_reason=(
                    f"frozen corpus replay ({freeze_path}): manifest format "
                    f"{manifest.version} recorded a census without its subagent "
                    "population filter"
                ),
            )
        elif manifest.census_includes_subagents != (not args.exclude_subagents):
            frozen_population = (
                "included subagents"
                if manifest.census_includes_subagents
                else "excluded subagents"
            )
            replay_population = (
                "includes subagents" if not args.exclude_subagents else "excludes subagents"
            )
            census = AgentCensus(
                totals={},
                archive_total=0,
                unavailable_reason=(
                    f"frozen corpus replay ({freeze_path}): the freeze-time census "
                    f"{frozen_population}, but this replay {replay_population}"
                ),
            )
        else:
            # A real census survived the freeze (TB-37): the fractions below are REAL,
            # but HISTORICAL -- the archive size as of freeze time, not today's. That
            # caveat is wired through `frozen_census_note` to `render_report`, which
            # renders it beside the sampling notes it qualifies, so a v2 census can never
            # read as "current" the way a v1 replay's silent absence used to (TB-33's
            # honesty floor, raised).
            census = manifest.census
            frozen_census_note = (
                "- **Historical denominator**: the archive census above was recorded at "
                f"freeze time ({freeze_path}), not re-measured for this replay. The live "
                "archive has almost certainly changed size since -- these fractions "
                "describe the corpus as it was WHEN FROZEN, not the archive today."
            )
    else:
        try:
            refs, fallback_reason, skips, census, limit_truncated = _discover_refs(
                args, root, runner
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"toolbench.passive: fatal source error: {exc}", file=sys.stderr)
            return 1
        if freeze_path is not None:
            write_manifest(
                freeze_path,
                refs,
                corpus_fingerprint(r.session_id for r in refs).digest,
                census=census,
                census_includes_subagents=not args.exclude_subagents,
            )

    # Counted before the filter runs, on both the discovery and the replay path -- these
    # are what the provenance line reports, so they must describe the corpus as it was
    # found, not as it was left (TB-31).
    sessions_discovered = len(refs)
    subagents_found = sum(1 for ref in refs if ref.is_subagent)

    if args.exclude_subagents:
        refs = filter_subagents(refs)

    # AFTER the filter, unlike the two provenance counts above (TB-35). The census's
    # `includes` track the POST-filter population (TB-33 Finding 1), so a pre-filter count
    # here would put parents-plus-children over a parents-only denominator and re-open the
    # very bug that finding closed. `census.totals[a] - sampled_by_agent[a]` is then the
    # number of a's sessions `--limit` never pulled: both sides observed, neither inferred.
    sampled_by_agent = Counter(ref.agent for ref in refs)

    run: RunManifest | None = None
    if args.run_manifest is not None:
        try:
            run = read_run_manifest(args.run_manifest)
        except (MalformedRunManifest, OSError) as exc:
            # S23: a bad manifest is a hard stop -- silently scanning without a run
            # would print a corpus report the operator would read as a run report.
            print(f"error: {exc}", file=sys.stderr)
            return 1

    reducer = Reducer(run=run)
    scanned_sigs: list[str] = []
    for ref in refs:
        if args.verbose:
            print(f"scanning {ref.session_id} ({ref.source})", file=sys.stderr)
        try:
            result = _parse_ref(ref, runner)
        except (OSError, RuntimeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError subclasses ValueError, not OSError/RuntimeError.
            # In-tree readers now decode leniently, so this only fires for a caller
            # who injects a strict-decode `runner`; catching it keeps one bad
            # session from taking the corpus down with it. The reason is typed at the
            # raise site and read back here rather than stringified away (TB-23).
            skips.append(skip_record_for(ref, exc))
            continue
        filtered = _apply_date_range(result, args.date_from, args.date_to)
        reducer.absorb(ref.agent, filtered)
        # Signature after date filtering: the report counts these calls, so the
        # fingerprint must fold the same post-filter count -- plus the malformed and
        # unjoinable counts, which the Summary also renders and date-filtering leaves
        # intact (S36, TB-24).
        scanned_sigs.append(
            session_signature(
                ref.session_id,
                len(filtered.calls),
                filtered.malformed,
                sum(filtered.unjoinable.values()),
            )
        )

    fingerprint = corpus_fingerprint(scanned_sigs)

    freeze_note: str | None = None
    if freeze_path is not None:
        if replaying:
            # A frozen ref that no longer loads (missing_source) has vanished from
            # disk since the freeze -- name the count so a shrinking scanned set is
            # never mistaken for a code effect (TB-22).
            vanished = sum(1 for s in skips if s.reason is SkipReason.MISSING_SOURCE)
            freeze_note = (
                f"Replaying frozen corpus: {freeze_path} ({vanished} vanished since freeze)"
            )
        else:
            freeze_note = f"Corpus frozen to: {freeze_path}"

    if reducer.calls_joined == 0:
        if skips:
            ranked = sorted(
                tally_skips(skips).items(), key=lambda kv: (-kv[1], kv[0].value)
            )
            tally = ", ".join(f"{r.value}={c}" for r, c in ranked)
            suffix = f" (skipped {len(skips)}: {tally})"
        else:
            suffix = ""
        lines = [f"toolbench.passive: no sessions matched the given selection.{suffix}"]
        # TB-34: the run already built a full `AgentCensus` before this early return --
        # `census.totals`/`archive_total`/`residual` are all in hand, and discarding
        # them here is the exact disclosure gap TB-33 exists to close, just relocated
        # to the one path TB-33 never reached. `_sampling_notes` already knows how to
        # render that census (unreached agents, an all-skipped agent, an unenumerated
        # residual) from these same six arguments -- reused rather than reinvented, so
        # a narrow window is never silently indistinguishable from a truly empty
        # archive. Additive only: the "no sessions matched" line above never changes.
        lines.extend(
            _sampling_notes(
                reducer, census, skips, args.limit, limit_truncated, dict(sampled_by_agent)
            )
        )
        print("\n".join(lines))
        return 0

    report = render_report(
        reducer,
        index_source=args.index_source,
        fallback_reason=fallback_reason,
        skips=skips,
        include_subagents=not args.exclude_subagents,
        subagents_found=subagents_found,
        sessions_discovered=sessions_discovered,
        since_note=args.since,
        census=census,
        verbose=args.verbose,
        fingerprint=fingerprint,
        freeze_note=freeze_note,
        frozen_census_note=frozen_census_note,
        run_tickets=args.tickets,
        limit=args.limit,
        limit_truncated=limit_truncated,
        sampled_by_agent=dict(sampled_by_agent),
        # `None` = "agentsview was never called, so its timeout is not a fact about this
        # report" -- a raw-only scan or a freeze replay. Disclosing a ceiling that governed
        # nothing would be misdirection, so the value is withheld rather than defaulted
        # (TB-39; same discipline as `limit_truncated`'s earned False, roborev #103).
        agentsview_timeout=(
            None if replaying or args.index_source == "raw" else args.agentsview_timeout
        ),
    )
    if args.out:
        Path(args.out).write_text(report)
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
