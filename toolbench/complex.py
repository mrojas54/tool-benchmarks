"""Complex debug probe: locate-then-fix over four toolset arms.

The active probe (S16-S18) measures cost per call, with the call dictated by a
run sheet -- which is what makes it honest, and also why it cannot answer "which
toolset should I reach for". A tool that costs 3x per call but needs a third as
many calls is free.

This module measures tokens to a *verified outcome* instead. The price is that
the agent chooses its own path, so step count dominates; everything here exists
to keep that measurable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# The agent emits this once, as soon as it believes it has localized the defect.
# Making the moment explicit beats inferring it: N1 is the tokens before it.
LOCATED_PREFIX = "LOCATED:"

# Never grantable to any arm.
BANNED_TOOLS: tuple[str, ...] = ("Task", "Agent")

BASELINE_TOOLS: tuple[str, ...] = ("Read", "TodoWrite")

SERENA_TOOLS: tuple[str, ...] = tuple(
    f"mcp__plugin_serena_serena__{name}"
    for name in (
        "find_symbol",
        "find_referencing_symbols",
        "find_file",
        "search_for_pattern",
        "get_symbols_overview",
        "list_dir",
        "read_file",
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
        "replace_content",
    )
)
NATIVE_TOOLS: tuple[str, ...] = ("Grep", "Glob", "Edit")


@dataclass(frozen=True)
class ArmSpec:
    """One toolset under test. `allowed_tools` is passed to `claude -p --allowedTools`."""

    name: str
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True)
class Truth:
    """Ground truth for one defect. Derived from its injection patch, never by hand."""

    file: str
    symbol: str
    lines: tuple[int, int]


_KNOWN_WINNERS = ("serena", "native", "bash", "neutral")

# <repo>-<id>-<slug>, e.g. "wids-D3-call-chain-lambda" -> ("wids", "D3", "call-chain-lambda").
# Ids repeat across repos (both wids and maltese have a D3): the (repo, id) pair
# is the fixture's real key, never id alone.
_FIXTURE_DIR_RE = re.compile(r"^(?P<repo>[a-z]+)-(?P<id>D\d+)-(?P<slug>.+)$")

# Matches "Predicted winner: native", "Predicted winner: **serena**", and
# "**Predicted winner: serena**" -- prediction.md's bolding is not consistent
# across fixtures, so the parser tolerates either.
_WINNER_RE = re.compile(r"Predicted winner:\s*\**\s*(\w+)", re.IGNORECASE)


@dataclass(frozen=True)
class DefectSpec:
    """One injected defect, with its winner predicted BEFORE any trial runs.

    A defect whose prediction comes out wrong is the most informative cell in the
    table. A run in which every prediction lands has taught us nothing.
    """

    id: str
    repo: str
    language: str
    truth: Truth
    predicted_winner: str
    rationale: str
    # The scoped (fast, per-defect) command that verifies a fix, and the
    # directory (relative to this defect's corpus root) it must run from.
    # Per-defect and scoped on purpose: this runs once per trial, dozens of
    # times -- an unscoped `cargo test --workspace` (46s) instead of the
    # scoped `-p falcon-mcp --lib sandbox` (3.1s) would multiply out badly.
    oracle_cmd: tuple[str, ...]
    oracle_cwd: str


def _parse_fixture_dirname(name: str) -> tuple[str, str]:
    """Split `<repo>-<id>-<slug>` into (repo, id). Never assume id is unique --
    wids and maltese both have a D3; (repo, id) is the real key."""
    match = _FIXTURE_DIR_RE.match(name)
    if not match:
        raise ValueError(
            f"fixture dir {name!r} does not match the required <repo>-<id>-<slug> shape"
        )
    return match.group("repo"), match.group("id")


def _read_truth(path: Path, fixture: str) -> Truth:
    if not path.exists():
        raise FileNotFoundError(f"{fixture}: missing {path.name} (required ground truth)")
    data = json.loads(path.read_text(encoding="utf-8"))
    start, end = data["lines"]
    return Truth(file=data["file"], symbol=data["symbol"], lines=(start, end))


def _extract_rationale(text: str, after: int) -> str:
    """The rationale paragraph sits between the winner line and "Falsified if:".
    Some fixtures label it "Rationale:" explicitly (maltese, wids); rich does not.
    Both are accepted since neither is wrong, just differently formatted."""
    body = text[after:].split("Falsified if:")[0].strip()
    body = re.sub(r"^Rationale:\s*", "", body, flags=re.IGNORECASE)
    body = body.strip("*").strip()
    return re.sub(r"\s+", " ", body)


def _read_prediction(path: Path, fixture: str) -> tuple[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"{fixture}: missing {path.name} (required pre-registered prediction)")
    text = path.read_text(encoding="utf-8")
    match = _WINNER_RE.search(text)
    if not match:
        raise ValueError(f"{fixture}: prediction.md has no 'Predicted winner:' line")
    winner = match.group(1).lower()
    if winner not in _KNOWN_WINNERS:
        raise ValueError(
            f"{fixture}: predicted winner {winner!r} is not one of {_KNOWN_WINNERS}"
        )
    return winner, _extract_rationale(text, match.end())


def _read_oracle(path: Path, fixture: str) -> tuple[tuple[str, ...], str, str]:
    if not path.exists():
        raise FileNotFoundError(f"{fixture}: missing {path.name} (required oracle command)")
    data = json.loads(path.read_text(encoding="utf-8"))
    cmd = tuple(data["cmd"])
    if not cmd:
        raise ValueError(f"{fixture}: oracle.json 'cmd' must be a non-empty argv list")
    cwd = data["cwd"]
    language = data.get("language")
    if not language:
        raise ValueError(f"{fixture}: oracle.json missing 'language'")
    return cmd, cwd, language


def load_defects(root: str | Path = "probes/complex") -> tuple[DefectSpec, ...]:
    """Load DefectSpec objects from the committed fixtures under `root`.

    A hand-maintained DEFECTS tuple can silently drift from the patches it
    claims to describe -- a truth.json edited without updating a parallel
    literal, a fixture added and forgotten. Deriving the list from the
    fixtures on every import makes fabricated ground truth impossible by
    construction rather than by discipline.
    """
    root_path = Path(root)
    defects = []
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        repo, defect_id = _parse_fixture_dirname(entry.name)
        truth = _read_truth(entry / "truth.json", entry.name)
        predicted_winner, rationale = _read_prediction(entry / "prediction.md", entry.name)
        oracle_cmd, oracle_cwd, language = _read_oracle(entry / "oracle.json", entry.name)
        defects.append(
            DefectSpec(
                id=defect_id,
                repo=repo,
                language=language,
                truth=truth,
                predicted_winner=predicted_winner,
                rationale=rationale,
                oracle_cmd=oracle_cmd,
                oracle_cwd=oracle_cwd,
            )
        )
    # Sorted by id (with repo as a tiebreaker for determinism): ids repeat
    # across repos on purpose, so this is not a unique ordering key by itself.
    defects.sort(key=lambda d: (d.id, d.repo))
    return tuple(defects)


DEFECTS: tuple[DefectSpec, ...] = load_defects()


def build_arms(test_gate: str) -> tuple[ArmSpec, ...]:
    """The four arms. `test_gate` is a command-scoped Bash rule, e.g. `Bash(cargo test:*)`.

    The gate exists so the fix checkpoint is verifiable without handing `rg` to
    the serena arm. `Read` is held constant across arms so the measured variable
    is search and edit, not file viewing.
    """
    base = BASELINE_TOOLS + (test_gate,)
    return (
        ArmSpec("serena", base + SERENA_TOOLS),
        ArmSpec("native", base + NATIVE_TOOLS),
        # The bash arm gets a full shell, which subsumes the gate.
        ArmSpec("bash", BASELINE_TOOLS + ("Bash",)),
        ArmSpec("control", base + SERENA_TOOLS + NATIVE_TOOLS + ("Bash",)),
    )
