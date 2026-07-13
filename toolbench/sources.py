"""Multi-agent session discovery (S7-S10). Stdlib only — shells to AgentsView."""

from __future__ import annotations

import json
import re
import subprocess
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

IndexSource = Literal["auto", "agentsview", "raw"]
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

# Every exclusion AgentsView applies by default. The design contract
# (docs/2026-07-07-tool-benchmarks-design.md) mandated all three from the start; the
# implementation passed none, and lost 70% of the archive to the omission (TB-30).
_ALL_INCLUDES = ("--include-children", "--include-automated", "--include-one-shot")

# The same listing with `--include-children` withheld. Differs from `_ALL_INCLUDES` in
# exactly one flag, so the difference between the two listings is exactly the set of
# child sessions -- AgentsView classifying its own taxonomy, rather than us inferring
# it from row fields that cannot support the inference (TB-31).
_PROBE_INCLUDES = ("--include-automated", "--include-one-shot")

# "Excluded 7497 sessions by default: 7435 one-shot, 62 automated." -- written to
# STDERR, which the old code never read, while it parsed only stdout.
_EXCLUSION_BANNER = re.compile(r"Excluded\s+(\d+)\s+sessions?\s+by default", re.IGNORECASE)

# Bytes sniffed to classify a payload as text or binary. A NUL cannot appear in a
# JSONL transcript, so it is a sound discriminator rather than a heuristic.
SNIFF_LEN = 8192

# `agentsview session export` writes this to stderr when it lists a session whose
# transcript is no longer on disk. Matched to raise a typed MissingSourceExport so
# the reason is decided where the evidence lives, not by regex on the report (TB-23).
_MISSING_SOURCE_MARKER = "source file not found"


class AgentsViewExclusionWarning(UserWarning):
    """AgentsView dropped sessions from the corpus that we explicitly asked it to keep.

    Not an error -- the run's numbers are still internally consistent -- but the
    population they describe is not the one the operator asked for, and a benchmark that
    quietly measures a subset is worse than one that fails (TB-30).
    """


class NonTranscriptExport(RuntimeError):
    """A session payload is not a JSONL transcript (e.g. `agentsview session export`
    returns a whole SQLite database for hermes cron sessions, with returncode 0).

    Subclasses RuntimeError so `passive.main` demotes the session to `skipped_roots`
    via its existing per-session guard, instead of absorbing megabytes of binary as
    'malformed lines'.
    """


class MissingSourceExport(RuntimeError):
    """AgentsView lists a session whose transcript no longer exists on disk.

    A DEAD INDEX ENTRY, not a data error in this repo: driven by external retention
    (TB-22). Subclasses RuntimeError so `passive.main`'s per-session guard demotes it,
    but deliberately NOT `NonTranscriptExport` -- "the file is gone" and "the file is
    binary" are different diagnoses, and flat siblings keep `classify_skip` (TB-23)
    unambiguous.
    """


class SkipReason(StrEnum):
    """Why a discovered session never reached the reducer (TB-23).

    Typed at the raise site so `passive` can answer "how many sessions have no
    parser?" without a regex over its own rendered prose. Mirrors the
    `UsageProvenance` enum (S29): type the absence rather than stringify it.
    """

    MISSING_SOURCE = "missing_source"  # dead index entry; transcript gone from disk (TB-22)
    UNKNOWN_SCHEMA = "unknown_schema"  # no registered parser claimed it (S28) -- the actionable bucket
    NON_TRANSCRIPT = "non_transcript"  # NUL-sniff rejected a binary/SQLite export (TB-10)
    DECODE_ERROR = "decode_error"      # a strict-decode runner hit invalid UTF-8
    EXPORT_FAILED = "export_failed"    # `export` returned non-zero for some other reason


@dataclass(frozen=True)
class SkipRecord:
    """A skipped session, tagged with its identity and a machine-readable reason.

    `detail` preserves the original message for `--verbose` / sidecar output (TB-21);
    it is never parsed to recover `reason` -- that is what `reason` is for.
    """

    session_id: str
    agent: str
    reason: SkipReason
    detail: str


