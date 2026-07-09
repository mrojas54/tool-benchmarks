"""Schema parsers (TB-13). One class per transcript schema. Stdlib only.

A parser interprets already-acquired lines. It never opens a file, never shells
out, and never decides which schema it is looking at -- `adapters.detect_parser`
does that by asking each parser's `claims_line`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from toolbench.transcript import ParseResult, ToolCall, result_len


@dataclass
class _PendingCall:
    """A `tool_use` block awaiting its matching result."""

    name: str
    input_chars: int
    session_id: str
    ts: str
    usage: dict[str, object] | None
    model: str | None


class TranscriptParser(ABC):
    """Interpretation. Knows nothing about acquisition."""

    schema_tag: ClassVar[str]

    @classmethod
    @abstractmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        """True if `entry` is discriminating evidence for this schema."""

    @abstractmethod
    def parse(
        self, lines: Iterable[str], *, agent: str, source: str, project: str
    ) -> ParseResult: ...


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


class ClaudeParser(TranscriptParser):
    """Claude Code / cowork JSONL: `tool_use` blocks joined to `tool_result` by id.

    `cowork` emits this schema too. That is why detection is by payload, not by
    producer: one parser, two agents, zero registry entries for either name.
    """

    schema_tag: ClassVar[str] = "claude"

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        # Every claude/cowork control and message record carries `sessionId`.
        # `tool_use` itself is NOT a usable discriminator: line 0 is a
        # `last-prompt` / `mode` record, and a session that used no tools has
        # no `tool_use` block anywhere.
        return "sessionId" in entry

    def parse(
        self, lines: Iterable[str], *, agent: str, source: str, project: str
    ) -> ParseResult:
        """Join tool_use blocks to their results, streaming.

        Malformed/partial JSON lines are counted and skipped, never fatal (S5). A
        `tool_use` with no matching result by end-of-input is kept with
        `output_chars=0, no_result=True` rather than dropped (S6). `duration_ms`
        is always `None`: raw Claude Code JSONL carries no per-tool-call duration
        field to derive it from.
        """
        pending: dict[str, _PendingCall] = {}
        calls: list[ToolCall] = []
        malformed = 0

        for raw_line in lines:
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
                    model = message.get("model") if isinstance(message, dict) else None
                    pending[tool_use_id] = _PendingCall(
                        name=name,
                        input_chars=result_len(tool_use_block.get("input")),
                        session_id=session_id_str,
                        ts=ts_str,
                        usage=usage if isinstance(usage, dict) else None,
                        model=model if isinstance(model, str) else None,
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
                        project=project,
                        name=pending_call.name,
                        input_chars=pending_call.input_chars,
                        output_chars=result_len(payload),
                        session_id=pending_call.session_id,
                        ts=pending_call.ts,
                        usage=pending_call.usage,
                        duration_ms=None,
                        error=error,
                        model=pending_call.model,
                        result_source=payload_source,
                    )
                )

        # S6: an unmatched `tool_use` at EOF is kept, never dropped.
        for pending_call in pending.values():
            calls.append(
                ToolCall(
                    agent=agent,
                    source=source,
                    project=project,
                    name=pending_call.name,
                    input_chars=pending_call.input_chars,
                    output_chars=0,
                    session_id=pending_call.session_id,
                    ts=pending_call.ts,
                    usage=pending_call.usage,
                    duration_ms=None,
                    error=None,
                    model=pending_call.model,
                    no_result=True,
                    result_source=None,
                )
            )

        return ParseResult(calls=calls, malformed=malformed)
