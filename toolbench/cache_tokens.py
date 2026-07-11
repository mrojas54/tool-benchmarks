"""Per-run cache-token metrics — façade over ClaudeParser (S39 / CQ 1.2).

Once a temporary second JSONL interpreter; now delegates session summing to
`ClaudeParser` so the production parse path is the sole Claude schema reader.
This module keeps run aggregation (`sum_run`), per-ticket normalization, and the
CLI that the cache-token-metrics skill invokes until TB-27's `--run-manifest`
lands on the passive analyzer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from toolbench.parsers import ClaudeParser


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


def sum_session(lines: Iterable[str]) -> SessionUsage:
    """Sum session-grain usage via ClaudeParser — no second JSONL interpreter."""
    result = ClaudeParser().parse(
        lines, agent="claude-code", source="raw", project="cache-tokens"
    )
    return SessionUsage(
        read=result.session_cache_read_tokens,
        creation=result.session_cache_creation_tokens,
        input=result.session_input_tokens,
        output=result.session_output_tokens,
        usage_messages=result.session_usage_messages,
    )


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