@dataclass(frozen=True)
class AgentCensus:
    """Per-agent archive population, measured at discovery (TB-33).

    Discovery-grain: the reducer counts CALLS, and a denominator is not a call, so this
    never enters `reducer.py`.

    `totals` and `archive_total` are gathered under THIS RUN'S filters. A denominator
    gathered under different filters describes a different population than the numerator,
    and the fraction becomes a lie with a decimal point on it -- the same invariant the
    TB-31 parent probe carries, which is why every census call is built by `_list_argv`
    rather than hand-assembled.

    `unavailable_reason` types the ABSENCE of a denominator (a failed census call, or a
    frozen-corpus replay that recorded none) rather than signalling it with an empty dict.
    The report can then say WHY it cannot disclose a fraction instead of quietly dropping
    the column -- which is the exact sin this ticket exists to close. Same habit as
    `SkipReason` and `UsageProvenance`: type the absence, never imply it.
    """

    totals: dict[str, int]
    archive_total: int
    unavailable_reason: str | None = None

    @property
    def residual(self) -> int:
        """Archive sessions belonging to no agent we enumerated.

        The probe listing excludes children, so the agent universe it yields is "agents
        with >= 1 non-child session"; an agent whose sessions are ALL children is
        invisible to it. Hardcoding a known-agent list to close that hole would rebuild
        the TB-30 failure mode one layer up -- a NEW agent would then silently vanish. So
        we reconcile and name what is left over instead (TB-21/TB-28: report the gap,
        never a silent zero). Zero on the live archive today; the net exists for the day
        it is not.
        """
        return self.archive_total - sum(self.totals.values())


def _looks_binary(sample: str) -> bool:
    return "\x00" in sample


@dataclass
class SessionRef:
    """A uniform discovery record across raw filesystem scans and AgentsView (S7-S9)."""

    agent: str
    source: str
    project: str
    session_id: str
    path: str | None
    # Set at discovery (CQ 3.2). Raw scans stamp it from the path layout; AgentsView
    # refs from the parent-probe set difference (TB-31), since the index exposes no
    # parent/child field. Stamping has to be truthful at discovery for BOTH sources:
    # `freeze` persists this flag, and its stale-`false` self-heal (freeze.py) can only
    # fall back to the path -- which AgentsView refs do not have.
    is_subagent: bool = False


def _project_and_subagent(root: Path, path: Path) -> tuple[str, bool]:
    """Owning project = first segment under `root`; subagent = nested under `subagents/`.

    TB-29: this tested `rel.parts[1] == "subagents"`, but the real layout is
    <project>/<session-uuid>/subagents/agent-*.jsonl -- parts[1] is the session UUID,
    so the flag was never True and `--exclude-subagents` silently included them while
    the report claimed otherwise. Match on any segment BETWEEN the project and the
    filename so the check does not re-break if the nesting depth changes again.
    """
    rel = path.relative_to(root)
    if not rel.parts:
        raise ValueError(f"session path is not under root: {path}")
    project = rel.parts[0]
    is_subagent = "subagents" in rel.parts[1:-1]
    return project, is_subagent


def _run_agentsview(argv: list[str]) -> subprocess.CompletedProcess[str]:
    # errors="replace": a session export carrying a stray non-UTF-8 byte must not
    # raise out of communicate() and abort the whole corpus scan (S9).
    return subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)


def iter_session_files(
    root: str = "~/.claude/projects",
    project: str | None = None,
    since: str | None = None,
) -> Iterator[Path]:
    """Yield Claude Code JSONL session files under `root` (S7)."""
    base = Path(root).expanduser()
    if not base.is_dir():
        raise FileNotFoundError(f"raw session root not found: {base}")
    since_ts = datetime.fromisoformat(since).timestamp() if since is not None else None
    for path in sorted(base.rglob("*.jsonl")):
        if project is not None:
            # Match the owning project dir by equality on the first segment under
            # root (CQ 3.2). Subagent transcripts live at <project>/subagents/*.jsonl
            # and still match because their first segment is the project, not
            # "subagents".
            rel = path.relative_to(base)
            if len(rel.parts) < 2 or rel.parts[0] != project:
                continue
        if since_ts is not None and path.stat().st_mtime < since_ts:
            continue
        yield path


def _list_argv(
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
    includes: tuple[str, ...],
    cursor: str | None = None,
) -> list[str]:
    """The one place a `session list` argv is built.

    Sole builder BY DESIGN (TB-33): the census denominators and the discovery numerators
    must carry identical filters or they describe different populations. Routing both
    through here makes that invariant structural instead of a comment two functions apart.
    """
    argv = ["agentsview", "session", "list", "--json", "--limit", str(limit), *includes]
    if agent != "all":
        argv += ["--agent", agent]
    if project is not None:
        argv += ["--project", project]
    if since is not None:
        argv += ["--date-from", since]
    if cursor:
        argv += ["--cursor", cursor]
    return argv


