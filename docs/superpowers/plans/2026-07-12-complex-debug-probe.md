# Complex Debug Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure, per class of debugging defect, which toolset (serena / native Grep+Edit / bash / unrestricted) reaches a verified fix for the fewest context tokens.

**Architecture:** Each trial is one headless `claude -p` session, in a fresh git worktree over a pinned corpus with a defect patch applied, restricted to one arm's toolset via `--allowedTools`. One session per trial means **the session is the cell** — grouping needs only a manifest, not new per-call attribution. Scoring replays the transcript: context tokens before the agent's `LOCATED:` line are navigation cost (N1), tokens after are edit cost (N2), and the repo's own test suite is the fix oracle.

**Tech Stack:** Python 3 stdlib only (project norm, S20), `uv`, existing `toolbench.parsers.ClaudeParser` / `toolbench.transcript.ToolCall`, headless `claude -p`, git worktrees.

## Global Constraints

- **Stdlib runtime only** (S20). No new dependencies. `uv run` for everything.
- **Gate must stay green** (S22): `uv run ruff check .`; `uv run mypy --strict toolbench tests`; `uv run pytest -q`.
- **Never touch `tools/`** — those five files are the active probe's matched targets (S17). A serena or `rg` call against them is structurally an arm. The two benchmarks must not share a corpus.
- **`Agent`/`Task` is banned in every arm.** A subagent inherits a full toolset; a serena-only arm could spawn one, run `rg`, and return the answer. The ban is verified post-hoc from the transcript, never trusted from the flag (this is the TB-29 `--exclude-subagents` no-op failure mode).
- **Cost is context tokens, never output tokens** (TB-17: output tokens are not comparable across arms).
- **Report failures loudly.** Unsolved trials are named, not dropped. Project norm: *visibly incomplete, never quietly wrong.*
- Fixtures must be **pinned to a shape observed in a real transcript**, not to the shape the code expects (`active-probes.md` records three separate bugs from violating this).

## Precheck results (already established — do not re-litigate)

Run 2026-07-12 against live serena. These are facts, not assumptions:

| finding | evidence |
|---|---|
| serena indexes **TypeScript, Python, Vue** | `wids-nyc` activates as `['typescript','vue','python']` |
| serena indexes **Rust** — but only when configured | `maltese-agent` auto-detected as `['typescript']` **only**; `get_symbols_overview` on a `.rs` raised `Cannot extract symbols … Active languages: ['typescript']`. With `languages: [rust, typescript]` it returned `{"Function": ["caesar_decode"], "Module": ["tests"]}`. `rust-analyzer` is installed at `~/.cargo/bin/rust-analyzer`. |
| serena **cannot ever index SQL** | `activate_project` with `sql` raised `Invalid language: sql`. The 60+ valid languages do not include it. **Structural, not a misconfiguration.** |
| test commands | `wids-nyc`: `npx vitest run` (in `web/`). `maltese-agent`: `cargo test` (workspace: `falcon-mcp`, `falcon-agent`). |
| pinned SHAs | `wids-nyc` `a39cdd0`; `maltese-agent` `7b8fa95` |

**Consequence:** vendored corpora MUST ship an explicit `.serena/project.yml`. Serena's auto-detection under-detected a Rust repo as TypeScript-only, and a benchmark run against the auto-detected config would have measured a crippled serena and blamed the tool.

**Consequence:** serena on SQL is not blind — `search_for_pattern` is a plain regex search and works on any file. What dies is its *symbolic* advantage. D5 therefore measures **how much serena's forced text-fallback costs**, not whether it must fall back.

## File Structure

- `corpus/manifest.json` — pinned SHAs, test commands, serena languages per repo.
- `corpus/vendor.sh` — clones both repos at pinned SHA into `corpus/<name>/`, writes `.serena/project.yml`.
- `protocols/complex-probe.md` — the protocol + precheck evidence table above.
- `probes/complex/<Dn>-<slug>/` — one dir per defect: `defect.patch`, `prompt.md`, `truth.json`, `prediction.md`.
- `toolbench/complex.py` — specs, `LOCATED:` parsing, trial scoring, arm audit, report. (Flat module, matching `probe.py` / `passive.py`.)
- `toolbench/complex_runner.py` — worktree + headless-claude + oracle driver, with an injectable runner so it is testable.
- `tests/test_complex.py`, `tests/test_complex_runner.py`
- `tests/fixtures/complex_session_located.jsonl` — real-shaped transcript fixture.

---

### Task 1: Defect and arm specs

**Files:**
- Create: `toolbench/complex.py`
- Test: `tests/test_complex.py`

