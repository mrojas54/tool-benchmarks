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

from toolbench.transcript import ParseResult, ToolCall, UsageProvenance, result_len

# `hermes sessions export --format trace` stamps this on every record. It is a
# positive producer declaration, not an inference from a missing field: verified
# on every trace record, and absent as a top-level `version` from all 4,061 real
# claude transcripts in the local archive.
HERMES_TRACE_VERSION = "hermes-agent"

# Inefficiency-tag policy (CQ 3.1). Lived on `Reducer` as name frozensets; next
# agent meant another `if`. Stamp at emit so absorb only counts tagged facts.
# `ToolSearch` is also the synthetic name CodexParser assigns to nameless
# `tool_search_call` records (S33). `spawn_agent` is codex's fan-out primitive;
# `wait_agent` awaits an already-spawned subagent and is not itself a fan-out.
DEFERRAL_TOOL_NAMES = frozenset({"ToolSearch"})
SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task", "spawn_agent"})


@dataclass
class _PendingCall:
    """A call awaiting its matching result (or EOF drain)."""

    name: str
    input_chars: int
    session_id: str
    ts: str
    usage: dict[str, object] | None
    usage_provenance: UsageProvenance
    model: str | None

    def finish(
        self,
        *,
        agent: str,
        source: str,
        project: str,
        output_chars: int,
        error: str | None = None,
        result_source: str | None = None,
        no_result: bool = False,
    ) -> ToolCall:
        """Emit the joined (or EOF-unmatched) `ToolCall` for this pending entry."""
        return ToolCall(
            agent=agent,
            source=source,
            project=project,
            name=self.name,
            input_chars=self.input_chars,
            output_chars=output_chars,
            session_id=self.session_id,
            ts=self.ts,
            usage=self.usage,
            usage_provenance=self.usage_provenance,
            duration_ms=None,
            error=error,
            model=self.model,
            no_result=no_result,
            result_source=result_source,
            is_deferral=self.name in DEFERRAL_TOOL_NAMES,
            is_subagent_fanout=self.name in SUBAGENT_TOOL_NAMES,
        )


def _drain_pending(
    pending: dict[str, _PendingCall],
    *,
    agent: str,
    source: str,
    project: str,
) -> list[ToolCall]:
    """S6: unmatched calls at EOF are kept with `no_result=True`, never dropped."""
    return [
        call.finish(
            agent=agent,
            source=source,
            project=project,
            output_chars=0,
            no_result=True,
        )
        for call in pending.values()
    ]


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
        #
        # `version` excludes hermes trace exports, which are claude-SHAPED but
        # have a different producer and different guarantees. Detection asserts
        # exactly one parser claims a line, so this predicate must not overlap
        # HermesTraceParser's.
        return "sessionId" in entry and entry.get("version") != HERMES_TRACE_VERSION

    @classmethod
    def _provenance(cls, usage: object) -> UsageProvenance:
        """Overridden by producers that know why usage is absent (S29).

        A classmethod, not a ClassVar: a ClassVar would need a `None` sentinel on
        ClaudeParser meaning "infer per row", reintroducing a null with two
        meanings inside the design meant to eliminate one.
        """
        return (
            UsageProvenance.PRESENT
            if isinstance(usage, dict)
            else UsageProvenance.ABSENT_UNEXPECTED
        )

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
                        usage_provenance=self._provenance(usage),
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
                    pending_call.finish(
                        agent=agent,
                        source=source,
                        project=project,
                        output_chars=result_len(payload),
                        error=error,
                        result_source=payload_source,
                    )
                )

        calls.extend(
            _drain_pending(pending, agent=agent, source=source, project=project)
        )
        return ParseResult(calls=calls, malformed=malformed)