def _agentsview_pages(
    runner: Runner,
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
    includes: tuple[str, ...],
) -> Iterator[tuple[Any, str]]:
    """Yield `(payload, stderr)` for each cursor page of one `session list` pass."""
    cursor: str | None = None
    while True:
        result = runner(
            _list_argv(
                agent=agent,
                project=project,
                since=since,
                limit=limit,
                includes=includes,
                cursor=cursor,
            )
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"agentsview session list failed ({result.returncode}): {result.stderr.strip()}"
            )
        payload = json.loads(result.stdout)
        yield payload, result.stderr
        cursor = payload.get("next_cursor") or None
        if not cursor:
            break


def _probe_pass(
    runner: Runner,
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
) -> tuple[set[str], set[str]]:
    """One drain of the child-excluded listing -> `(parent_ids, agents_seen)`.

    This pass ALREADY ran -- TB-31 needs `parent_ids` to classify children -- and it threw
    the agent names on the floor. Returning them is what makes the TB-33 census cost zero
    extra pagination.
    """
    parent_ids: set[str] = set()
    agents_seen: set[str] = set()
    for payload, _ in _agentsview_pages(
        runner, agent=agent, project=project, since=since, limit=limit, includes=_PROBE_INCLUDES
    ):
        for entry in payload.get("sessions", []):
            parent_ids.add(entry["id"])
            agents_seen.add(entry["agent"])
    return parent_ids, agents_seen


def _list_total(
    runner: Runner, *, agent: str, project: str | None, since: str | None
) -> int:
    """The `total` for one scoped listing. `--limit 1` because we want the COUNT, not rows."""
    result = runner(
        _list_argv(agent=agent, project=project, since=since, limit=1, includes=_ALL_INCLUDES)
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agentsview session list failed ({result.returncode}): {result.stderr.strip()}"
        )
    total = json.loads(result.stdout).get("total")
    if not isinstance(total, int):
        raise RuntimeError(f"agentsview session list returned no usable `total`: {total!r}")
    return total


def _agent_census(
    runner: Runner,
    agents_seen: set[str],
    *,
    agent: str,
    project: str | None,
    since: str | None,
) -> AgentCensus:
    """One scoped `--limit 1` per agent, plus the run-scoped archive total (TB-33).

    `archive_total` inherits the run's `--agent`, and that is load-bearing: under
    `--agent codex` the run's population IS codex, so an UNSCOPED archive total would
    compute a residual of every other agent's sessions and scream about thousands of
    "unenumerated" sessions that were never in scope.
    """
    totals = {
        a: _list_total(runner, agent=a, project=project, since=since) for a in sorted(agents_seen)
    }
    archive_total = _list_total(runner, agent=agent, project=project, since=since)
    return AgentCensus(totals=totals, archive_total=archive_total)


def _yield_refs(
    runner: Runner,
    parent_ids: set[str],
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
) -> Iterator[SessionRef]:
    """The full listing, stamped with TB-31's child classification."""
    warned = False
    for payload, stderr in _agentsview_pages(
        runner, agent=agent, project=project, since=since, limit=limit, includes=_ALL_INCLUDES
    ):
        if not warned and (excluded := _EXCLUSION_BANNER.search(stderr)):
            # We opted into every exclusion AgentsView documents, so a banner here means
            # it dropped sessions we did not ask it to drop -- a new default, silently
            # shrinking the corpus. Discarding this banner is precisely how TB-30 hid for
            # as long as it did, so it never goes unsaid again.
            warned = True
            warnings.warn(
                f"agentsview excluded {excluded.group(1)} sessions from the corpus despite "
                f"--include-children/--include-automated/--include-one-shot; the benchmark "
                f"population is incomplete: {stderr.strip()}",
                AgentsViewExclusionWarning,
                stacklevel=2,
            )
        for entry in payload.get("sessions", []):
            yield SessionRef(
                agent=entry["agent"],
                source="agentsview",
                project=entry["project"],
                session_id=entry["id"],
                path=None,
                is_subagent=entry["id"] not in parent_ids,
            )


