"""Active probes: tool-vs-Bash comparison over the vendored corpus (S16-S18, S26)."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from toolbench.adapters import detect_parser
from toolbench.parsers import (
    HermesTraceParser,
    _PendingCall,
    _result_id,
    _result_payload,
)
from toolbench.transcript import ToolCall, UsageProvenance, result_len

BASH_TOOL_NAME = "Bash"


class SeededReportError(RuntimeError):
    """Raised when a comparison table containing no real measurement is rendered.

    A fully-seeded table restates `SEED_BASELINES` and measures nothing. It must
    not be written to `reports/` as though it were a result.
    """


class NonIsolableTurns(RuntimeError):
    """Raised when turns cannot be keyed to the billing unit (S26).

    A probe that cannot group by `requestId` does not produce an incomplete
    measurement -- it produces a confidently wrong one, by silently treating each
    JSONL record as its own API response. There is no useful degraded mode, so
    there is no partial-corpus path.
    """


@dataclass(frozen=True)
class ProbeSpec:
    """One matched tool-arm-vs-Bash-arm pair over a single vendored corpus file."""

    id: str
    corpus_path: str
    task: str
    tool_names: tuple[str, ...]
    target: str
    tool_sentinel: str
    bash_sentinel: str

    @property
    def tool_name(self) -> str:
        """The name the tool arm is reported under: the plugin-namespaced form."""
        return self.tool_names[0]


def _spec(id_: str, corpus_path: str, task: str, serena_tool: str) -> ProbeSpec:
    """Build a spec from serena's bare tool name (e.g. `find_file`).

    Claude Code namespaces an MCP tool by how the server was installed. Serena
    reaches this machine as the `serena` plugin's `serena` server, so calls are
    recorded as `mcp__plugin_serena_serena__find_file`. A bare install would
    record `mcp__serena__find_file`. Both are accepted; matching stays exact.

    `target` is the corpus basename. It is what identifies the tool arm (S17):
    a tool arm cannot carry a sentinel, because serena's schemas have no inert
    free-text field to park one in.
    """
    return ProbeSpec(
        id=id_,
        corpus_path=corpus_path,
        task=task,
        tool_names=(
            f"mcp__plugin_serena_serena__{serena_tool}",
            f"mcp__serena__{serena_tool}",
        ),
        target=Path(corpus_path).name,
        tool_sentinel=f"TB_PROBE_{id_}_TOOL_V2",
        bash_sentinel=f"TB_PROBE_{id_}_BASH_V2",
    )


PROBE_SPECS: tuple[ProbeSpec, ...] = (
    _spec("01", "tools/regex_check.py", "find", "find_file"),
    _spec("02", "tools/mcp.py", "search", "search_for_pattern"),
    _spec("03", "tools/monitor.py", "find", "find_file"),
    _spec("04", "tools/llm_extraction.py", "search", "search_for_pattern"),
    _spec("05", "tools/code_analysis.py", "find", "find_file"),
)

# A call that reads the transcript corpus, or the files where the sentinels are
# defined, is searching for a probe rather than running one.
MENTION_MARKERS: tuple[str, ...] = (
    ".claude/projects",
    "toolbench/probe.py",
    "protocols/active-probes.md",
    "protocols/probe-run-sheet.md",
)

# Seeded #8376 baselines (S18): used per (task, arm) only when that arm has
# no matching call in the scored session.
SEED_BASELINES: dict[tuple[str, str], int] = {
    ("search", "serena"): 723,
    ("search", "bash"): 794,
    ("find", "serena"): 68,
    ("find", "bash"): 89,
}


@dataclass
class ArmMatch:
    """Per-probe match result: the joined `ToolCall` for each arm, if found."""

    tool: ToolCall | None = None
    tool_isolable: bool = False
    bash: ToolCall | None = None
    bash_isolable: bool = False


@dataclass
class ComparisonRow:
    """One row of the S18 comparison table: both arms of a single probe."""

    probe_id: str
    corpus_path: str
    task: str
    tool_name: str
    tool_tokens: int
    tool_usage_tokens: int | None
    tool_seeded: bool
    bash_tokens: int
    bash_usage_tokens: int | None
    bash_seeded: bool


@dataclass
class _TurnStats:
    """What one API response emitted, for deciding whether its usage is attributable."""

    tool_uses: int = 0
    non_tool_output: bool = False


@dataclass(frozen=True)
class _ProbeHit:
    """One tool_use joined (or EOF-drained) with the raw input text probe matching needs."""

    name: str
    serialized_input: str
    turn_key: str
    call: ToolCall


@dataclass
class _ScanResult:
    """`hits` carry joined calls + raw input; `turns` is keyed by `turn_key`."""

    hits: list[_ProbeHit]
    turns: dict[str, _TurnStats]


def _turn_key(entry: dict[str, object]) -> str:
    """The unit `output_tokens` is billed against: the API response (S26).

    Claude Code writes one API response as several JSONL entries -- `thinking`,
    `text`, and each `tool_use` -- sharing a `requestId` and a single `usage`
    figure, but carrying *distinct* timestamps. Grouping by timestamp therefore
    sees every response as a lone block, which is the TB-16 defect. There is no
    timestamp fallback: an entry that cannot be keyed to a response is refused.
    """
    request_id = entry.get("requestId")
    if not (isinstance(request_id, str) and request_id):
        raise NonIsolableTurns(
            "probe requires requestId to group turns by the billing unit (S26); "
            "this entry has none. hermes --format trace exports never carry it."
        )
    return f"req:{request_id}"


def _is_assistant(entry: dict[str, object], message: dict[str, object]) -> bool:
    """User records carry `text` blocks too (tool results); they are not model output."""
    return entry.get("type") != "user" and message.get("role") != "user"


def _emits_non_tool_output(block: dict[str, object]) -> bool:
    """True for a block that costs `output_tokens` without being the tool call.

    Prose and reasoning are both billed to `output_tokens`. A whitespace-only
    text block costs nothing and must not disqualify an otherwise clean arm.
    """
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        return isinstance(text, str) and bool(text.strip())
    return kind in ("thinking", "redacted_thinking")


def _scan_tool_use_blocks(path: str | os.PathLike[str]) -> _ScanResult:
    """Single pass: join tool_use→result like ClaudeParser, keep raw input for matching.

    Previously this walked the file once for sentinels and again via
    `parse_session` for joined `ToolCall`s, then glued them on `(ts, name)`.
    One pass deletes that join and the Claude-only shim dependency.

    Routes through `adapters.detect_parser` to refuse a hermes trace export by
    name before it silently produces a plausible, wrong answer. The `_turn_key`
    guard is still the load-bearing S26 check for any corpus.
    """
    hits: list[_ProbeHit] = []
    turns: dict[str, _TurnStats] = defaultdict(_TurnStats)
    # tool_use id -> (pending call, serialized input, turn_key)
    pending: dict[str, tuple[_PendingCall, str, str]] = {}
    session_path = Path(path)
    project = session_path.parent.name

    with session_path.open(encoding="utf-8") as handle:
        parser, replayed = detect_parser(handle)
        if parser.schema_tag == HermesTraceParser.schema_tag:
            raise NonIsolableTurns(
                "hermes --format trace carries no requestId, so turns cannot be "
                "keyed to the billing unit (S30). Trace exports are valid input to "
                "passive.py but not to probe.py. Use a native Claude transcript."
            )
        for raw_line in replayed:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue

            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            session_id = entry.get("sessionId")
            ts = entry.get("timestamp")
            session_id_str = session_id if isinstance(session_id, str) else ""
            ts_str = ts if isinstance(ts, str) else ""

            if (
                isinstance(message, dict)
                and isinstance(content, list)
                and _is_assistant(entry, message)
            ):
                key = _turn_key(entry)
                stats = turns[key]
                usage = message.get("usage")
                model = message.get("model")
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        stats.non_tool_output |= _emits_non_tool_output(block)
                        continue
                    stats.tool_uses += 1
                    tool_use_id = block.get("id")
                    name = block.get("name")
                    if not isinstance(tool_use_id, str) or not isinstance(name, str):
                        continue
                    pending[tool_use_id] = (
                        _PendingCall(
                            name=name,
                            input_chars=result_len(block.get("input")),
                            session_id=session_id_str,
                            ts=ts_str,
                            usage=usage if isinstance(usage, dict) else None,
                            usage_provenance=(
                                UsageProvenance.PRESENT
                                if isinstance(usage, dict)
                                else UsageProvenance.ABSENT_UNEXPECTED
                            ),
                            model=model if isinstance(model, str) else None,
                        ),
                        json.dumps(block.get("input")),
                        key,
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
                pending_call, serialized_input, turn_key = pending.pop(result_id)
                error = None
                if isinstance(result_block, dict) and result_block.get("is_error"):
                    error = "tool_error"
                hits.append(
                    _ProbeHit(
                        name=pending_call.name,
                        serialized_input=serialized_input,
                        turn_key=turn_key,
                        call=pending_call.finish(
                            agent="claude-code",
                            source="raw",
                            project=project,
                            output_chars=result_len(payload),
                            error=error,
                            result_source=payload_source,
                        ),
                    )
                )

    for pending_call, serialized_input, turn_key in pending.values():
        hits.append(
            _ProbeHit(
                name=pending_call.name,
                serialized_input=serialized_input,
                turn_key=turn_key,
                call=pending_call.finish(
                    agent="claude-code",
                    source="raw",
                    project=project,
                    output_chars=0,
                    no_result=True,
                ),
            )
        )
    return _ScanResult(hits=hits, turns=dict(turns))


def _mentions_probe_machinery(serialized_input: str) -> bool:
    """True when a call reads the transcript corpus or the probe's own source.

    Such a call is searching for a probe, not running one, whatever strings it
    happens to name.
    """
    return any(marker in serialized_input for marker in MENTION_MARKERS)


def _sentinels_in(serialized_input: str, probes: Sequence[ProbeSpec]) -> set[str]:
    """Every probe sentinel named anywhere in a call's serialized input."""
    return {
        sentinel
        for spec in probes
        for sentinel in (spec.tool_sentinel, spec.bash_sentinel)
        if sentinel in serialized_input
    }


