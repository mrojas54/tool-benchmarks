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

from toolbench.sources import SessionRef

MANIFEST_VERSION = "toolbench-freeze-1"


@dataclass
class CorpusManifest:
    """A frozen corpus: the discovered refs plus the fingerprint they hashed to."""

    version: str
    fingerprint: str
    count: int
    refs: list[SessionRef]


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


def write_manifest(path: str, refs: list[SessionRef], fingerprint: str) -> None:
    """Freeze `refs` to `path` (write-once). Sorted keys keep the file stable."""
    payload = {
        "version": MANIFEST_VERSION,
        "fingerprint": fingerprint,
        "count": len(refs),
        "refs": [_ref_to_dict(r) for r in refs],
    }
    Path(path).expanduser().write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_manifest(path: str) -> CorpusManifest:
    """Load a frozen manifest for replay."""
    data = json.loads(Path(path).expanduser().read_text())
    return CorpusManifest(
        version=str(data["version"]),
        fingerprint=str(data["fingerprint"]),
        count=int(data["count"]),
        refs=[_ref_from_dict(r) for r in data["refs"]],
    )
