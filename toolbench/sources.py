"""Multi-agent session discovery (S7-S10). Stdlib only — shells to AgentsView."""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

IndexSource = Literal["auto", "agentsview", "raw"]
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

# Bytes sniffed to classify a payload as text or binary. A NUL cannot appear in a
# JSONL transcript, so it is a sound discriminator rather than a heuristic.
SNIFF_LEN = 8192

# `agentsview session export` writes this to stderr when it lists a session whose
# transcript is no longer on disk. Matched to raise a typed MissingSourceExport so
# the reason is decided where the evidence lives, not by regex on the report (TB-23).
_MISSING_SOURCE_MARKER = "source file not found"


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
    # Set at discovery (CQ 3.2). Raw scans stamp it from the path layout;
    # AgentsView refs default False (the index does not expose the nesting).
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


def iter_agentsview_sessions(
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    runner: Runner = _run_agentsview,
) -> Iterator[SessionRef]:
    """Page `agentsview session list --json` with cursor pagination (S8)."""
    cursor: str | None = None
    while True:
        argv = ["agentsview", "session", "list", "--json", "--limit", str(limit)]
        if agent != "all":
            argv += ["--agent", agent]
        if project is not None:
            argv += ["--project", project]
        if since is not None:
            argv += ["--date-from", since]
        if cursor:
            argv += ["--cursor", cursor]
        result = runner(argv)
        if result.returncode != 0:
            raise RuntimeError(
                f"agentsview session list failed ({result.returncode}): {result.stderr.strip()}"
            )
        payload = json.loads(result.stdout)
        for entry in payload.get("sessions", []):
            yield SessionRef(
                agent=entry["agent"],
                source="agentsview",
                project=entry["project"],
                session_id=entry["id"],
                path=None,
            )
        cursor = payload.get("next_cursor") or None
        if not cursor:
            break


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


def iter_sessions(
    index_source: IndexSource = "auto",
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    root: str = "~/.claude/projects",
    runner: Runner | None = None,
) -> tuple[Iterator[SessionRef], str | None]:
    """Resolve the `--index-source` policy; return (refs, fallback_reason) (S10)."""
    run = runner if runner is not None else _run_agentsview
    if index_source == "raw":
        return _raw_session_refs(root, project, since), None
    if index_source == "agentsview":
        refs = iter_agentsview_sessions(
            agent=agent, project=project, since=since, limit=limit, runner=run
        )
        return refs, None
    if index_source == "auto":
        reason = _probe_agentsview(run)
        if reason is None:
            refs = iter_agentsview_sessions(
                agent=agent, project=project, since=since, limit=limit, runner=run
            )
            return refs, None
        return _raw_session_refs(root, project, since), reason
    raise ValueError(f"unknown index_source: {index_source!r}")