class CodexParser(TranscriptParser):
    """codex JSONL: `response_item` records joined on `payload.call_id` (TB-12).

    A well-formed schema that shares nothing with claude's. Three call shapes join
    to three output shapes on `call_id` -- never `tool_use_id`, which is why a
    claude-only parser once read 60 codex sessions as 2089 dropped calls and a
    healthy zero. See `CALL_SHAPES`: the shapes agree on `call_id` and on nothing
    else, so each needs its own input field and its own name source.

    Two fields are session-scoped rather than call-scoped and must be carried
    forward as the transcript streams:

      * `session_id` comes from the first `session_meta`'s `id` -- the rollout's
        own identity. NOT `payload.session_id`, which is absent from older
        rollouts and names the parent thread in a subagent rollout.
      * `model` lives on `turn_context` and may change between turns, so a call
        is attributed to the last `turn_context` that preceded it.

    `web_search_call` is not JOINED as a call: it carries no `call_id` and has no
    matching output record, so this parser's key cannot reach it. But it is a real
    tool call, so it is counted in `ParseResult.unjoinable` by kind (S38, TB-24) and
    surfaced in the report Summary -- named, not silently dropped, and not faked into
    a `no_result` orphan that would inflate codex's call count.

    codex reports token usage as per-TURN `token_count` events. A turn routinely
    contains several tool calls, so those totals cannot be divided across calls
    without inventing an attribution the producer never made. Usage is therefore
    ABSENT_BY_SCHEMA (S29) -- a producer assertion, not a failure to look.

    There is likewise no per-call error channel: codex encodes exit status inside
    the output TEXT ("Process exited with code 1"), and `custom_tool_call.status`
    is `completed` even for a tool that failed. `error` is always None rather than
    guessed from prose.
    """

    schema_tag: ClassVar[str] = "codex"

    # Every top-level record kind codex emits. Disjoint from claude's
    # user/assistant/system/summary, and codex never carries `sessionId`.
    RECORD_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"session_meta", "response_item", "event_msg", "turn_context", "compacted"}
    )

    # The three paired call shapes, which agree on `call_id` and on nothing else.
    # Each row is (payload field holding the input, fixed name or None to read
    # `payload.name`). Collapsing these into one rule drops data every time:
    #   * reading `arguments` for custom_tool_call zeroes every apply_patch's size
    #   * requiring `payload.name` skips tool_search_call, which has no name field
    CALL_SHAPES: ClassVar[dict[str, tuple[str, str | None]]] = {
        "function_call": ("arguments", None),
        "custom_tool_call": ("input", None),
        # `ToolSearch` is the synthetic name stamped for nameless tool_search_call
        # records so DEFERRAL_TOOL_NAMES (and thus is_deferral) matches at emit.
        "tool_search_call": ("arguments", "ToolSearch"),
    }

    # Output shape -> the payload field holding the result. tool_search returns a
    # `tools` list; the other two return an `output` string.
    OUTPUT_FIELDS: ClassVar[dict[str, str]] = {
        "function_call_output": "output",
        "custom_tool_call_output": "output",
        "tool_search_output": "tools",
    }

    # Record kinds that are real tool calls but carry no `call_id` and have no
    # paired output, so this parser's join key cannot reach them. Counted as
    # `ParseResult.unjoinable` rather than dropped, so codex's ~4% web-search
    # undercount is named in the Summary, not silently absent (S38, TB-24).
    UNJOINABLE_TYPES: ClassVar[frozenset[str]] = frozenset({"web_search_call"})

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        # A positive declaration: codex names its record kind in top-level `type`
        # and always pairs it with a `payload` object.
        return entry.get("type") in cls.RECORD_TYPES and isinstance(
            entry.get("payload"), dict
        )

    def parse(
        self, lines: Iterable[str], *, agent: str, source: str, project: str
    ) -> ParseResult:
        """Join calls to their outputs on `call_id`, streaming.

        Mirrors ClaudeParser's contract: malformed lines are counted and skipped,
        never fatal (S5); a call with no matching output by end-of-input is kept
        with `output_chars=0, no_result=True` rather than dropped (S6).
        """
        pending: dict[str, _PendingCall] = {}
        calls: list[ToolCall] = []
        unjoinable: dict[str, int] = {}
        malformed = 0
        session_id = ""
        model: str | None = None

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

            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = entry.get("type")

            if kind == "session_meta":
                # `payload.id` is the rollout's own identity. `payload.session_id` is
                # absent from older rollouts (118 of 183 in the local archive) and, in
                # a subagent rollout, names the PARENT thread -- keying on it collapses
                # subagents into their parent. Only the first record establishes
                # identity; a `compacted` session emits a second one.
                if not session_id:
                    found_id = payload.get("id") or payload.get("session_id")
                    if isinstance(found_id, str):
                        session_id = found_id
                continue

            if kind == "turn_context":
                found_model = payload.get("model")
                if isinstance(found_model, str):
                    model = found_model
                continue

            if kind != "response_item":
                continue

            ts = entry.get("timestamp")
            ts_str = ts if isinstance(ts, str) else ""
            payload_type = payload.get("type")

            if isinstance(payload_type, str) and payload_type in self.UNJOINABLE_TYPES:
                # A real tool call with no `call_id` and no paired output. Count it
                # by kind before the join guard below (which would silently drop it)
                # so the Summary can name the gap rather than report a silent zero.
                unjoinable[payload_type] = unjoinable.get(payload_type, 0) + 1
                continue

            call_id = payload.get("call_id")
            if not isinstance(call_id, str):
                continue

            if payload_type in self.CALL_SHAPES:
                input_field, fixed_name = self.CALL_SHAPES[payload_type]
                name = fixed_name if fixed_name is not None else payload.get("name")
                if not isinstance(name, str):
                    continue
                pending[call_id] = _PendingCall(
                    name=name,
                    input_chars=result_len(payload.get(input_field)),
                    session_id=session_id,
                    ts=ts_str,
                    usage=None,
                    usage_provenance=UsageProvenance.ABSENT_BY_SCHEMA,
                    model=model,
                )
            elif payload_type in self.OUTPUT_FIELDS and call_id in pending:
                output_field = self.OUTPUT_FIELDS[payload_type]
                pending_call = pending.pop(call_id)
                calls.append(
                    pending_call.finish(
                        agent=agent,
                        source=source,
                        project=project,
                        output_chars=result_len(payload.get(output_field)),
                        result_source="payload",
                    )
                )

        calls.extend(
            _drain_pending(pending, agent=agent, source=source, project=project)
        )
        return ParseResult(calls=calls, malformed=malformed, unjoinable=unjoinable)


class HermesTraceParser(ClaudeParser):
    """`hermes sessions export --format trace`: the claude schema, a different producer.

    Inherits the entire parse path -- the export really is claude-shaped, which is
    why a lone ClaudeParser once swallowed it silently (TB-18). What differs is the
    guarantee: the trace serializer drops `message.usage` and `requestId`, so every
    call it yields has an unmeasurable usage channel. Because the producer declares
    itself in `version`, this parser can name the cause rather than merely observe
    the absence.

    `requestId` is likewise absent. That is `probe.py`'s problem, not this parser's;
    see S30.
    """

    schema_tag: ClassVar[str] = "hermes-trace"

    @classmethod
    def claims_line(cls, entry: dict[str, object]) -> bool:
        return "sessionId" in entry and entry.get("version") == HERMES_TRACE_VERSION

    @classmethod
    def _provenance(cls, usage: object) -> UsageProvenance:
        # Unconditional: trace never carries usage, so this must not consult the value.
        return UsageProvenance.ABSENT_BY_EXPORT
