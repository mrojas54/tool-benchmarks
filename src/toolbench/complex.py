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
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from toolbench.adapters import detect_parser
from toolbench.parsers import ClaudeParser
from toolbench.shell_safety import (
    BANNED_TOOLS as BANNED_TOOLS,
    _command_escapes_gate as _command_escapes_gate,
    arm_violations as arm_violations,
    read_escapes as read_escapes,
)
from toolbench.transcript import JsonLines, ToolCall

# The agent emits this once, as soon as it believes it has localized the defect.
# Making the moment explicit beats inferring it: N1 is the tokens before it.
LOCATED_PREFIX = "LOCATED:"

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

# Arms granted a bare, unrestricted `Bash` (a full shell) by `build_arms`. Their
# read-scope audit is BEST-EFFORT only -- a shell reads via indirection no static
# audit can follow -- so the profile discloses this whenever such an arm is present.
FULL_SHELL_ARMS: frozenset[str] = frozenset({"bash", "control"})


@dataclass(frozen=True)
class ArmSpec:
    """One toolset under test. `allowed_tools` is passed to `claude -p --allowedTools`."""

    name: str
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True)
class Truth:
    """Ground truth for one defect. Derived from its injection patch, never by hand.

    COORDINATE CONVENTION -- `lines` are POST-PATCH line numbers.

    They index the file *as the agent under test sees it*: the defect patch is
    already applied in its worktree, so the only coordinates it can possibly
    report in its `LOCATED:` line are post-patch ones. A patch that changes the
    line count (rich-D1 adds two) makes pre- and post-patch coordinates differ,
    and a truth recorded in pre-patch space would silently be off by that delta.

    `located_correct` matches by range OVERLAP rather than equality, so a small
    drift usually still scores -- which is exactly why the convention is written
    down instead of left to be inferred from whichever fixture is read first.
    `tests/test_complex.py::PatchTruthTests` enforces it: every line the patch
    actually changes, numbered in the post-image, must fall inside `lines`.
    """

    file: str
    symbol: str
    lines: tuple[int, int]


_KNOWN_WINNERS = ("serena", "native", "bash", "neutral")

# The package root, so `import toolbench.complex` works from any cwd AND any
# install (editable checkout or wheel). The fixtures ship inside the package
# and live at a fixed path relative to this module, never relative to the
# process's working directory: a relative default made the import itself raise
# FileNotFoundError anywhere but the repo root, and a repo-root anchor made it
# raise anywhere but a checkout.
_PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE_ROOT = _PACKAGE_ROOT / "probes" / "complex"
MANIFEST_PATH = _PACKAGE_ROOT / "corpus" / "manifest.json"

# A gate is the leading run of bare-word tokens in the oracle's own argv:
# ["npx","vitest","run","tests/x.ts"] -> `Bash(npx vitest run:*)`. A token with a
# slash, a dot or a leading dash ends the prefix -- it is an argument, not part of
# the command's identity.
_BARE_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

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
    # The Bash rule granted to the serena/native/control arms, DERIVED from this
    # defect's own oracle_cmd -- never from its repo. The gate was per-repo once,
    # and the prompt then told the agent to "make the test suite pass" while the
    # arm was denied the only command that proves it: maltese's repo gate was
    # `Bash(cargo test:*)` while three of its four defects are vitest, and rich's
    # was `Bash(python -m pytest:*)` while its oracle runs the venv's pytest.
    # Four of eight cells were unprovable. Derivation makes that class of drift
    # impossible: the gate is a function of the oracle, so it cannot disagree.
    test_gate: str


def derive_test_gate(cmd: tuple[str, ...]) -> str:
    """`Bash(<command prefix>:*)` for an oracle argv -- the arm's fix checkpoint.

    Scoped to the command, never to a bare interpreter: the prefix stops at the
    first argument-shaped token, so `.venv/bin/pytest tests/test_progress.py -q`
    grants `Bash(.venv/bin/pytest:*)` and nothing wider. (This is also why rich's
    oracle invokes `pytest` directly rather than `python -m pytest` -- granting a
    Python interpreter to the serena arm would hand it arbitrary code execution,
    i.e. a shell, i.e. the very thing the arm exists to withhold.)
    """
    if not cmd:
        raise ValueError("cannot derive a test gate from an empty oracle command")
    prefix = [cmd[0]]
    for token in cmd[1:]:
        if not _BARE_WORD_RE.match(token):
            break
        prefix.append(token)
    return f"Bash({' '.join(prefix)}:*)"


