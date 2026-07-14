"""Passive analyzer CLI (S11-S15, S19, S23): discovery, scan loop, freeze.

Aggregation lives in `reducer.py`; markdown rendering in `report.py`. This module
owns argparse, session discovery, per-ref orchestration, and re-exports the
public symbols tests and docs historically imported from `toolbench.passive`.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
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
    corpus_fingerprint,
    render_report,
    session_signature,
    tally_skips,
)
from toolbench.run_manifest import MalformedRunManifest, RunManifest, read_run_manifest
from toolbench.sources import (
    AgentCensus,
    IndexSource,
    MissingSourceExport,
    NonTranscriptExport,
    Runner,
    SessionRef,
    SkipReason,
    SkipRecord,
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


def _positive_int(raw: str) -> int:
    """`--tickets 0` cannot normalize (S39). Reject at parse rather than silently
    dropping the per-ticket line from the report."""
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("--tickets must be > 0 to normalize per ticket")
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


def _discover_refs(
    args: CliArgs, root: str, runner: Runner | None
) -> tuple[list[SessionRef], str | None, list[SkipRecord], AgentCensus]:
    """Resolve the index-source policy into a bounded list of refs (S10, S23).

    The `iter_sessions(...)` CALL itself now belongs inside the try block, not just
    the ref-iteration loop that follows it. `discover_agentsview` (TB-33) runs its
    parent-probe pass and per-agent census EAGERLY -- before it hands back a single
    ref -- because the caller can break out of the ref loop early on `--limit`, and a
    lazily-gathered census would then be missing exactly when it is needed. That
    eagerness means a `FileNotFoundError` from a mid-run agentsview disappearance can
    now surface from the `iter_sessions(...)` call itself, not only from iterating its
    result. If that call sat outside this guard, `auto` mode would lose its graceful
    degrade to a `MISSING_SOURCE` skip and `main` would treat a transient agentsview
    vanish as a fatal source error instead of exit 0.
    """
    project = None if args.all_projects else args.project
    page_limit = args.limit if args.limit is not None else 500

    refs: list[SessionRef] = []
    skips: list[SkipRecord] = []
    fallback_reason: str | None = None
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
        for ref in refs_iter:
            refs.append(ref)
            if args.limit is not None and len(refs) >= args.limit:
                break
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
    return refs, fallback_reason, skips, census


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

    refs: list[SessionRef]
    fallback_reason: str | None
    skips: list[SkipRecord]
    census: AgentCensus
    if replaying:
        assert freeze_path is not None
        manifest = read_manifest(freeze_path)
        refs, fallback_reason, skips = manifest.refs, None, []
        # A freeze pins the REF LIST, not the archive it was drawn from (TB-22), so no
        # denominator exists on replay. Persisting one into the manifest would be a
        # format change this ticket does not own -- and an unstated "unknown" is exactly
        # the silence TB-33 exists to break, so it is stated instead.
        census = AgentCensus(
            totals={},
            archive_total=0,
            unavailable_reason=(
                f"frozen corpus replay ({freeze_path}): no denominator was recorded at "
                "freeze time"
            ),
        )
    else:
        try:
            refs, fallback_reason, skips, census = _discover_refs(args, root, runner)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"toolbench.passive: fatal source error: {exc}", file=sys.stderr)
            return 1
        if freeze_path is not None:
            write_manifest(
                freeze_path, refs, corpus_fingerprint(r.session_id for r in refs).digest
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
        print(f"toolbench.passive: no sessions matched the given selection.{suffix}")
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
        run_tickets=args.tickets,
        limit=args.limit,
        sampled_by_agent=dict(sampled_by_agent),
    )
    if args.out:
        Path(args.out).write_text(report)
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