**Interfaces:**
- Produces: `Truth`, `DefectSpec`, `ArmSpec`, `build_arms(test_gate: str) -> tuple[ArmSpec, ...]`, `BANNED_TOOLS`, `DEFECTS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_complex.py
import unittest

from toolbench.complex import BANNED_TOOLS, DEFECTS, build_arms


class ArmSpecTests(unittest.TestCase):
    def test_every_arm_gets_read_todowrite_and_the_test_gate(self) -> None:
        arms = build_arms("Bash(cargo test:*)")
        for arm in arms:
            self.assertIn("Read", arm.allowed_tools, arm.name)
            self.assertIn("TodoWrite", arm.allowed_tools, arm.name)

    def test_no_arm_may_carry_the_agent_tool(self) -> None:
        # A subagent inherits a full toolset: a serena-only arm could spawn one,
        # run rg inside it, and hand back the answer. The restriction would look
        # enforced and be void.
        for arm in build_arms("Bash(cargo test:*)"):
            for banned in BANNED_TOOLS:
                self.assertNotIn(banned, arm.allowed_tools, f"{arm.name} carries {banned}")

    def test_serena_arm_has_no_search_shell(self) -> None:
        serena = next(a for a in build_arms("Bash(cargo test:*)") if a.name == "serena")
        self.assertNotIn("Bash", serena.allowed_tools)
        self.assertIn("Bash(cargo test:*)", serena.allowed_tools)

    def test_every_defect_declares_a_pre_registered_winner(self) -> None:
        for defect in DEFECTS:
            self.assertIn(defect.predicted_winner, {"serena", "native", "bash", "neutral"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'toolbench.complex'`

- [ ] **Step 3: Write minimal implementation**

```python
# toolbench/complex.py
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

from dataclasses import dataclass

# The agent emits this once, as soon as it believes it has localized the defect.
# Making the moment explicit beats inferring it: N1 is the tokens before it.
LOCATED_PREFIX = "LOCATED:"

# Never grantable to any arm. See module docstring of `arm_violations`.
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
    """Ground truth for one defect, derived from its injection patch."""

    file: str
    symbol: str
    lines: tuple[int, int]


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


def build_arms(test_gate: str) -> tuple[ArmSpec, ...]:
    """The four arms. `test_gate` is a command-scoped Bash rule, e.g.
    `Bash(cargo test:*)`.

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


DEFECTS: tuple[DefectSpec, ...] = (
    DefectSpec(
        id="D1",
        repo="wids",
        language="typescript",
        truth=Truth("web/src/lib/schedule.ts", "formatSlot", (1, 1)),
        predicted_winner="serena",
        rationale="many unrelated types share the method name; rg returns a wall "
        "of false positives, find_referencing_symbols resolves true callers",
    ),
    DefectSpec(
        id="D2",
        repo="wids",
        language="typescript",
        truth=Truth("web/src/lib/rpc.ts", "callRpc", (1, 1)),
        predicted_winner="native",
        rationale="handler resolved by string literal; the LSP reference graph "
        "contains zero edges for a string, grep finds it instantly",
    ),
    DefectSpec(
        id="D5",
        repo="wids",
        language="sql",
        truth=Truth("migrations/003_rls_policies.sql", "check_member", (1, 1)),
        predicted_winner="native",
        rationale="string-keyed call crossing into SQL, which serena CANNOT index "
        "(Invalid language: sql). Its symbolic tools are useless; it is forced "
        "down to search_for_pattern, i.e. a more expensive grep",
    ),
)
```

> **Note for the implementer:** the `Truth` line ranges and file paths above are
> placeholders **only until Task 2 writes the real patches**, which is the step
> that makes them ground truth. Task 2 rewrites this tuple from the patches. Do
> not ship them as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py
git commit -m "feat(complex): arm and defect specs; Agent tool banned in every arm"
```

---

### Task 2: Corpus vendoring with explicit serena languages

**Files:**
- Create: `corpus/manifest.json`, `corpus/vendor.sh`
- Test: `tests/test_complex_corpus.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `corpus/<repo>/` working trees at pinned SHAs, each with `.serena/project.yml`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_complex_corpus.py
import json
import unittest
from pathlib import Path

MANIFEST = Path("corpus/manifest.json")