def _known_repos() -> frozenset[str]:
    """Repo names the corpus manifest declares. A fixture dir naming any other
    repo is a typo, not a corpus: `wid-D2-...` would otherwise parse cleanly as
    repo "wid" and load as a real defect pointing at a repo that does not exist.
    """
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return frozenset(data)


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


def load_defects(root: str | Path = DEFAULT_FIXTURE_ROOT) -> tuple[DefectSpec, ...]:
    """Load DefectSpec objects from the committed fixtures under `root`.

    A hand-maintained DEFECTS tuple can silently drift from the patches it
    claims to describe -- a truth.json edited without updating a parallel
    literal, a fixture added and forgotten. Deriving the list from the
    fixtures on every import makes fabricated ground truth impossible by
    construction rather than by discipline.
    """
    root_path = Path(root)
    known = _known_repos()
    seen: dict[tuple[str, str], str] = {}
    defects = []
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        repo, defect_id = _parse_fixture_dirname(entry.name)
        if repo not in known:
            raise ValueError(
                f"fixture dir {entry.name!r} names repo {repo!r}, which is not in "
                f"the packaged corpus manifest {MANIFEST_PATH} ({sorted(known)}). A typo'd "
                f"prefix would otherwise load as a real defect against a corpus that does not exist."
            )
        if (repo, defect_id) in seen:
            raise ValueError(
                f"fixture dir {entry.name!r} duplicates ({repo}, {defect_id}), already "
                f"claimed by {seen[(repo, defect_id)]!r}. (repo, id) is the fixture's "
                f"key -- two fixtures sharing it silently collide in every lookup."
            )
        seen[(repo, defect_id)] = entry.name
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
                test_gate=derive_test_gate(oracle_cmd),
            )
        )
    # Sorted by id (with repo as a tiebreaker for determinism): ids repeat
    # across repos on purpose, so this is not a unique ordering key by itself.
    defects.sort(key=lambda d: (d.id, d.repo))
    return tuple(defects)


DEFECTS: tuple[DefectSpec, ...] = load_defects()


