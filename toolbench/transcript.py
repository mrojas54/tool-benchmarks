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


@dataclass
class _PendingCall:
    """A `tool_use` block awaiting its matching result."""

    name: str
    input_chars: int
    session_id: str
    ts: str
    usage: dict[str, object] | None


def _result_id(entry: dict[str, object], block: dict[str, object] | None) -> str | None:
    """Join key (S1): block-local `tool_use_id` first, else top-level `toolUseID`."""
    if block is not None:
        block_id = block.get("tool_use_id")
        if isinstance(block_id, str):
            return block_id
    top_level_id = entry.get("toolUseID")
    return top_level_id if isinstance(top_level_id, str) else None


def _result_payload(
    entry: dict[str, object], block: dict[str, object] | None
) -> tuple[object, str | None]:
    """Payload resolution (S2): block-local `content` wins over top-level `toolUseResult`."""
    if block is not None and "content" in block:
        return block["content"], "block_local"
    if "toolUseResult" in entry:
        return entry["toolUseResult"], "top_level"
    return None, None


def parse_session(
    path: str | os.PathLike[str],
    *,
    agent: str = "claude-code",
    source: str = "raw",
    project: str | None = None,
) -> ParseResult:
    """Stream one Claude Code session JSONL, joining tool_use blocks to results.

    Malformed/partial JSON lines are counted and skipped, never fatal (S5). A
    `tool_use` with no matching result by end-of-file is kept with
    `output_chars=0, no_result=True` rather than dropped (S6). `duration_ms`
    is always `None`: raw Claude Code JSONL carries no per-tool-call
    duration field to derive it from.
    """
    session_path = Path(path)
    resolved_project = project if project is not None else session_path.parent.name

    pending: dict[str, _PendingCall] = {}
    calls: list[ToolCall] = []
    malformed = 0

    with session_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(entry, dict):
                malformed += 1
                continue

            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            session_id = entry.get("sessionId")
            ts = entry.get("timestamp")
            session_id_str = session_id if isinstance(session_id, str) else ""
            ts_str = ts if isinstance(ts, str) else ""

            if isinstance(content, list):
                for tool_use_block in content:
                    if not isinstance(tool_use_block, dict):
                        continue
                    if tool_use_block.get("type") != "tool_use":
                        continue
                    tool_use_id = tool_use_block.get("id")
                    name = tool_use_block.get("name")
                    if not isinstance(tool_use_id, str) or not isinstance(name, str):
                        continue
                    usage = message.get("usage") if isinstance(message, dict) else None
                    pending[tool_use_id] = _PendingCall(
                        name=name,
                        input_chars=result_len(tool_use_block.get("input")),
                        session_id=session_id_str,
                        ts=ts_str,
                        usage=usage if isinstance(usage, dict) else None,
                    )

            result_blocks: list[dict[str, object] | None] = []
            if isinstance(content, list):
                result_blocks = [
                    block
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
            if not result_blocks and "toolUseID" in entry:
                result_blocks = [None]

            for result_block in result_blocks:
                result_id = _result_id(entry, result_block)
                if result_id is None or result_id not in pending:
                    continue
                payload, payload_source = _result_payload(entry, result_block)
                pending_call = pending.pop(result_id)
                error = None
                if isinstance(result_block, dict) and result_block.get("is_error"):
                    error = "tool_error"
                calls.append(
                    ToolCall(
                        agent=agent,
                        source=source,
                        project=resolved_project,
                        name=pending_call.name,
                        input_chars=pending_call.input_chars,
                        output_chars=result_len(payload),
                        session_id=pending_call.session_id,
                        ts=pending_call.ts,
                        usage=pending_call.usage,
                        duration_ms=None,
                        error=error,
                        result_source=payload_source,
                    )
                )

    for pending_call in pending.values():
        calls.append(
            ToolCall(
                agent=agent,
                source=source,
                project=resolved_project,
                name=pending_call.name,
                input_chars=pending_call.input_chars,
                output_chars=0,
                session_id=pending_call.session_id,
                ts=pending_call.ts,
                usage=pending_call.usage,
                duration_ms=None,
                error=None,
                no_result=True,
                result_source=None,
            )
        )

    return ParseResult(calls=calls, malformed=malformed)
