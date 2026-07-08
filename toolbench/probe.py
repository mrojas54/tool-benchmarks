"""Active probes: tool-vs-Bash comparison over the vendored corpus (S16-S18)."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from toolbench.transcript import ToolCall, parse_session

BASH_TOOL_NAME = "Bash"


@dataclass(frozen=True)
class ProbeSpec:
    """One matched tool-arm-vs-Bash-arm pair over a single vendored corpus file."""

    id: str
    corpus_path: str
    task: str
    tool_name: str
    tool_sentinel: str
    bash_sentinel: str


def _spec(id_: str, corpus_path: str, task: str, tool_name: str) -> ProbeSpec:
    return ProbeSpec(
        id=id_,
        corpus_path=corpus_path,
        task=task,
        tool_name=tool_name,
        tool_sentinel=f"TB_PROBE_{id_}_TOOL_V2",
        bash_sentinel=f"TB_PROBE_{id_}_BASH_V2",
    )


PROBE_SPECS: tuple[ProbeSpec, ...] = (
    _spec("01", "tools/regex_check.py", "find", "mcp__serena__find_file"),
    _spec("02", "tools/mcp.py", "search", "mcp__serena__search_for_pattern"),
    _spec("03", "tools/monitor.py", "find", "mcp__serena__find_file"),
    _spec("04", "tools/llm_extraction.py", "search", "mcp__serena__search_for_pattern"),
    _spec("05", "tools/code_analysis.py", "find", "mcp__serena__find_file"),
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


def _scan_tool_use_blocks(path: str | os.PathLike[str]) -> list[tuple[str, str, str]]:
    """Raw pass over a session JSONL: `(ts, name, serialized_input)` per `tool_use` block.

    Deliberately independent of `transcript.parse_session`, which normalizes
    tool input to a character count (`result_len`) and drops the raw text a
    sentinel would live in.
    """
    records: list[tuple[str, str, str]] = []
    session_path = Path(path)
    with session_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
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
            if not isinstance(content, list):
                continue
            ts = entry.get("timestamp")
            ts_str = ts if isinstance(ts, str) else ""
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if not isinstance(name, str):
                    continue
                records.append((ts_str, name, json.dumps(block.get("input"))))
    return records


def find_probe_calls(
    path: str | os.PathLike[str], probes: Sequence[ProbeSpec] = PROBE_SPECS
) -> dict[str, ArmMatch]:
    """Match sentinel + expected tool name (S17) to a joined `ToolCall`.

    A call matches an arm only if it carries that arm's sentinel *and* used
    the expected tool for that arm -- both conditions, not either.
    """
    raw_records = _scan_tool_use_blocks(path)
    turn_call_counts: Counter[str] = Counter(ts for ts, _name, _input in raw_records)

    calls_by_key: dict[tuple[str, str], ToolCall] = {}
    for call in parse_session(path).calls:
        calls_by_key.setdefault((call.ts, call.name), call)

    matches: dict[str, ArmMatch] = {spec.id: ArmMatch() for spec in probes}
    for ts, name, serialized_input in raw_records:
        isolable = turn_call_counts[ts] == 1
        for spec in probes:
            arm = matches[spec.id]
            if name == spec.tool_name and spec.tool_sentinel in serialized_input:
                arm.tool = calls_by_key.get((ts, name))
                arm.tool_isolable = isolable
            elif name == BASH_TOOL_NAME and spec.bash_sentinel in serialized_input:
                arm.bash = calls_by_key.get((ts, name))
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


def render_report(rows: Sequence[ComparisonRow]) -> str:
    """Render the S18 comparison table as markdown. `*` marks a seeded arm."""
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
        help="Probe session JSONL to score; omit for an all-seeded table.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/active-probe-comparison.md"),
        help="Report output path (created if missing).",
    )
    args = parser.parse_args(argv)

    matches: dict[str, ArmMatch] = (
        find_probe_calls(args.session)
        if args.session is not None
        else {spec.id: ArmMatch() for spec in PROBE_SPECS}
    )

    report = render_report(build_comparison_table(matches))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