def find_located(path: str | Path) -> tuple[str, dict[str, object]] | None:
    """The first assistant text block emitting `LOCATED: {...}`, with its timestamp.

    Returns `None` when the agent never claimed a localization -- a real outcome
    (it may still have guessed its way to a passing test), recorded as such and
    never back-filled.
    """
    with Path(path).open(encoding="utf-8") as handle:
        # Scoring does not report a malformed count, so `JsonLines.malformed` is
        # ignored here. The guard it brings is not: this loop used to hand a
        # bare-scalar line (`123`) straight to `entry.get(...)` and die with an
        # AttributeError, taking the whole scoring run with it. A garbled
        # transcript line is a skipped line, never a crash.
        for entry in JsonLines(handle):
            if entry.get("type") != "assistant":
                continue
            # Same shape-checking discipline as ClaudeParser.parse: a field
            # arriving from outside is never dereferenced until its type is
            # checked. `message`, `content`, and `text` were each read straight
            # off the entry, so a transcript carrying any of them as the wrong
            # type crashed the run instead of skipping the line.
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                start = text.find(LOCATED_PREFIX)
                if start == -1:
                    continue
                payload = text[start + len(LOCATED_PREFIX) :].strip()
                end = payload.find("}")
                if end == -1:
                    continue
                try:
                    obj = json.loads(payload[: end + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    return str(entry.get("timestamp") or ""), obj
    return None


# The most extra width a claimed `lines` range may carry BEYOND the truth
# range's own span and still score as a correct localization. Overlap alone is
# not a guard: [0, 99999] overlaps every truth there is, so an overlap-only
# check cannot fail, and a check that cannot fail is not a check -- the same
# defect class the patch<->truth guard in tests/test_complex.py (MAX_TRUTH_SLACK)
# already exists to catch, one layer over (there: truth vs. the patch it
# describes; here: a claim vs. the truth it is scored against).
#
# Why 30, reusing that guard's constant rather than picking a fresh number for
# the same idea: the widest real symbol shipped across the 8 fixtures in
# probes/complex/*/truth.json is maltese-D5's `commitOneHandle` at 24 lines
# (29-52). A bound under 24 would score that symbol's own, exact, correct span
# as WRONG -- the opposite failure from the one being fixed, and worse, since it
# would fail a genuinely correct find. 30 clears 24 with headroom for a somewhat
# larger function, while a vacuous claim like [0, 99999] (span ~100000) or a
# whole-file guess like [1, 200] against a 1-line truth misses by two to four
# orders of magnitude -- there is no legitimate localization in the gap between
# 30 and those.
MAX_LOCATED_SLACK = 30


_NAME_PATH_SEP_RE = re.compile(r"[./:#]+")


def _name_path_matches(claim: object, truth_symbol: str) -> bool:
    """True iff one symbol's name-path components are a suffix of the other's.

    Raw string equality is wrong here because there is no one spelling of a
    symbol: serena's `find_symbol` reports name paths slash-separated
    (`TaskProgressColumn/render`), the fixtures record them dotted
    (`TaskProgressColumn.render`), and an agent may name only the leaf (`render`).
    Splitting both on `.`, `/`, `::`, `#` and matching by suffix (in either
    direction) makes those all agree while still rejecting a same-leaf, wrong-owner
    claim: `["SpinnerColumn","render"]` is not a suffix of
    `["TaskProgressColumn","render"]`. The file + line-range checks below are what
    disambiguate among same-named symbols; this only frees the symbol string from a
    spelling convention that was never actually specified.
    """
    if not isinstance(claim, str):
        return False
    claim_parts = [p for p in _NAME_PATH_SEP_RE.split(claim) if p]
    truth_parts = [p for p in _NAME_PATH_SEP_RE.split(truth_symbol) if p]
    if not claim_parts or not truth_parts:
        return False
    shorter, longer = sorted((claim_parts, truth_parts), key=len)
    return longer[len(longer) - len(shorter) :] == shorter


# KNOWN LIMITATION -- maltese-D4. Its truth symbol `detective` is an *imported
# binding* (`cli.ts:3`), not a definition, so an agent may never name it as a
# symbol at all and locate=0 across all four arms is a plausible, honest outcome.
# The fixture is deliberately left alone: tuning it to guarantee a locate would be
# confirmation, not verification -- exactly the trap the pilot exists to surface.
# If D4 locates nowhere, that is a recorded fixture limitation, not a bug here.


def located_correct(obj: dict[str, object], truth: Truth) -> bool:
    """File and normalized symbol must match; lines must overlap AND stay tight.

    Exact line equality would be brittle: an agent that reports a whole function
    body while the patch touched one line inside it has still localized
    correctly. But overlap by itself is vacuous -- `[0, 99999]` overlaps any
    truth, so a sloppy (or gaming) agent claiming a huge span would be scored as
    a correct find, silently inflating solve rate, the one number this benchmark
    exists to produce. MAX_LOCATED_SLACK bounds how much wider than the truth's
    own span a claim may be while still counting as a real localization.

    The symbol is matched by name-path suffix, not raw equality -- see
    `_name_path_matches`. The file and line-range checks are untouched: they are
    what disambiguate among the same-named symbols a suffix match admits.
    """
    if obj.get("file") != truth.file or not _name_path_matches(obj.get("symbol"), truth.symbol):
        return False
    lines = obj.get("lines")
    if not isinstance(lines, list) or len(lines) != 2:
        return False
    low, high = lines
    if not isinstance(low, int) or not isinstance(high, int) or high < low:
        return False
    if high < truth.lines[0] or low > truth.lines[1]:
        return False
    truth_span = truth.lines[1] - truth.lines[0] + 1
    claimed_span = high - low + 1
    return claimed_span - truth_span <= MAX_LOCATED_SLACK


@dataclass(frozen=True)
class TrialResult:
    """One (defect, arm, trial) cell.

    N1 and N2 are the two halves of ONE trial, split at the `LOCATED:` line: N1 is
    everything before it (navigation), N2 everything from it onward (edit). The
    split only exists if the line does. So both are None when the trial did not
    locate -- including when it went on to fix the bug anyway. Booking that trial's
    whole cost as N2 would file its NAVIGATION spend under the edit label, and
    `median_n2` would then be the median of two different quantities.

    `total` is every call's tokens and is always defined, so a fixed-but-unlocated
    trial keeps a real cost number. It is simply not called N2.
    """

    defect_id: str
    repo: str
    arm: str
    trial: int
    located: bool
    fixed: bool
    n1: int | None
    n2: int | None
    total: int
    steps: int
    violations: tuple[str, ...]
    # Reads outside the trial tree (the primary arm-enforcement gate). A trial with
    # any read escape may have gotten the answer for free, so -- like `violations` --
    # it is VOID: its numbers are discarded from rates/medians and it is named.
    read_escapes: tuple[str, ...]

    @property
    def void(self) -> bool:
        """A trial whose numbers must not be trusted: it used a tool its arm was
        not granted (`violations`) or read outside its own tree (`read_escapes`)."""
        return bool(self.violations or self.read_escapes)


def load_calls(path: str | Path) -> list[ToolCall]:
    """Joined tool calls for one trial session, via the existing Claude parser."""
    session_path = Path(path)
    with session_path.open(encoding="utf-8") as handle:
        _parser, replayed = detect_parser(handle)
        result = ClaudeParser().parse(
            replayed,
            agent="claude-code",
            source="raw",
            # keep_raw_input so arm_violations can read each Bash call's command
            # string -- a gate escape is a fact about the command, not the tool name.
            keep_raw_input=True,
            project=session_path.parent.name,
        )
    return result.calls


def score_trial(
    session_path: str | Path,
    defect: DefectSpec,
    arm: ArmSpec,
    trial: int,
    fixed: bool,
    trial_root: Path,
) -> TrialResult:
    """Score one trial. `fixed` is the oracle's verdict, supplied by the runner.

    `trial_root` is the trial tree's own directory (the runner's `dest`): reads
    resolved outside it void the trial. Passed explicitly rather than inferred from
    parser internals -- it is what the runner already knows and what the read-scope
    audit compares against lexically.
    """
    calls = load_calls(session_path)
    hit = find_located(session_path)
    located = hit is not None and located_correct(hit[1], defect.truth)

    n1: int | None = None
    n2: int | None = None
    if located and hit is not None:
        located_ts = hit[0]
        # ISO-8601 Z timestamps sort lexicographically.
        n1 = sum(call.tokens for call in calls if call.ts < located_ts)
        n2 = sum(call.tokens for call in calls if call.ts >= located_ts)
    # No `elif fixed:` arm. A trial that fixed without locating is a solved fix
    # with no navigation measurement, recorded as such and never back-filled --
    # its cost lands in `total`, which is what it is.

    return TrialResult(
        defect_id=defect.id,
        repo=defect.repo,
        arm=arm.name,
        trial=trial,
        located=located,
        fixed=fixed,
        n1=n1,
        n2=n2,
        total=sum(call.tokens for call in calls),
        steps=len(calls),
        violations=arm_violations(calls, arm),
        read_escapes=read_escapes(calls, trial_root),
    )


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


@dataclass(frozen=True)
class ProfileRow:
    """One (repo, defect, arm) cell of the routing profile."""

    repo: str
    defect_id: str
    arm: str
    trials: int
    locate_rate: float
    fix_rate: float
    median_n1: int | None
    median_n2: int | None
    unsolved: int
    # Trials that reached green without a correct LOCATED: line. Their fix cost is
    # in `total`, not in N2, so they are invisible to `median_n2` -- counted here
    # so the table names them instead of dropping them.
    fixed_unlocated: int
    violations: tuple[str, ...]
    # Trials voided by the arm-enforcement gates (tool/gate `violations` OR
    # read-scope `read_escapes`). Excluded from every rate and median in this row;
    # counted here and named via `read_escapes` so the table shows the hole.
    void: int
    read_escapes: tuple[str, ...]


def _median_or_none(values: list[int]) -> int | None:
    return int(statistics.median(values)) if values else None


def build_profile(results: list[TrialResult]) -> list[ProfileRow]:
    """Aggregate trials into one row per (repo, defect, arm).

    Solve rate and cost are kept SEPARATE and never blended: an arm that never
    finds the bug is cheap, and cost is uninterpretable without conditioning on
    success.
    """
    grouped: dict[tuple[str, str, str], list[TrialResult]] = defaultdict(list)
    for result in results:
        grouped[(result.repo, result.defect_id, result.arm)].append(result)

    rows: list[ProfileRow] = []
    for (repo, defect_id, arm), trials in sorted(grouped.items()):
        # A void trial (tool/gate violation OR read outside its tree) may have gotten
        # the answer for free: its numbers are meaningless, so it is excluded from
        # every rate and median and measured only over the VALID trials. It is still
        # counted (`void`) and named (`read_escapes`) -- visibly incomplete, never
        # quietly wrong. Guard the empty-valid case: an all-void cell has no rate.
        void_trials = [t for t in trials if t.void]
        valid = [t for t in trials if not t.void]
        located = [t for t in valid if t.located]
        fixed = [t for t in valid if t.fixed]
        # N2 is edit cost, which only exists once the trial has located: median it
        # over trials that BOTH located and fixed, never over `fixed` alone. A fix
        # that never located has n2 is None, so folding it in would median it away
        # -- but it would still corrupt the count it was drawn from.
        located_and_fixed = [t for t in valid if t.located and t.fixed]
        rows.append(
            ProfileRow(
                repo=repo,
                defect_id=defect_id,
                arm=arm,
                trials=len(trials),
                locate_rate=len(located) / len(valid) if valid else 0.0,
                fix_rate=len(fixed) / len(valid) if valid else 0.0,
                median_n1=_median_or_none([t.n1 for t in located if t.n1 is not None]),
                median_n2=_median_or_none(
                    [t.n2 for t in located_and_fixed if t.n2 is not None]
                ),
                unsolved=len(valid) - len(fixed),
                fixed_unlocated=sum(1 for t in fixed if not t.located),
                violations=tuple(sorted({v for t in trials for v in t.violations})),
                void=len(void_trials),
                read_escapes=tuple(sorted({e for t in trials for e in t.read_escapes})),
            )
        )
    return rows


def render_profile(rows: list[ProfileRow]) -> str:
    """Markdown routing profile. Unsolved trials and arm violations are named.

    A benchmark that hides its failures is the same defect class as a fully-seeded
    table: visibly incomplete beats quietly wrong.
    """
    lines = [
        "# Complex debug probe — routing profile",
        "",
        "Cost is **context tokens**. Usage/output tokens are not comparable "
        "between arms (TB-17).",
        "",
        "| repo | defect | arm | trials | locate | fix | median N1 | median N2 "
        "| fixed, unlocated | unsolved | void |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        n1 = str(row.median_n1) if row.median_n1 is not None else "—"
        n2 = str(row.median_n2) if row.median_n2 is not None else "—"
        lines.append(
            f"| {row.repo} | {row.defect_id} | {row.arm} | {row.trials} "
            f"| {row.locate_rate:.0%} | {row.fix_rate:.0%} | {n1} | {n2} "
            f"| {row.fixed_unlocated} | {row.unsolved} | {row.void} |"
        )
    lines.append("")
    lines.append(
        "`—` in a cost column = no defined sample for that metric; the cost is "
        "undefined, not zero. It does NOT mean the cell is unsolved -- a "
        "fixed-but-unlocated trial is solved yet renders `—` for N2 (next line)."
    )
    lines.append(
        "`fixed, unlocated` = reached green with no correct `LOCATED:` line, so "
        "the fix has no N2 by construction. Those trials ARE solved; their cost is "
        "real but is not edit cost, and it is not back-filled into median N2."
    )
    lines.append(
        "`void` = trials excluded from every rate and median in the row because "
        "the arm was not enforced (used an ungranted tool, or read outside its own "
        "trial tree). Their numbers may be free; they are named below, never scored."
    )

    if any(row.arm in FULL_SHELL_ARMS for row in rows):
        lines.append("")
        lines.append(
            "> **Read-scope audit is best-effort for full-shell arms.** The "
            f"{', '.join(sorted(FULL_SHELL_ARMS))} arms hold an unrestricted shell, "
            "which can read via indirection (`bash script.sh`, `cat $(locate x)`, a "
            "compiled helper) that no static transcript audit can follow. The audit "
            "flags the escapes it CAN see -- absolute paths and `..` sequences "
            "leaving the tree -- as a tripwire; it does not prove a full-shell arm "
            "stayed in-tree. That is precisely why per-trial filesystem sandboxing "
            "is the deferred stronger option for these arms."
        )

    offenders = [row for row in rows if row.violations]
    if offenders:
        lines.append("")
        lines.append("## VIOLATION — arm restriction was not enforced")
        lines.append("")
        lines.append(
            "These cells used tools their arm was not granted. Their numbers are "
            "**void**: the arm did not measure the toolset it claims to."
        )
        for row in offenders:
            lines.append(
                f"- {row.repo}/{row.defect_id}/{row.arm}: {', '.join(row.violations)}"
            )

    read_offenders = [row for row in rows if row.read_escapes]
    if read_offenders:
        lines.append("")
        lines.append("## VOID — read outside the trial tree")
        lines.append("")
        lines.append(
            "These cells read a path outside their own trial tree, so they may have "
            "gotten the answer for free. Their numbers are **void**: discarded from "
            "the rates and medians above, and named here instead of dropped."
        )
        for row in read_offenders:
            lines.append(
                f"- {row.repo}/{row.defect_id}/{row.arm}: {', '.join(row.read_escapes)}"
            )

    lines.append("")
    # Denominator must match the numerator's population: `unsolved` is counted over
    # VALID (non-void) trials only, so the "of" total is valid trials too, not
    # `trials` (which includes voids). A void is in neither term; the VOID section
    # above carries the void count separately.
    valid_total = sum(r.trials - r.void for r in rows)
    lines.append(f"Unsolved trials: {sum(r.unsolved for r in rows)} of {valid_total}.")
    return "\n".join(lines)
