"""Corpus freeze manifest (TB-22, S37).

Pins the discovered ref set so a later run can replay identical inputs instead of
re-discovering a corpus whose tail is being deleted underneath it (claude-mem
observer transcripts age out of a ~30-day sliding window mid-scan). The manifest
is written once, on the first `--freeze <path>` run, and replayed on every run
thereafter; refs that have since vanished are named in the report.

Stdlib only, and deliberately free of any dependency on `passive` -- it stores
the fingerprint it is handed as an opaque string, so `passive` owns fingerprint
computation and no import cycle forms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from toolbench.sources import AgentCensus, SessionRef

# v1 -> v2 (TB-37): v2 adds an optional `census` block persisting the AgentCensus
# measured at freeze time, so replay can disclose real sampling fractions instead of
# the blanket "no denominator" TB-33/TB-22 shipped with (that gap was intentional --
# persisting a census was a manifest FORMAT change out of TB-22's scope). The bump is
# purely additive: a v1 manifest has no `census` key, and `read_manifest` treats that
# the same as a v2 manifest deliberately written without one -- key ABSENCE, not the
# version string, is what `read_manifest` branches on (see its docstring).
MANIFEST_VERSION = "toolbench-freeze-2"


class MalformedFreezeManifest(RuntimeError):
    """The freeze manifest is unreadable or cannot define a frozen corpus."""


@dataclass
class CorpusManifest:
    """A frozen corpus: the discovered refs plus the fingerprint they hashed to.

    `census` is the archive-size census taken AT FREEZE TIME (TB-37). It is `None`
    for a v1 manifest, or a v2 manifest whose freeze run had no census to persist
    (e.g. discovery itself failed). When present, it is a HISTORICAL snapshot -- the
    live archive has moved on since -- and callers disclosing it must say so; see
    `passive.py`'s replay branch, which is the one place that renders it.

    `census_includes_subagents` records the population filter used to measure that
    denominator. Older v2 manifests lack it; replay must treat their census as
    unavailable rather than risk pairing it with a differently filtered numerator.
    """

    version: str
    fingerprint: str
    count: int
    refs: list[SessionRef]
    census: AgentCensus | None
    census_includes_subagents: bool | None


def _ref_to_dict(ref: SessionRef) -> dict[str, str | bool | None]:
    return {
        "agent": ref.agent,
        "source": ref.source,
        "project": ref.project,
        "session_id": ref.session_id,
        "path": ref.path,
        "is_subagent": ref.is_subagent,
    }


def _is_subagent_from_manifest(d: dict[str, str | bool | None], path: str | None) -> bool:
    """True if EITHER the flag says so or the path proves it (S13).

    TB-29: manifests frozen before the discovery fix persist an explicit
    `"is_subagent": false` for real subagent sessions -- written by the very code that
    could never set it True. Letting an explicit flag win would carry the no-op into
    replay forever, so a stale `false` would keep `--exclude-subagents` lying on every
    frozen corpus. The path is ground truth: a session under `/subagents/` IS one,
    whatever a stale flag claims. OR-ing the two also self-heals legacy manifests that
    omit the key entirely.
    """
    flag = d.get("is_subagent")
    if isinstance(flag, bool) and flag:
        return True
    return path is not None and "/subagents/" in path


def _ref_from_dict(d: dict[str, str | bool | None]) -> SessionRef:
    raw_path = d.get("path")
    path = str(raw_path) if raw_path is not None else None
    return SessionRef(
        agent=str(d["agent"]),
        source=str(d["source"]),
        project=str(d["project"]),
        session_id=str(d["session_id"]),
        path=path,
        is_subagent=_is_subagent_from_manifest(d, path),
    )


def _census_to_dict(c: AgentCensus) -> dict[str, object]:
    """`residual` is not stored -- it's a property derived from `totals` and
    `archive_total`, so it reconstructs correctly on read without being persisted
    twice (and could never drift from its inputs if it were)."""
    return {
        "totals": dict(c.totals),
        "archive_total": c.archive_total,
        "unavailable_reason": c.unavailable_reason,
    }


def _census_from_dict(d: dict[str, object]) -> AgentCensus:
    raw_totals = d.get("totals")
    totals: dict[str, int] = {}
    if isinstance(raw_totals, dict):
        for k, v in raw_totals.items():
            totals[str(k)] = int(v)
    raw_archive_total = d.get("archive_total")
    archive_total = int(raw_archive_total) if isinstance(raw_archive_total, (int, str)) else 0
    reason = d.get("unavailable_reason")
    return AgentCensus(
        totals=totals,
        archive_total=archive_total,
        unavailable_reason=str(reason) if isinstance(reason, str) else None,
    )


def write_manifest(
    path: str,
    refs: list[SessionRef],
    fingerprint: str,
    census: AgentCensus | None = None,
    census_includes_subagents: bool | None = None,
) -> None:
    """Freeze `refs` to `path` (write-once). Sorted keys keep the file stable.

    `census` (TB-37) is the archive-size census measured at freeze time -- optional
    so existing callers that have none (or none worth keeping) still write a valid
    manifest; `read_manifest` treats an absent `census` key exactly like a v1
    manifest, whichever version string is on it. Its population filter is persisted
    separately when known so replay can verify that the denominator still describes
    the selected refs.
    """
    payload: dict[str, object] = {
        "version": MANIFEST_VERSION,
        "fingerprint": fingerprint,
        "count": len(refs),
        "refs": [_ref_to_dict(r) for r in refs],
    }
    if census is not None:
        payload["census"] = _census_to_dict(census)
        if census_includes_subagents is not None:
            payload["census_includes_subagents"] = census_includes_subagents
    Path(path).expanduser().write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_manifest(path: str) -> CorpusManifest:
    """Load a frozen manifest for replay.

    `census` is `None` whenever the `census` key is absent -- a v1 manifest (no such
    key ever existed) and a v2 manifest written without one both degrade identically,
    by construction (TB-37): the caller checks key PRESENCE, not the version string,
    so a hand-edited or future manifest that simply omits the block is never
    mistaken for a crash. Same tolerant-read shape as `_is_subagent_from_manifest`
    above -- absence is handled, never rejected.
    """
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedFreezeManifest(f"{path} could not be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise MalformedFreezeManifest(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedFreezeManifest(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MalformedFreezeManifest(f"{path} must contain a JSON object")
    raw_refs = data.get("refs")
    if not isinstance(raw_refs, list):
        raise MalformedFreezeManifest(f"{path} field `refs` must be a list")
    try:
        refs = [_ref_from_dict(r) for r in raw_refs]
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedFreezeManifest(f"{path} has an invalid ref entry: {exc}") from exc
    raw_census = data.get("census")
    census = _census_from_dict(raw_census) if isinstance(raw_census, dict) else None
    raw_census_includes_subagents = data.get("census_includes_subagents")
    census_includes_subagents = (
        raw_census_includes_subagents
        if isinstance(raw_census_includes_subagents, bool)
        else None
    )
    try:
        return CorpusManifest(
            version=str(data["version"]),
            fingerprint=str(data["fingerprint"]),
            count=int(data["count"]),
            refs=refs,
            census=census,
            census_includes_subagents=census_includes_subagents,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedFreezeManifest(f"{path} is missing a required field: {exc}") from exc
