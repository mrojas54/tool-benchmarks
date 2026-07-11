"""Parser & records (S1, S2, S3, S4, S5, S6)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
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


class UsageProvenance(Enum):
    """Why a `ToolCall` does or does not carry a usage record (S29).

    `usage=None` previously meant three different things at once. Each arm below
    is one of them, made explicit. Only a producer may assert ABSENT_BY_SCHEMA or
    ABSENT_BY_EXPORT; a parser that merely fails to find usage on a schema that
    promises it records ABSENT_UNEXPECTED and says nothing about the cause.
    """

    PRESENT = "present"
    ABSENT_BY_SCHEMA = "absent_by_schema"  # producer has no per-call usage (hermes SQLite)
    ABSENT_BY_EXPORT = "absent_by_export"  # producer had usage; the export dropped it (trace)
    ABSENT_UNEXPECTED = "absent_unexpected"  # claude schema, claude producer, no usage: anomaly


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
    usage_provenance: UsageProvenance
    duration_ms: float | None
    error: str | None
    model: str | None
    no_result: bool = False
    result_source: str | None = None
    # Parse-time inefficiency tags (CQ 3.1). Stamped by the emit path from
    # schema/agent policy; the reducer counts these facts and never re-derives
    # them from tool names.
    is_deferral: bool = False
    is_subagent_fanout: bool = False

    @property
    def tokens(self) -> int:
        return self.output_chars // 4

    @property
    def input_tokens(self) -> int:
        return self.input_chars // 4


@dataclass
class ParseResult:
    """Output of `parse_session` (S5): joined calls plus a malformed-line count.

    `session_cache_read_tokens` (S32) is session-grain, not per-call: only
    `parse_hermes_session` ever populates it, from the hermes `sessions` row's
    own `cache_read_tokens` column. `None` means "not measured" (SQL NULL);
    an int -- including `0` -- means the session was measured. It is never
    attributed to an individual `ToolCall` or folded into `UsageProvenance`,
    which answers a different, per-call question this field cannot answer.

    `unjoinable` (S38, TB-24) is a third bucket alongside joined `calls` and
    `malformed` lines: tool records a parser RECOGNIZED as real calls but
    structurally CANNOT join -- no join key and no output record -- keyed by the
    record kind. Emitting them as calls would fabricate a join or leave permanent
    `no_result` orphans; dropping them would silently understate the producer's
    tool usage. Counting them by kind lets the Summary name the gap instead
    (codex `web_search_call`: no `call_id`, no paired output). Empty for every
    parser with nothing to report.
    """

    calls: list[ToolCall]
    malformed: int
    session_cache_read_tokens: int | None = None
    unjoinable: dict[str, int] = field(default_factory=dict)


def parse_session(
    path: str | os.PathLike[str],
    *,
    agent: str = "claude-code",
    source: str = "raw",
    project: str | None = None,
) -> ParseResult:
    """Deprecated: parse a Claude Code session JSONL by path.

    Retained as a documented / test entry point. Live callers should prefer
    `registry.pick_adapter(ref).parse(ref)`, which detects the schema instead of
    assuming Claude's. `probe.py` no longer uses this shim (single-pass scan).

    A `TranscriptParser` consumes an `Iterable[str]` and so cannot derive
    `project` from a path; this shim resolves it before delegating, preserving
    the historical `project=None -> path.parent.name` default.

    errors="replace": a stray non-UTF-8 byte degrades to U+FFFD rather than
    aborting the session (S5, TB-10).
    """
    from toolbench.parsers import ClaudeParser  # local: avoids an import cycle

    session_path = Path(path)
    resolved_project = project if project is not None else session_path.parent.name
    with session_path.open(encoding="utf-8", errors="replace") as handle:
        return ClaudeParser().parse(
            handle, agent=agent, source=source, project=resolved_project
        )
