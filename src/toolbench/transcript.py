"""Parser & records (S1, S2, S3, S4, S5, S6)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum


class JsonLines:
    """JSONL records: blanks skipped, undecodable and non-object lines counted.

    Every parser opened with the same six lines -- strip, skip blank, `json.loads`,
    catch `JSONDecodeError`, then an `isinstance(entry, dict)` guard -- and each
    copy had to re-derive that a line of *valid* JSON which is not an object
    (`123`, `[1, 2]`, `"text"`) is malformed too, not something to hand downstream
    where `entry.get(...)` would raise. Counting is the parser's job (S5) and the
    count is never fatal, so `malformed` is read once after iteration and handed
    to `ParseResult.malformed`.

    Callers that must not count -- a schema sniff that would charge a session
    twice -- simply ignore the attribute.

    Single-pass: the underlying iterable is consumed as it is walked.
    """

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = lines
        self.malformed = 0

    def __iter__(self) -> Iterator[dict[str, object]]:
        for raw_line in self._lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                self.malformed += 1
                continue
            if not isinstance(entry, dict):
                self.malformed += 1
                continue
            yield entry


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
    # Optional parse enrichments (CQ 7.1). Default None/absent so passive
    # reduction ignores them; probe opts in via ClaudeParser flags.
    raw_input: str | None = None
    turn_key: str | None = None

    @property
    def tokens(self) -> int:
        return self.output_chars // 4

    @property
    def input_tokens(self) -> int:
        return self.input_chars // 4


@dataclass
class TurnStats:
    """Per-API-response emission counts for isolability (S26 / CQ 7.1)."""

    tool_uses: int = 0
    non_tool_output: bool = False


@dataclass
class BranchUsage:
    """Per-branch usage sums for one session (S40).

    Keyed in `ParseResult.usage_by_branch` by the *entry's* `gitBranch`. A session
    that straddles branches has one bucket per branch: attribution is per-entry,
    because a session is not owned by one run (29/158 sessions straddle).
    """

    read: int = 0
    creation: int = 0
    input: int = 0
    output: int = 0
    messages: int = 0


@dataclass
class ParseResult:
    """Output of a `TranscriptParser.parse` pass (S5): joined calls plus a malformed-line count.

    `session_cache_read_tokens` (S32) is session-grain, not per-call: hermes
    populates it from the `sessions` row; ClaudeParser (S39 / CQ 1.2) sums it
    from per-message `usage.cache_read_input_tokens`. `None` means "not
    measured"; an int -- including `0` -- means the session was measured. It is
    never attributed to an individual `ToolCall` or folded into `UsageProvenance`.

    `session_cache_creation_tokens` (S39) is the matching creation sum from
    `usage.cache_creation_input_tokens`. ClaudeParser stamps both together;
    hermes leaves creation as `None` (no session-grain creation column).

    `session_input_tokens` / `session_output_tokens` / `session_usage_messages`
    are the companion per-message totals ClaudeParser accumulates in the same
    pass so the cache-token benchmark CLI can façade over ParseResult without a
    second JSONL interpreter (CQ 1.2).

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
    session_cache_creation_tokens: int | None = None
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    session_usage_messages: int = 0
    # S40: per-entry usage bucketed by gitBranch. ADDITIVE beside the S39 session
    # totals -- the invariant `session total == sum of buckets` is an eval. Entries
    # with usage but no gitBranch bucket under "" so no billed token is dropped.
    usage_by_branch: dict[str, BranchUsage] = field(default_factory=dict)
    unjoinable: dict[str, int] = field(default_factory=dict)
    # Populated only when ClaudeParser(track_turns=True); empty otherwise.
    turns: dict[str, TurnStats] = field(default_factory=dict)