def find_probe_calls(
    path: str | os.PathLike[str], probes: Sequence[ProbeSpec] = PROBE_SPECS
) -> dict[str, ArmMatch]:
    """Join each arm of each probe to a `ToolCall`, by the evidence that arm can leave.

    The two arms are identified differently, because they can carry different
    evidence (S17):

    * The **tool arm** is matched *structurally*: an accepted tool name plus the
      corpus target in the input. Serena's schemas (`find_file` takes exactly
      `file_mask` and `relative_path`) have no inert free-text field, so a tool
      arm physically cannot carry a sentinel without corrupting the very query
      being measured. It does not need one: `find_file` over `regex_check.py` is
      already unambiguously probe 01's tool arm.
    * The **bash arm** is matched by *sentinel*, because a shell command is
      unstructured text in which nothing else is reliably distinctive.

    A call is discarded outright when it trips `MENTION_MARKERS`, or names more
    than one sentinel -- both mean it is discussing probes rather than running
    them. A tool-arm candidate carrying some *other* probe's sentinel is
    likewise rejected: that is a search for a sentinel, not a probe arm.

    This narrows contamination; it does not eliminate it. A single-sentinel grep
    against the corpus file remains indistinguishable from the bash arm it
    imitates, which is why probes must be scored from a dedicated session (see
    `protocols/active-probes.md`).

    Matching an arm is separate from *pricing* it. An arm's `output_tokens` is
    attributable only when its whole API response emitted that one `tool_use`
    block and nothing else -- no second call, no prose, no reasoning (S26). A
    non-isolable arm still matches and keeps its real context tokens; usage
    renders as `—`. It is not re-seeded -- `*` marks only an absent arm.
    """
    scan = _scan_tool_use_blocks(path)

    matches: dict[str, ArmMatch] = {spec.id: ArmMatch() for spec in probes}
    for hit in scan.hits:
        if _mentions_probe_machinery(hit.serialized_input):
            continue
        present = _sentinels_in(hit.serialized_input, probes)
        if len(present) > 1:
            continue
        sentinel = next(iter(present), None)
        turn = scan.turns[hit.turn_key]
        isolable = turn.tool_uses == 1 and not turn.non_tool_output
        for spec in probes:
            arm = matches[spec.id]
            is_tool_arm = (
                hit.name in spec.tool_names
                and spec.target in hit.serialized_input
                and sentinel in (None, spec.tool_sentinel)
            )
            if is_tool_arm:
                arm.tool = hit.call
                arm.tool_isolable = isolable
            elif hit.name == BASH_TOOL_NAME and sentinel == spec.bash_sentinel:
                arm.bash = hit.call
                arm.bash_isolable = isolable
    return matches


