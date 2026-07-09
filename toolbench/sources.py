"""Multi-agent session discovery (S7-S10). Stdlib only — shells to AgentsView."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

IndexSource = Literal["auto", "agentsview", "raw"]
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

# Bytes sniffed to classify a payload as text or binary. A NUL cannot appear in a
# JSONL transcript, so it is a sound discriminator rather than a heuristic.
SNIFF_LEN = 8192


class NonTranscriptExport(RuntimeError):
    """A session payload is not a JSONL transcript (e.g. `agentsview session export`
    returns a whole SQLite database for hermes cron sessions, with returncode 0).

    Subclasses RuntimeError so `passive.main` demotes the session to `skipped_roots`
    via its existing per-session guard, instead of absorbing megabytes of binary as
    'malformed lines'.
    """


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
            # Match the owning project dir, not `path.parent.name`: subagent
            # transcripts live at <project>/subagents/*.jsonl (S13).
            rel = path.relative_to(base)
            if len(rel.parts) < 2 or project not in rel.parts[0]:
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
    """Stream JSONL lines for `ref` from disk or via `agentsview session export` (S9)."""
    if ref.path is not None:
        # Sniff on a separate binary handle so the text handle can still stream
        # line-by-line; slurping a head as text would force us to stitch a
        # mid-line cut back together.
        if path_looks_binary(ref.path):
            raise NonTranscriptExport(f"non-transcript payload (binary content): {ref.path}")
        with open(ref.path, encoding="utf-8", errors="replace") as f:
            yield from f
        return
    result = runner(["agentsview", "session", "export", ref.session_id])
    if result.returncode != 0:
        raise RuntimeError(
            f"agentsview session export failed ({result.returncode}): {result.stderr.strip()}"
        )
    if _looks_binary(result.stdout[:SNIFF_LEN]):
        # No session id here: callers that record this already prefix it.
        raise NonTranscriptExport("non-transcript payload (binary content) from session export")
    yield from result.stdout.splitlines(keepends=True)


def path_looks_binary(path: str) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(SNIFF_LEN)


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
    for path in iter_session_files(root=root, project=project, since=since):
        yield SessionRef(
            agent="claude-code",
            source="raw",
            project=path.parent.name,
            session_id=path.stem,
            path=str(path),
        )


def iter_sessions(
    index_source: IndexSource = "auto",
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    root: str = "~/.claude/projects",
    runner: Runner = _run_agentsview,
) -> tuple[Iterator[SessionRef], str | None]:
    """Resolve the `--index-source` policy; return (refs, fallback_reason) (S10)."""
    if index_source == "raw":
        return _raw_session_refs(root, project, since), None
    if index_source == "agentsview":
        refs = iter_agentsview_sessions(agent=agent, project=project, since=since, limit=limit, runner=runner)
        return refs, None
    if index_source == "auto":
        reason = _probe_agentsview(runner)
        if reason is None:
            refs = iter_agentsview_sessions(
                agent=agent, project=project, since=since, limit=limit, runner=runner
            )
            return refs, None
        return _raw_session_refs(root, project, since), reason
    raise ValueError(f"unknown index_source: {index_source!r}")
