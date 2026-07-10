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


def _ref_to_dict(ref: SessionRef) -> dict[str, str | None]:
    return {
        "agent": ref.agent,
        "source": ref.source,
        "project": ref.project,
        "session_id": ref.session_id,
        "path": ref.path,
    }


def _ref_from_dict(d: dict[str, str | None]) -> SessionRef:
    return SessionRef(
        agent=str(d["agent"]),
        source=str(d["source"]),
        project=str(d["project"]),
        session_id=str(d["session_id"]),
        path=d["path"] if d["path"] is None else str(d["path"]),
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