def _usage_output_tokens(call: ToolCall, isolable: bool) -> int | None:
    """Real usage tokens, only when the turn is isolable (S18)."""
    if not isolable or call.usage is None:
        return None
    tokens = call.usage.get("output_tokens")
    return tokens if isinstance(tokens, int) else None


def build_comparison_table(
    matches: dict[str, ArmMatch], probes: Sequence[ProbeSpec] = PROBE_SPECS
) -> list[ComparisonRow]:
    """Emit one row per probe, seeding an arm from #8376 baselines when absent (S18)."""
    rows: list[ComparisonRow] = []
    for spec in probes:
        arm = matches.get(spec.id, ArmMatch())

        if arm.tool is not None:
            tool_tokens = arm.tool.tokens
            tool_usage = _usage_output_tokens(arm.tool, arm.tool_isolable)
            tool_seeded = False
        else:
            tool_tokens = SEED_BASELINES[(spec.task, "serena")]
            tool_usage = None
            tool_seeded = True

        if arm.bash is not None:
            bash_tokens = arm.bash.tokens
            bash_usage = _usage_output_tokens(arm.bash, arm.bash_isolable)
            bash_seeded = False
        else:
            bash_tokens = SEED_BASELINES[(spec.task, "bash")]
            bash_usage = None
            bash_seeded = True

        rows.append(
            ComparisonRow(
                probe_id=spec.id,
                corpus_path=spec.corpus_path,
                task=spec.task,
                tool_name=spec.tool_name,
                tool_tokens=tool_tokens,
                tool_usage_tokens=tool_usage,
                tool_seeded=tool_seeded,
                bash_tokens=bash_tokens,
                bash_usage_tokens=bash_usage,
                bash_seeded=bash_seeded,
            )
        )
    return rows