class CorpusManifestTests(unittest.TestCase):
    def test_manifest_pins_a_sha_and_test_command_per_repo(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name, entry in data.items():
            self.assertRegex(entry["sha"], r"^[0-9a-f]{7,40}$", name)
            self.assertTrue(entry["test_gate"].startswith("Bash("), name)

    def test_serena_languages_are_declared_explicitly_never_autodetected(self) -> None:
        # Serena auto-detected maltese-agent (a Cargo workspace) as typescript-ONLY,
        # and refused to extract a single Rust symbol. A benchmark on the
        # auto-detected config would measure a crippled serena and blame the tool.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("rust", data["maltese"]["serena_languages"])

    def test_sql_is_never_declared_because_serena_cannot_index_it(self) -> None:
        # activate_project with `sql` raises `Invalid language: sql`. Declaring it
        # would make every vendored wids corpus fail to activate.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name, entry in data.items():
            self.assertNotIn("sql", entry["serena_languages"], name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex_corpus.py -q`
Expected: FAIL — `FileNotFoundError: corpus/manifest.json`

- [ ] **Step 3: Write minimal implementation**

```json
// corpus/manifest.json
{
  "wids": {
    "origin": "https://github.com/mrojas54/wids-nyc-reading-group-assistant",
    "sha": "a39cdd0",
    "test_gate": "Bash(npx vitest run:*)",
    "test_cmd": ["npx", "vitest", "run"],
    "test_cwd": "web",
    "serena_languages": ["typescript", "vue", "python"]
  },
  "maltese": {
    "origin": "https://github.com/mrojas54/maltese-agent",
    "sha": "7b8fa95",
    "test_gate": "Bash(cargo test:*)",
    "test_cmd": ["cargo", "test"],
    "test_cwd": ".",
    "serena_languages": ["rust", "typescript"]
  }
}
```

```bash
#!/usr/bin/env bash
# corpus/vendor.sh -- clone both repos at their pinned SHA and write serena config.
#
# The .serena/project.yml is written EXPLICITLY and never left to auto-detection:
# serena detected maltese-agent (a Cargo workspace) as typescript-only and could
# not extract a single Rust symbol. `sql` is deliberately absent -- serena rejects
# it as an invalid language, so declaring it would break activation outright.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import json, subprocess, pathlib
manifest = json.loads(pathlib.Path("manifest.json").read_text())
for name, entry in manifest.items():
    dest = pathlib.Path(name)
    if dest.exists():
        print(f"{name}: present, skipping")
    else:
        subprocess.run(["git", "clone", "-q", entry["origin"], name], check=True)
        subprocess.run(["git", "-C", name, "checkout", "-q", entry["sha"]], check=True)
    serena = dest / ".serena"
    serena.mkdir(exist_ok=True)
    langs = "\n".join(f"- {lang}" for lang in entry["serena_languages"])
    (serena / "project.yml").write_text(
        f'project_name: "{name}"\nlanguages:\n{langs}\n'
        'encoding: "utf-8"\nignore_all_files_in_gitignore: true\n'
        'ls_workspace_folders: ["."]\nread_only: false\nexcluded_tools: []\n'
    )
    print(f"{name}: pinned at {entry['sha']}, languages={entry['serena_languages']}")
PY
```

- [ ] **Step 4: Run and verify**

Run: `chmod +x corpus/vendor.sh && ./corpus/vendor.sh && uv run pytest tests/test_complex_corpus.py -q`
Expected: both repos vendored; 3 tests PASS.

Then verify serena actually indexes Rust in the vendored copy — **this is the arm precondition, and a serena arm that silently text-searches is an invalid arm that still emits a plausible number**:

Run: activate `corpus/maltese` in serena, then `get_symbols_overview` on `falcon-agent/src/decoder.rs`.
Expected: `{"Function": ["caesar_decode"], "Module": ["tests"]}` — real symbols, not an error.

- [ ] **Step 5: Commit**

```bash
git add corpus/manifest.json corpus/vendor.sh tests/test_complex_corpus.py
git commit -m "feat(complex): vendor pinned corpora with explicit serena languages

Serena auto-detected a Cargo workspace as typescript-only and refused to
extract any Rust symbol. The config is written explicitly so the serena arm
is never silently crippled. sql is omitted deliberately: serena rejects it as
an invalid language."
```

---

### Task 3: `LOCATED:` parsing and truth matching

**Files:**
- Modify: `toolbench/complex.py`
- Create: `tests/fixtures/complex_session_located.jsonl`
- Modify: `tests/test_complex.py`

**Interfaces:**
- Produces: `find_located(path) -> tuple[str, dict] | None`, `located_correct(obj, truth) -> bool`.

- [ ] **Step 1: Write the failing test**

The fixture must be pinned to a **real** Claude transcript shape — one assistant
record per content block, `timestamp` at top level. (`active-probes.md`: four
fixtures once pooled every block of a response into one record, a shape the
runtime never emits, and hid a bug for three revisions.)

```json
// tests/fixtures/complex_session_located.jsonl
{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","requestId":"req_1","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Grep","input":{"pattern":"formatSlot"}}]}}
{"type":"user","timestamp":"2026-07-12T10:00:01Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"web/src/lib/schedule.ts:12:formatSlot"}]}}
{"type":"assistant","timestamp":"2026-07-12T10:00:02Z","requestId":"req_2","message":{"role":"assistant","content":[{"type":"text","text":"LOCATED: {\"file\": \"web/src/lib/schedule.ts\", \"symbol\": \"formatSlot\", \"lines\": [12, 20]}"}]}}
{"type":"assistant","timestamp":"2026-07-12T10:00:03Z","requestId":"req_3","message":{"role":"assistant","content":[{"type":"tool_use","id":"t2","name":"Edit","input":{"file_path":"web/src/lib/schedule.ts"}}]}}
{"type":"user","timestamp":"2026-07-12T10:00:04Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t2","content":"ok"}]}}
```

```python
# append to tests/test_complex.py
from toolbench.complex import Truth, find_located, located_correct

FIXTURE = "tests/fixtures/complex_session_located.jsonl"


class LocatedTests(unittest.TestCase):
    def test_finds_the_located_line_and_its_timestamp(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        ts, obj = hit
        self.assertEqual(ts, "2026-07-12T10:00:02Z")
        self.assertEqual(obj["symbol"], "formatSlot")

    def test_correct_when_file_and_symbol_match_and_lines_overlap(self) -> None:
        truth = Truth("web/src/lib/schedule.ts", "formatSlot", (15, 18))
        _, obj = find_located(FIXTURE)  # type: ignore[misc]
        self.assertTrue(located_correct(obj, truth))

    def test_wrong_file_is_not_a_hit_even_with_the_right_symbol(self) -> None:
        truth = Truth("web/src/lib/other.ts", "formatSlot", (15, 18))
        _, obj = find_located(FIXTURE)  # type: ignore[misc]
        self.assertFalse(located_correct(obj, truth))

    def test_disjoint_line_ranges_are_not_a_hit(self) -> None:
        truth = Truth("web/src/lib/schedule.ts", "formatSlot", (90, 99))
        _, obj = find_located(FIXTURE)  # type: ignore[misc]
        self.assertFalse(located_correct(obj, truth))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex.py -q`
Expected: FAIL — `ImportError: cannot import name 'find_located'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to toolbench/complex.py
import json
from pathlib import Path


def find_located(path: str | Path) -> tuple[str, dict[str, object]] | None:
    """The first assistant text block emitting `LOCATED: {...}`, with its timestamp.

    Returns `None` when the agent never claimed a localization -- which is a real
    outcome (it may still have guessed its way to a passing test) and is recorded
    as such, never back-filled.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text") or ""
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


def located_correct(obj: dict[str, object], truth: Truth) -> bool:
    """File and symbol must match exactly; line ranges need only overlap.

    Exact line equality would be brittle -- an agent that reports the whole
    function body while the patch touched one line inside it has still localized
    the defect correctly.
    """
    if obj.get("file") != truth.file or obj.get("symbol") != truth.symbol:
        return False
    lines = obj.get("lines")
    if not isinstance(lines, list) or len(lines) != 2:
        return False
    low, high = lines
    if not isinstance(low, int) or not isinstance(high, int):
        return False
    return not (high < truth.lines[0] or low > truth.lines[1])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py tests/fixtures/complex_session_located.jsonl
git commit -m "feat(complex): parse the LOCATED: marker and match it against patch ground truth"
```

---

### Task 4: Trial scoring — N1, N2, and the arm audit

**Files:**
- Modify: `toolbench/complex.py`, `tests/test_complex.py`

**Interfaces:**
- Consumes: `find_located`, `located_correct`, `ArmSpec`, `DefectSpec` (Tasks 1, 3).
- Produces: `TrialResult`, `load_calls(path) -> list[ToolCall]`, `arm_violations(calls, arm) -> tuple[str, ...]`, `score_trial(session_path, defect, arm, trial, fixed) -> TrialResult`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_complex.py
from toolbench.complex import ArmSpec, arm_violations, load_calls, score_trial


class TrialScoringTests(unittest.TestCase):
    def test_n1_counts_only_calls_before_the_located_line(self) -> None:
        defect = DEFECTS[0]
        arm = next(a for a in build_arms("Bash(npx vitest run:*)") if a.name == "native")
        truth_defect = replace_truth(defect, Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20)))
        result = score_trial(FIXTURE, truth_defect, arm, trial=1, fixed=True)
        self.assertTrue(result.located)
        # Grep's tool_result is 34 chars -> 8 tokens; Edit's "ok" -> 0.
        self.assertEqual(result.n1, 8)
        self.assertEqual(result.n2, 0)

    def test_unlocated_but_fixed_records_no_navigation_number(self) -> None:
        # Guessing its way to green is a real outcome and must stay visible.
        defect = replace_truth(DEFECTS[0], Truth("nope.ts", "nope", (1, 2)))
        arm = next(a for a in build_arms("Bash(npx vitest run:*)") if a.name == "native")
        result = score_trial(FIXTURE, defect, arm, trial=1, fixed=True)
        self.assertFalse(result.located)
        self.assertIsNone(result.n1)
        self.assertTrue(result.fixed)

    def test_a_call_outside_the_arm_is_a_violation(self) -> None:
        serena = next(a for a in build_arms("Bash(npx vitest run:*)") if a.name == "serena")
        calls = load_calls(FIXTURE)  # fixture uses Grep + Edit
        self.assertEqual(arm_violations(calls, serena), ("Edit", "Grep"))

    def test_the_agent_tool_is_always_a_violation(self) -> None:
        # The ban must be verified from the transcript, never trusted from the flag.
        control = next(a for a in build_arms("Bash(npx vitest run:*)") if a.name == "control")
        calls = load_calls("tests/fixtures/complex_session_agent_escape.jsonl")
        self.assertIn("Task", arm_violations(calls, control))


def replace_truth(defect, truth):
    from dataclasses import replace
    return replace(defect, truth=truth)
```

Also create `tests/fixtures/complex_session_agent_escape.jsonl` — one assistant record with a `Task` tool_use, same real shape as above:

```json
{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","requestId":"req_1","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Task","input":{"prompt":"grep for formatSlot"}}]}}
{"type":"user","timestamp":"2026-07-12T10:00:01Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"found it"}]}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex.py -q`
Expected: FAIL — `ImportError: cannot import name 'score_trial'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to toolbench/complex.py
from toolbench.adapters import detect_parser
from toolbench.parsers import ClaudeParser
from toolbench.transcript import ToolCall


@dataclass(frozen=True)
class TrialResult:
    """One (defect, arm, trial) cell.

    `n1` is navigation cost, `n2` edit cost. Either may be None: an arm that never
    localized has no navigation number, and one that never fixed has no edit
    number. They are never back-filled -- an arm that fails is cheap, and its
    cheapness means nothing.
    """

    defect_id: str
    repo: str
    arm: str
    trial: int
    located: bool
    fixed: bool
    n1: int | None
    n2: int | None
    steps: int
    violations: tuple[str, ...]


def load_calls(path: str | Path) -> list[ToolCall]:
    """Joined tool calls for one trial session, via the existing Claude parser."""
    session_path = Path(path)
    with session_path.open(encoding="utf-8") as handle:
        parser, replayed = detect_parser(handle)
        del parser
        result = ClaudeParser().parse(
            replayed,
            agent="claude-code",
            source="raw",
            project=session_path.parent.name,
        )
    return result.calls


def arm_violations(calls: list[ToolCall], arm: ArmSpec) -> tuple[str, ...]:
    """Tool names the arm used but was not granted -- plus any banned tool, always.

    The arm restriction is verified from the transcript, never trusted from the
    `--allowedTools` flag. A flag that silently fails to restrict is the TB-29
    `--exclude-subagents` no-op: the suite ratified it while it did nothing.
    """
    granted = {name for name in arm.allowed_tools if not name.startswith("Bash(")}
    if any(name.startswith("Bash(") for name in arm.allowed_tools):
        granted.add("Bash")
    used = {call.name for call in calls}
    return tuple(sorted((used - granted) | (used & set(BANNED_TOOLS))))


def score_trial(
    session_path: str | Path,
    defect: DefectSpec,
    arm: ArmSpec,
    trial: int,
    fixed: bool,
) -> TrialResult:
    """Score one trial. `fixed` is the oracle's verdict, supplied by the runner."""
    calls = load_calls(session_path)
    hit = find_located(session_path)
    located = hit is not None and located_correct(hit[1], defect.truth)

    n1: int | None = None
    n2: int | None = None
    if located:
        assert hit is not None
        located_ts = hit[0]
        # ISO-8601 Z timestamps sort lexicographically.
        n1 = sum(call.tokens for call in calls if call.ts < located_ts)
        n2 = sum(call.tokens for call in calls if call.ts >= located_ts)
    elif fixed:
        # Solved without ever claiming a localization. Real outcome, no N1.
        n2 = sum(call.tokens for call in calls)

    return TrialResult(
        defect_id=defect.id,
        repo=defect.repo,
        arm=arm.name,
        trial=trial,
        located=located,
        fixed=fixed,
        n1=n1,
        n2=n2,
        steps=len(calls),
        violations=arm_violations(calls, arm),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex.py -q`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py tests/fixtures/complex_session_agent_escape.jsonl
git commit -m "feat(complex): score N1/N2 per trial and audit arm restrictions from the transcript"
```

---

### Task 5: Profile report — solve rate and cost-among-solves, never blended

**Files:**
- Modify: `toolbench/complex.py`, `tests/test_complex.py`

**Interfaces:**
- Consumes: `TrialResult` (Task 4).
- Produces: `ProfileRow`, `build_profile(results) -> list[ProfileRow]`, `render_profile(rows) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_complex.py
from toolbench.complex import TrialResult, build_profile, render_profile


def _trial(arm, located, fixed, n1, n2, violations=()):
    return TrialResult("D1", "wids", arm, 1, located, fixed, n1, n2, 3, violations)


class ProfileTests(unittest.TestCase):
    def test_median_cost_counts_only_solved_trials(self) -> None:
        results = [
            _trial("serena", True, True, 100, 10),
            _trial("serena", True, True, 300, 10),
            _trial("serena", False, False, None, None),  # must not drag the median
        ]
        row = next(r for r in build_profile(results) if r.arm == "serena")
        self.assertEqual(row.median_n1, 200)
        self.assertEqual(row.locate_rate, 2 / 3)

    def test_an_arm_that_never_solves_reports_no_cost_at_all(self) -> None:
        # Its cheapness is meaningless; a number here would be a lie.
        rows = build_profile([_trial("bash", False, False, None, None)])
        self.assertIsNone(rows[0].median_n1)

    def test_unsolved_trials_are_named_in_the_report_not_dropped(self) -> None:
        text = render_profile(build_profile([_trial("bash", False, False, None, None)]))
        self.assertIn("unsolved", text.lower())

    def test_a_violation_is_shouted_because_it_voids_the_arm(self) -> None:
        text = render_profile(build_profile([_trial("serena", True, True, 5, 5, ("Task",))]))
        self.assertIn("VIOLATION", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_profile'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to toolbench/complex.py
import statistics
from collections import defaultdict


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
    violations: tuple[str, ...]


def _median_or_none(values: list[int]) -> int | None:
    return int(statistics.median(values)) if values else None


def build_profile(results: list[TrialResult]) -> list[ProfileRow]:
    """Aggregate trials into one row per (repo, defect, arm).

    Solve rate and cost are kept as SEPARATE numbers and never blended. An arm
    that never finds the bug is cheap, and cost is uninterpretable without
    conditioning on success.
    """
    grouped: dict[tuple[str, str, str], list[TrialResult]] = defaultdict(list)
    for result in results:
        grouped[(result.repo, result.defect_id, result.arm)].append(result)

    rows: list[ProfileRow] = []
    for (repo, defect_id, arm), trials in sorted(grouped.items()):
        located = [t for t in trials if t.located]
        fixed = [t for t in trials if t.fixed]
        violations = tuple(sorted({v for t in trials for v in t.violations}))
        rows.append(
            ProfileRow(
                repo=repo,
                defect_id=defect_id,
                arm=arm,
                trials=len(trials),
                locate_rate=len(located) / len(trials),
                fix_rate=len(fixed) / len(trials),
                median_n1=_median_or_none([t.n1 for t in located if t.n1 is not None]),
                median_n2=_median_or_none([t.n2 for t in fixed if t.n2 is not None]),
                unsolved=len(trials) - len(fixed),
                violations=violations,
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
        "| repo | defect | arm | trials | locate | fix | median N1 | median N2 | unsolved |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        n1 = str(row.median_n1) if row.median_n1 is not None else "—"
        n2 = str(row.median_n2) if row.median_n2 is not None else "—"
        lines.append(
            f"| {row.repo} | {row.defect_id} | {row.arm} | {row.trials} "
            f"| {row.locate_rate:.0%} | {row.fix_rate:.0%} | {n1} | {n2} "
            f"| {row.unsolved} |"
        )
    lines.append("")
    lines.append("`—` = no solved trial in that cell; cost is undefined, not zero.")

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
            used = ", ".join(row.violations)
            lines.append(f"- {row.repo}/{row.defect_id}/{row.arm}: {used}")

    total_unsolved = sum(row.unsolved for row in rows)
    lines.append("")
    lines.append(f"Unsolved trials: {total_unsolved} of {sum(r.trials for r in rows)}.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex.py -q`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py
git commit -m "feat(complex): routing-profile report; solve rate and cost never blended"
```

---

### Task 6: The trial runner

**Files:**
- Create: `toolbench/complex_runner.py`, `tests/test_complex_runner.py`

**Interfaces:**
- Consumes: `ArmSpec`, `DefectSpec`, `score_trial` (Tasks 1, 4).
- Produces: `run_trial(defect, arm, trial, workdir, launch, oracle) -> TrialResult` where `launch` and `oracle` are injectable callables (so tests never shell out to a real `claude`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_complex_runner.py
import unittest
from pathlib import Path

from toolbench.complex import DEFECTS, build_arms
from toolbench.complex_runner import build_claude_argv, run_trial

FIXTURE = Path("tests/fixtures/complex_session_located.jsonl")


class ClaudeArgvTests(unittest.TestCase):
    def test_allowed_tools_are_passed_and_agent_is_never_among_them(self) -> None:
        arm = next(a for a in build_arms("Bash(cargo test:*)") if a.name == "serena")
        argv = build_claude_argv("find the bug", arm, cwd=Path("/tmp/wt"))
        joined = " ".join(argv)
        self.assertIn("--allowedTools", argv)
        self.assertIn("mcp__plugin_serena_serena__find_symbol", joined)
        self.assertNotIn("Task", joined)
        self.assertNotIn("Agent", joined)

    def test_disallowed_tools_are_belt_and_braces(self) -> None:
        # --allowedTools alone is an allowlist; --disallowedTools states the ban
        # explicitly so a future permissive default cannot quietly reopen it.
        arm = next(a for a in build_arms("Bash(cargo test:*)") if a.name == "control")
        argv = build_claude_argv("find the bug", arm, cwd=Path("/tmp/wt"))
        self.assertIn("--disallowedTools", argv)


class RunTrialTests(unittest.TestCase):
    def test_oracle_verdict_flows_into_the_scored_result(self) -> None:
        arm = next(a for a in build_arms("Bash(npx vitest run:*)") if a.name == "native")
        calls: list[str] = []

        def fake_launch(argv, cwd):
            calls.append("launched")
            return FIXTURE

        def fake_oracle(cwd):
            return False  # tests still red

        result = run_trial(DEFECTS[0], arm, 1, Path("/tmp/wt"), fake_launch, fake_oracle)
        self.assertEqual(calls, ["launched"])
        self.assertFalse(result.fixed)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'toolbench.complex_runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# toolbench/complex_runner.py
"""Drive one trial: worktree -> headless claude -> oracle -> scored result.

`launch` and `oracle` are injected so the suite never shells out to a real
`claude` binary (the project's fake-runner pattern, S24).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from toolbench.complex import BANNED_TOOLS, ArmSpec, DefectSpec, TrialResult, score_trial

Launch = Callable[[list[str], Path], Path]
Oracle = Callable[[Path], bool]


def build_claude_argv(prompt: str, arm: ArmSpec, cwd: Path) -> list[str]:
    """Headless invocation for one arm.

    `--disallowedTools` is belt-and-braces beside the allowlist: it states the
    Agent ban explicitly, so a future permissive default cannot quietly reopen the
    subagent escape hatch. The ban is still verified post-hoc from the transcript
    (`arm_violations`) -- a flag is a claim, not evidence.
    """
    return [
        "claude",
        "-p",
        prompt,
        "--allowedTools",
        ",".join(arm.allowed_tools),
        "--disallowedTools",
        ",".join(BANNED_TOOLS),
        "--add-dir",
        str(cwd),
    ]


def run_trial(
    defect: DefectSpec,
    arm: ArmSpec,
    trial: int,
    workdir: Path,
    launch: Launch,
    oracle: Oracle,
) -> TrialResult:
    """Run one cell and score it. Returns the scored `TrialResult`."""
    prompt = (workdir / "PROMPT.md").read_text(encoding="utf-8") if (
        workdir / "PROMPT.md"
    ).exists() else defect.rationale
    session_path = launch(build_claude_argv(prompt, arm, workdir), workdir)
    fixed = oracle(workdir)
    return score_trial(session_path, defect, arm, trial, fixed)


def shell_oracle(test_cmd: list[str], test_cwd: str) -> Oracle:
    """Real oracle: the corpus repo's own test suite must exit 0."""

    def _oracle(workdir: Path) -> bool:
        proc = subprocess.run(
            test_cmd, cwd=workdir / test_cwd, capture_output=True, check=False
        )
        return proc.returncode == 0

    return _oracle
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex_runner.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex_runner.py tests/test_complex_runner.py
git commit -m "feat(complex): trial runner with injectable launch + oracle"
```

---

### Task 7: Write the defect patches and re-derive ground truth

**Files:**
- Create: `probes/complex/D1-name-collision/{defect.patch,prompt.md,truth.json,prediction.md}` (and D2, D5; then the maltese twins)
- Modify: `toolbench/complex.py` (`DEFECTS` rewritten from the real patches)

- [ ] **Step 1: Find a real precondition for each defect — do not invent one**

For each defect class, locate an **existing** seam in the vendored corpus:

- **D1** — a method/function name shared by several unrelated types. Verify with:
  `rg -n 'formatSlot|render\(' corpus/wids/web/src --type ts | wc -l` — you need a name where `rg` returns many hits and only one is the true caller.
- **D2/D5** — a string-keyed call: `rg -n "\.rpc\('" corpus/wids/web/src` — each hit names a Postgres function defined in `corpus/wids/migrations/*.sql`.

**A defect must be a seam the corpus already had.** One designed to prove the
prediction is the "confirmation, not verification" trap `active-probes.md`
records three times. If a class has no real precondition in a repo, **say so and
drop that cell** — do not manufacture one.

- [ ] **Step 2: Write `prediction.md` and commit it BEFORE any trial runs**

```markdown
# D5 — prediction (pre-registered)

Predicted winner: **native / bash**

Serena cannot index SQL at all (`activate_project` → `Invalid language: sql`).
Its symbolic tools are structurally useless here; it is forced down to
`search_for_pattern`, i.e. a more expensive grep. The reference is *also* a
string literal, which an LSP reference graph cannot represent even in an indexed
language.

Falsified if: serena reaches a correct LOCATED in fewer context tokens than
native on this defect.
```

- [ ] **Step 3: Derive `truth.json` from the patch, never by hand**

```bash
git -C corpus/wids apply --stat ../../probes/complex/D5-sql-rename/defect.patch
```

Record the touched file and line range into `truth.json`, then rewrite the
`DEFECTS` tuple in `toolbench/complex.py` from those files.

- [ ] **Step 4: Verify the oracle is red before the fix and green after**

Run: apply the patch in a scratch worktree, run the repo's test command.
Expected: **FAIL** with the patch applied. Revert; expected **PASS**.
A defect whose test suite is green with the bug applied has no oracle and is not a defect.

- [ ] **Step 5: Commit (predictions in their own commit, before results exist)**

```bash
git add probes/complex/
git commit -m "feat(complex): defect fixtures with pre-registered predictions

Predictions are committed before any trial runs. Git history is the
pre-registration ledger: a prediction cannot be retrofitted to a result."
```

---

### Task 8: Pilot run (operator-driven)

- [ ] **Step 1: Confirm the serena arm precondition per (repo, language)**

For each vendored repo, activate it in serena and call `get_symbols_overview` on
one file of each language. Expect real symbols. **A serena arm in a language
serena cannot index is not a serena arm** — it is a text search wearing serena's
name, and it will still emit a plausible number. Disqualify the cell; do not score it.

Expected: `corpus/maltese` returns Rust symbols; `corpus/wids` returns TS symbols;
SQL returns an error — **which is the D5 finding, not a bug.**

- [ ] **Step 2: Pre-warm serena's LSP index outside the measured window**

A fresh worktree per trial would otherwise charge serena a cold-index tax on every
trial that a real user pays once — which could single-handedly manufacture a
serena loss.

- [ ] **Step 3: Run the pilot — 1 trial per cell**

Matrix: 2 repos × (defects with a real precondition in that repo) × 4 arms.

**The pilot proves the harness, not the answer.** Path variance means N=1 is never
an answer. Success criterion is: isolation held (no cross-arm leakage), the oracle
fired, tokens were attributable, and `render_profile` reports **zero VIOLATION
lines**.

- [ ] **Step 4: Read the variance, then choose trial depth**

Set the real run's trial count from the pilot's observed spread — do not guess it now.

- [ ] **Step 5: Commit the report**

```bash
git add reports/2026-07-12-complex-probe-pilot.md
git commit -m "docs(complex): pilot results — harness validation, not an answer"
```

---

## Self-review

**Spec coverage:** corpus §1 → Task 2; defects + pre-registration §2 → Tasks 1, 7;
arms + Agent ban §3 → Tasks 1, 4, 6; arm precondition check §4 → Tasks 2, 8;
isolation §5 → Tasks 6, 8; metrics N1/N2 §6 → Tasks 3, 4; failure handling §7 →
Task 5; run size → Task 8. No spec section is unimplemented.

**Placeholder scan:** the `DEFECTS` tuple in Task 1 carries provisional `Truth`
values. This is flagged inline and Task 7 Step 3 rewrites them from the real
patches. It is a deliberate two-phase construction (specs must exist before
patches can be scored against them), not an unfilled TODO.

**Type consistency:** `Truth`, `DefectSpec`, `ArmSpec`, `TrialResult`, `ProfileRow`
are defined once in `toolbench/complex.py` and consumed with the same names and
field types in `complex_runner.py` and both test modules. `score_trial`'s
signature is identical at definition (Task 4) and call site (Task 6).

**Known risk carried forward:** D3 (call chain) and D4 (moved module) are named in
the spec but have no task, because Task 7 Step 1 may find they have no genuine
precondition in one or both repos. They are added by repeating Task 7 per defect
once a real seam is confirmed. Manufacturing a seam to fill the matrix is
explicitly forbidden.
