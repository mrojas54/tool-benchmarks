"""Standalone per-run cache-token metrics — steps 1-4 of the lattice token benchmark.

A *benchmark reader*, deliberately separate from the passive analyzer's
`ParseResult`/Summary path: it reads raw Claude Code transcript JSONL directly, sums
per-message `usage`, aggregates a run's session set, and normalizes per ticket. This
is the "run now, before TB-26 lands" path (TB-26 wires cache sums into the production
analyzer; TB-27's `--run-manifest` will automate the run-grain grouping this does by
hand). Kept dependency-free so it runs anywhere a transcript does.

`read`/`creation` follow S39 NULL-vs-measured semantics: `None` when a session carried
no `usage` at all (unmeasured, SQL NULL), an int — including `0` — once at least one
message did. Read and creation are reported together on purpose: a prefix-sharing
change (per-ticket context extracts vs a shared contract) trades one for the other, so
a cache-read delta read alone misleads.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionUsage:
    """Per-session token sums. `read`/`creation` are `None` iff `usage_messages == 0`."""

    read: int | None
    creation: int | None
    input: int
    output: int
    usage_messages: int

    @property
    def measured(self) -> bool:
        return self.usage_messages > 0

    @property
    def total_billed(self) -> int:
        """read + creation + input + output, counting unmeasured cache as 0."""
        return (self.read or 0) + (self.creation or 0) + self.input + self.output


@dataclass(frozen=True)
class RunUsage:
    """A run's sums over its session set. `read`/`creation` here are never `None` —
    unmeasured sessions contribute 0 and are counted in `unmeasured_sessions`."""

    read: int
    creation: int
    input: int
    output: int
    sessions: int
    unmeasured_sessions: int

    @property
    def total_billed(self) -> int:
        return self.read + self.creation + self.input + self.output


def _as_int(value: object) -> int:
    """Coerce a usage field to int; anything non-numeric (or a bool) reads as 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def sum_session(lines: Iterable[str]) -> SessionUsage:
    """Sum `message.usage` over one transcript's JSONL lines.

    A line that is blank, non-JSON, or carries no `message.usage` is skipped rather
    than aborting the pass (mirrors the analyzer's S5 malformed-tolerance).
    """
    read = creation = inp = out = msgs = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        msgs += 1
        read += _as_int(usage.get("cache_read_input_tokens"))
        creation += _as_int(usage.get("cache_creation_input_tokens"))
        inp += _as_int(usage.get("input_tokens"))
        out += _as_int(usage.get("output_tokens"))
    if msgs == 0:
        return SessionUsage(read=None, creation=None, input=0, output=0, usage_messages=0)
    return SessionUsage(read=read, creation=creation, input=inp, output=out, usage_messages=msgs)


def sum_run(session_files: Iterable[Path]) -> RunUsage:
    """Fold a run's session set (step 4) — one file per constituent session."""
    read = creation = inp = out = sessions = unmeasured = 0
    for path in session_files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            session = sum_session(handle)
        sessions += 1
        if not session.measured:
            unmeasured += 1
        read += session.read or 0
        creation += session.creation or 0
        inp += session.input
        out += session.output
    return RunUsage(
        read=read,
        creation=creation,
        input=inp,
        output=out,
        sessions=sessions,
        unmeasured_sessions=unmeasured,
    )


def per_ticket(run: RunUsage, tickets: int) -> dict[str, float]:
    """Normalize a run's sums by ticket count so runs of different size compare."""
    if tickets <= 0:
        raise ValueError("tickets must be > 0 to normalize per ticket")
    return {
        "cache_read": run.read / tickets,
        "cache_creation": run.creation / tickets,
        "input": run.input / tickets,
        "output": run.output / tickets,
        "total_billed": run.total_billed / tickets,
    }


def _read_manifest(path: Path) -> list[Path]:
    """One transcript path per line (the run's session set); blank/`#` lines ignored."""
    files: list[Path] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            files.append(Path(stripped))
    return files


def _render(run: RunUsage, norm: dict[str, float] | None) -> str:
    lines = [
        f"sessions={run.sessions} (unmeasured={run.unmeasured_sessions})",
        f"cache_read={run.read:,}  cache_creation={run.creation:,}  "
        f"input={run.input:,}  output={run.output:,}",
        f"TOTAL_BILLED={run.total_billed:,}",
    ]
    if norm is not None:
        lines.append(
            "per-ticket: "
            f"read={norm['cache_read']:.1f}  creation={norm['cache_creation']:.1f}  "
            f"total={norm['total_billed']:.1f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="toolbench.cache_tokens",
        description="Per-run cache-token metrics from raw Claude transcripts (S39).",
    )
    parser.add_argument(
        "files", nargs="*", type=Path, help="transcript JSONL paths (a run's sessions)"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="file listing transcript paths, one per line (overrides positional FILES)",
    )
    parser.add_argument(
        "--tickets", type=int, default=0, help="ticket count for per-ticket normalization"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    args = parser.parse_args(argv)

    files = _read_manifest(args.manifest) if args.manifest else list(args.files)
    if not files:
        parser.error("no transcript files given (positional FILES or --manifest)")

    run = sum_run(files)
    norm = per_ticket(run, args.tickets) if args.tickets > 0 else None

    if args.as_json:
        payload: dict[str, object] = {
            "sessions": run.sessions,
            "unmeasured_sessions": run.unmeasured_sessions,
            "cache_read": run.read,
            "cache_creation": run.creation,
            "input": run.input,
            "output": run.output,
            "total_billed": run.total_billed,
            "per_ticket": norm,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(_render(run, norm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
