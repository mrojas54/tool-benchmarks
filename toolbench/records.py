"""Core records shared across the toolbench package (S3, S4)."""

from __future__ import annotations

import json
from dataclasses import dataclass


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

    @property
    def tokens(self) -> int:
        return self.output_chars // 4

    @property
    def input_tokens(self) -> int:
        return self.input_chars // 4