def discover_agentsview(
    runner: Runner,
    *,
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> tuple[AgentCensus, Iterator[SessionRef]]:
    """Census + refs (TB-30, TB-31, TB-33).

    Three questions, and AgentsView is the only thing that can answer any of them.

    WHAT WE ARE ALLOWED TO SEE (TB-30). AgentsView excludes one-shot, automated, and child
    sessions BY DEFAULT. Omitting the three `--include-*` flags cost 70% of the live
    archive, and -- fatally for a benchmark whose whole purpose is comparing agents -- it
    cost each agent a DIFFERENT fraction. Every cross-agent number was computed over
    incomparable populations, and nothing said so.

    WHICH OF THEM ARE SUBAGENTS (TB-31). The session-list row exposes no parent/child
    field, and every field-derived predicate is wrong. So we do not guess: the parent probe
    repeats the listing with `--include-children` withheld, and anything in the full listing
    but not in the probe is a child BY AGENTSVIEW'S OWN DEFINITION.

    HOW MUCH OF EACH AGENT WE ACTUALLY LOOKED AT (TB-33). `--limit` truncates the full
    listing in RECENCY order across the whole archive, so each agent lands at a wildly
    different fraction of its own history -- and an agent whose work is all older than the
    window disappears from the report with no note at all. The census is the denominator
    that makes the rendered rows comparable, and the roll-call that makes absence sayable.

    The census is computed EAGERLY, before the caller consumes a single ref: the caller
    breaks out of the ref loop early precisely when `--limit` is set, so a census gathered
    lazily during iteration would be missing exactly when it is needed most. A generator
    cannot both `return` a value and `yield`, which is why this is not one.
    """
    parent_ids, agents_seen = _probe_pass(
        runner, agent=agent, project=project, since=since, limit=limit
    )
    try:
        census = _agent_census(
            runner, agents_seen, agent=agent, project=project, since=since
        )
    except (RuntimeError, ValueError) as exc:
        # A census we cannot take is disclosed as UNKNOWN, never dropped -- a quietly
        # missing column is the sin this ticket exists to close. Discovery itself is
        # unaffected: the refs are already ours. (json.JSONDecodeError subclasses
        # ValueError, so a garbled payload lands here too.)
        census = AgentCensus(totals={}, archive_total=0, unavailable_reason=str(exc))
    refs = _yield_refs(
        runner, parent_ids, agent=agent, project=project, since=since, limit=limit
    )
    return census, refs


def iter_agentsview_sessions(
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    runner: Runner = _run_agentsview,
) -> Iterator[SessionRef]:
    """Refs only, no census (S8). Retained for callers that render no denominators, so
    they do not pay for the scoped `total` calls they would never use.

    Written with `yield from` rather than `return _yield_refs(...)` so the function body
    -- including the `_probe_pass` call -- does not run until the caller starts
    iterating. `iter_sessions(index_source="agentsview", ...)` depends on that laziness:
    it hands back this iterator unconsumed, and a caller that never iterates it (or that
    wraps the call in `assertRaises` around the `list(...)`, not around the call itself)
    must not eagerly hit the runner.
    """
    parent_ids, _agents_seen = _probe_pass(
        runner, agent=agent, project=project, since=since, limit=limit
    )
    yield from _yield_refs(
        runner, parent_ids, agent=agent, project=project, since=since, limit=limit
    )


def open_session_jsonl(
    ref: SessionRef,
    runner: Runner = _run_agentsview,
) -> Iterator[str]:
    """Stream JSONL lines for `ref` from disk or via `agentsview session export` (S9).

    Retained as the documented entry point; the branch it used to own is now
    `RawFileLoader` / `AgentsViewLoader`.
    """
    loader: SessionLoader = RawFileLoader() if ref.path is not None else AgentsViewLoader(runner)
    yield from loader.lines(ref)


def path_looks_binary(path: str) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(SNIFF_LEN)


class SessionLoader(ABC):
    """Acquisition. Knows nothing about schemas.

    The NUL sniff lives here, and therefore runs before schema detection -- a
    SQLite dump has no first JSON line to detect (TB-11).
    """

    @abstractmethod
    def lines(self, ref: SessionRef) -> Iterator[str]: ...


class RawFileLoader(SessionLoader):
    """A session already on disk."""

    def lines(self, ref: SessionRef) -> Iterator[str]:
        assert ref.path is not None, "RawFileLoader requires ref.path"
        # A raw transcript that is no longer on disk is a vanished source, not a
        # generic read error: raise the same typed MissingSourceExport the
        # AgentsView path raises so a frozen ref that has aged out of the
        # retention window is bucketed as `missing_source` on replay (TB-22).
        if not Path(ref.path).exists():
            raise MissingSourceExport(f"source file not found: {ref.path}")
        # Sniff on a separate binary handle so the text handle can still stream
        # line-by-line; slurping a head as text would force us to stitch a
        # mid-line cut back together.
        if path_looks_binary(ref.path):
            raise NonTranscriptExport(f"non-transcript payload (binary content): {ref.path}")
        with open(ref.path, encoding="utf-8", errors="replace") as f:
            yield from f


class AgentsViewLoader(SessionLoader):
    """A session fetched through `agentsview session export`."""

    def __init__(self, runner: Runner = _run_agentsview) -> None:
        self._runner = runner

    def lines(self, ref: SessionRef) -> Iterator[str]:
        result = self._runner(["agentsview", "session", "export", ref.session_id])
        if result.returncode != 0:
            detail = f"agentsview session export failed ({result.returncode}): {result.stderr.strip()}"
            if _MISSING_SOURCE_MARKER in result.stderr:
                raise MissingSourceExport(detail)
            raise RuntimeError(detail)
        if _looks_binary(result.stdout[:SNIFF_LEN]):
            # No session id here: callers that record this already prefix it.
            raise NonTranscriptExport(
                "non-transcript payload (binary content) from session export"
            )
        yield from result.stdout.splitlines(keepends=True)


def _probe_agentsview(runner: Runner) -> str | None:
    """Return a fallback reason if AgentsView is unavailable, else None (S10)."""
    try:
        result = runner(["agentsview", "session", "list", "--json", "--limit", "1"])
    except FileNotFoundError as exc:
        return f"agentsview binary not found: {exc}"
    if result.returncode != 0:
        return f"agentsview exited {result.returncode}: {result.stderr.strip()}"
    return None


def _raw_session_refs(
    root: str,
    project: str | None,
    since: str | None,
) -> Iterator[SessionRef]:
    base = Path(root).expanduser()
    for path in iter_session_files(root=root, project=project, since=since):
        owning_project, is_subagent = _project_and_subagent(base, path)
        yield SessionRef(
            agent="claude-code",
            source="raw",
            project=owning_project,
            session_id=path.stem,
            path=str(path),
            is_subagent=is_subagent,
        )


# The only agent the raw filesystem path can discover; `_raw_session_refs` stamps it.
RAW_AGENT = "claude-code"


def _raw_census(root: str, project: str | None, since: str | None) -> AgentCensus:
    """Denominator for the raw path: a filesystem count, no subprocess (TB-33).

    A missing root is an UNAVAILABLE census, not an exception: `iter_sessions` is called
    eagerly, and the `auto` path reaches here precisely when AgentsView is down and the
    raw root may not exist either. `_discover_refs` still surfaces the FileNotFoundError
    from the ref iterator as a MISSING_SOURCE skip -- this must not pre-empt it.
    """
    try:
        count = sum(1 for _ in iter_session_files(root=root, project=project, since=since))
    except FileNotFoundError as exc:
        return AgentCensus(totals={}, archive_total=0, unavailable_reason=str(exc))
    return AgentCensus(totals={RAW_AGENT: count}, archive_total=count)


def iter_sessions(
    index_source: IndexSource = "auto",
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    root: str = "~/.claude/projects",
    runner: Runner | None = None,
) -> tuple[Iterator[SessionRef], str | None, AgentCensus]:
    """Resolve the `--index-source` policy; return (refs, fallback_reason, census) (S10).

    The census rides along rather than being fetched separately so it cannot drift from
    the refs: same source, same filters, same call (TB-33).
    """
    run = runner if runner is not None else _run_agentsview
    if index_source == "raw":
        return (
            _raw_session_refs(root, project, since),
            None,
            _raw_census(root, project, since),
        )
    if index_source == "agentsview":
        census, refs = discover_agentsview(
            run, agent=agent, project=project, since=since, limit=limit
        )
        return refs, None, census
    if index_source == "auto":
        reason = _probe_agentsview(run)
        if reason is None:
            census, refs = discover_agentsview(
                run, agent=agent, project=project, since=since, limit=limit
            )
            return refs, None, census
        return (
            _raw_session_refs(root, project, since),
            reason,
            _raw_census(root, project, since),
        )
    raise ValueError(f"unknown index_source: {index_source!r}")
