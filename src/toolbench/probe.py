"""Active probes: tool-vs-Bash comparison over the vendored corpus (S16-S18, S26)."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from toolbench.adapters import detect_parser
from toolbench.parsers import ClaudeParser, HermesTraceParser, TurnKeyError, _claude_turn_key
from toolbench.transcript import ToolCall, TurnStats

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
    turns: dict[str, TurnStats]


def _turn_key(entry: dict[str, object]) -> str:
    """The unit `output_tokens` is billed against: the API response (S26).

    Thin wrapper over the parser seam so probe's public error type stays
    `NonIsolableTurns` (tests and callers depend on it).
    """
    try:
        return _claude_turn_key(entry)
    except TurnKeyError as exc:
        raise NonIsolableTurns(
            "probe requires requestId to group turns by the billing unit (S26); "
            "this entry has none. hermes --format trace exports never carry it."
        ) from exc


def _scan_tool_use_blocks(path: str | os.PathLike[str]) -> _ScanResult:
    """Join via ClaudeParser with keep-raw + turn tracking (CQ 7.1).

    Routes through `adapters.detect_parser` to refuse a hermes trace export by
    name before it silently produces a plausible, wrong answer. Turn-key
    refusal (S26) is raised from the parser seam as `TurnKeyError` and mapped
    to `NonIsolableTurns`.
    """
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
        try:
            result = ClaudeParser().parse(
                replayed,
                agent="claude-code",
                source="raw",
                project=project,
                keep_raw_input=True,
                track_turns=True,
            )
        except TurnKeyError as exc:
            raise NonIsolableTurns(
                "probe requires requestId to group turns by the billing unit (S26); "
                "this entry has none. hermes --format trace exports never carry it."
            ) from exc

    hits = [
        _ProbeHit(
            name=call.name,
            serialized_input=call.raw_input or "",
            turn_key=call.turn_key or "",
            call=call,
        )
        for call in result.calls
        if call.raw_input is not None and call.turn_key is not None
    ]
    return _ScanResult(hits=hits, turns=result.turns)


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