def is_fully_seeded(rows: Sequence[ComparisonRow]) -> bool:
    """True when no arm in the table came from a real call."""
    return bool(rows) and all(row.tool_seeded and row.bash_seeded for row in rows)


def render_report(rows: Sequence[ComparisonRow], *, allow_seeded: bool = False) -> str:
    """Render the S18 comparison table as markdown. `*` marks a seeded arm.

    Raises `SeededReportError` when every arm is seeded, unless `allow_seeded`.
    Such a table measures nothing -- it restates `SEED_BASELINES` -- and an
    asterisk in a cell is too quiet to carry that.
    """
    if is_fully_seeded(rows) and not allow_seeded:
        raise SeededReportError(
            "every arm is seeded: this table restates SEED_BASELINES and measures "
            "nothing. Score a real probe session with --session, or pass "
            "--allow-seeded to emit the baseline table deliberately."
        )
    lines = [
        "# Active probe comparison (S18)",
        "",
        "| probe | corpus | task | tool_name | tool tokens | tool usage | "
        "bash tokens | bash usage |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        tool_tokens = f"{row.tool_tokens}{'*' if row.tool_seeded else ''}"
        bash_tokens = f"{row.bash_tokens}{'*' if row.bash_seeded else ''}"
        tool_usage = str(row.tool_usage_tokens) if row.tool_usage_tokens is not None else "—"
        bash_usage = str(row.bash_usage_tokens) if row.bash_usage_tokens is not None else "—"
        lines.append(
            f"| {row.probe_id} | {row.corpus_path} | {row.task} | {row.tool_name} "
            f"| {tool_tokens} | {tool_usage} | {bash_tokens} | {bash_usage} |"
        )
    lines.append("")
    lines.append("`*` = seeded #8376 baseline (arm absent from the scored session).")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Active tool-vs-Bash probe comparison (S16-S18)."
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Probe session JSONL to score. Without it every arm is seeded and "
        "the report is refused unless --allow-seeded.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/active-probe-comparison.md"),
        help="Report output path (created if missing).",
    )
    parser.add_argument(
        "--allow-seeded",
        action="store_true",
        help="Emit the all-seeded baseline table. It measures nothing; do not "
        "publish it as a result.",
    )
    args = parser.parse_args(argv)

    matches: dict[str, ArmMatch] = (
        find_probe_calls(args.session)
        if args.session is not None
        else {spec.id: ArmMatch() for spec in PROBE_SPECS}
    )

    # Render before touching the filesystem so a refused report leaves no trace.
    report = render_report(build_comparison_table(matches), allow_seeded=args.allow_seeded)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
