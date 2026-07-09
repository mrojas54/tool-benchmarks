"""Parser & records (S1, S2, S3, S4, S5, S6)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def result_len(payload: object) -> int:
    """Normalize a tool-result payload to a character length.

    Handles four shapes: a plain string, a JSON-able dict, an MCP
    block-list (``list[dict]`` with ``text`` entries), and a block-local
    ``content`` payload (``{"content": [...]}``) wrapping the same blocks.
    """
    if isinstance(payload, str):
        return len(payload)
    if isinstance(payload, list):
        return _block_list_len(payload)
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, list):
            return _block_list_len(content)
        return len(json.dumps(payload))
    return len(str(payload))


def _block_list_len(blocks: list[object]) -> int:
    total = 0
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            total += len(block["text"])
        elif isinstance(block, str):
            total += len(block)
        else:
            total += len(json.dumps(block))
    return total


@dataclass
class ToolCall:
    """A single tool invocation joined from an agent transcript (S4)."""

    agent: str
    source: str
    project: str
    name: str
    input_chars: int
    output_chars: int
    session_id: str
    ts: str
    usage: dict[str, object] | None
    duration_ms: float | None
    error: str | None
    model: str | None
    no_result: bool = False
    result_source: str | None = None

    @property
    def tokens(self) -> int:
        return self.output_chars // 4

    @property
    def input_tokens(self) -> int:
        return self.input_chars // 4


@dataclass
class ParseResult:
    """Output of `parse_session` (S5): joined calls plus a malformed-line count."""

    calls: list[ToolCall]
    malformed: int


def parse_session(
    path: str | os.PathLike[str],
    *,
    agent: str = "claude-code",
    source: str = "raw",
    project: str | None = None,
) -> ParseResult:
    """Deprecated: parse a Claude Code session JSONL by path.

    Retained because it is the documented entry point and `probe.py` imports it.
    A `TranscriptParser` consumes an `Iterable[str]` and so cannot derive
    `project` from a path; this shim resolves it before delegating, preserving
    the historical `project=None -> path.parent.name` default.

    errors="replace": a stray non-UTF-8 byte degrades to U+FFFD rather than
    aborting the session (S5, TB-10).

    Prefer `registry.pick_adapter(ref).parse(ref)`, which detects the schema
    instead of assuming Claude's.
    """
    from toolbench.parsers import ClaudeParser  # local: avoids an import cycle

    session_path = Path(path)
    resolved_project = project if project is not None else session_path.parent.name
    with session_path.open(encoding="utf-8", errors="replace") as handle:
        return ClaudeParser().parse(
            handle, agent=agent, source=source, project=resolved_project
        )
