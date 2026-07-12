# Complex Debug Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure, per class of debugging defect, which toolset (serena / native Grep+Edit / bash / unrestricted) reaches a verified fix for the fewest context tokens.

**Architecture:** Each trial is one headless `claude -p` session, in a fresh git worktree over a pinned corpus with a defect patch applied, restricted to one arm's toolset via `--allowedTools`. One session per trial means **the session is the cell** — grouping needs only a manifest, not new per-call attribution. Scoring replays the transcript: context tokens before the agent's `LOCATED:` line are navigation cost (N1), tokens after are edit cost (N2), and the repo's own test suite is the fix oracle.

**Tech Stack:** Python 3 stdlib only (project norm, S20), `uv`, existing `toolbench.parsers.ClaudeParser` / `toolbench.transcript.ToolCall`, headless `claude -p`, git worktrees.

## Global Constraints

- **Stdlib runtime only** (S20). No new dependencies. `uv run` for everything.
- **Gate must stay green** (S22): `uv run ruff check .`; `uv run mypy --strict toolbench tests`; `uv run pytest -q`.
- **The fast suite is hermetic** (S22/`test`): no network, no clones, no `~/.claude` access. Tests may read `corpus/manifest.json` (committed) but must never require a vendored clone to exist.
- **Never touch `tools/`** — those five files are the active probe's matched targets (S17). A serena or `rg` call against them is structurally an arm. The two benchmarks must not share a corpus.
- **`Agent`/`Task` is banned in every arm.** A subagent inherits a full toolset; a serena-only arm could spawn one, run `rg`, and return the answer. The ban is verified post-hoc from the transcript, never trusted from the flag (this is the TB-29 `--exclude-subagents` no-op failure mode).
- **Cost is context tokens, never output tokens** (TB-17: output tokens are not comparable across arms).
- **No fabricated data.** Every `Truth` value is derived from a real injection patch. A defect must be a seam the corpus **already had** — manufacturing one to fill the matrix is forbidden.
- **Report failures loudly.** Unsolved trials are named, not dropped. Project norm: *visibly incomplete, never quietly wrong.*
- Fixtures must be **pinned to a shape observed in a real transcript**, not to the shape the code expects (`active-probes.md` records three separate bugs from violating this).

## Precheck results (established 2026-07-12 — facts, do not re-litigate)

| finding | evidence |
|---|---|
| serena indexes **TypeScript, Python, Vue** | `wids-nyc` activates as `['typescript','vue','python']` |
| serena indexes **Rust** — but only when configured | `maltese-agent` auto-detected as `['typescript']` **only**; `get_symbols_overview` on a `.rs` raised `Cannot extract symbols … Active languages: ['typescript']`. With `languages: [rust, typescript]` it returned `{"Function": ["caesar_decode"], "Module": ["tests"]}`. `rust-analyzer` is at `~/.cargo/bin/rust-analyzer`. |
| serena **cannot ever index SQL** | `activate_project` with `sql` raised `Invalid language: sql`. The 60+ valid languages do not include it. **Structural, not a misconfiguration.** |
| test commands | `wids-nyc`: `npx vitest run` (cwd `web/`). `maltese-agent`: `cargo test` (workspace: `falcon-mcp`, `falcon-agent`). |
| pinned SHAs | `wids-nyc` `a39cdd0`; `maltese-agent` `7b8fa95` |

**Consequence:** vendored corpora MUST ship an explicit `.serena/project.yml`. A benchmark on the auto-detected config would have measured a crippled serena and blamed the tool.

**Consequence:** serena on SQL is not blind — `search_for_pattern` is a plain regex search and works on any file. What dies is its *symbolic* advantage. D5 measures **how much serena's forced text-fallback costs**, not whether it must fall back.

## File Structure

- `corpus/manifest.json` — pinned SHAs, test commands, serena languages. **Committed.**
- `corpus/vendor.sh` — clones both repos at pinned SHA, writes `.serena/project.yml`. **Committed.**
- `corpus/<repo>/` — the clones. **Gitignored.** Pinned SHA + vendor script is what makes this reproducible from a clean checkout; committing two upstream repos is not.
- `probes/complex/<Dn>-<slug>/` — `defect.patch`, `prompt.md`, `truth.json`, `prediction.md`.
- `toolbench/complex.py` — specs, `LOCATED:` parsing, scoring, arm audit, report. Flat module, matching `probe.py`.
- `toolbench/complex_runner.py` — worktree + headless-claude + oracle driver, injectable.
- `tests/test_complex.py`, `tests/test_complex_corpus.py`, `tests/test_complex_runner.py`
- `tests/fixtures/complex_session_located.jsonl`, `tests/fixtures/complex_session_agent_escape.jsonl`

---

### Task 1: Arm specs and the Agent ban

**Files:**
- Create: `toolbench/complex.py`
- Test: `tests/test_complex.py`

**Interfaces:**
- Produces: `ArmSpec`, `Truth`, `DefectSpec`, `build_arms(test_gate: str) -> tuple[ArmSpec, ...]`, `BANNED_TOOLS`, `LOCATED_PREFIX`.
- **Does NOT produce `DEFECTS`.** That tuple is built in Task 3 from real patches. Writing it now would mean inventing file paths and line numbers, which the Global Constraints forbid.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_complex.py
import unittest

from toolbench.complex import BANNED_TOOLS, build_arms


class ArmSpecTests(unittest.TestCase):
    def test_every_arm_gets_read_todowrite_and_the_test_gate(self) -> None:
        for arm in build_arms("Bash(cargo test:*)"):
            self.assertIn("Read", arm.allowed_tools, arm.name)
            self.assertIn("TodoWrite", arm.allowed_tools, arm.name)

    def test_no_arm_may_carry_the_agent_tool(self) -> None:
        # A subagent inherits a full toolset: a serena-only arm could spawn one,
        # run rg inside it, and hand back the answer. The restriction would look
        # enforced and be void.
        for arm in build_arms("Bash(cargo test:*)"):
            for banned in BANNED_TOOLS:
                self.assertNotIn(banned, arm.allowed_tools, f"{arm.name} carries {banned}")

    def test_serena_arm_has_no_search_shell_only_the_test_gate(self) -> None:
        serena = next(a for a in build_arms("Bash(cargo test:*)") if a.name == "serena")
        self.assertNotIn("Bash", serena.allowed_tools)
        self.assertIn("Bash(cargo test:*)", serena.allowed_tools)

    def test_all_four_arms_are_built(self) -> None:
        names = {a.name for a in build_arms("Bash(cargo test:*)")}
        self.assertEqual(names, {"serena", "native", "bash", "control"})
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py
git commit -m "feat(complex): arm specs; Agent tool banned from every arm"
```

---

### Task 2: Corpus vendoring with explicit serena languages

**Files:**
- Create: `corpus/manifest.json`, `corpus/vendor.sh`
- Modify: `.gitignore`
- Test: `tests/test_complex_corpus.py`

**Interfaces:**
- Produces: `corpus/<repo>/` working trees at pinned SHAs, each with `.serena/project.yml`. Consumed by Task 3.

- [ ] **Step 1: Write the failing test**

The test reads only `corpus/manifest.json`, which is committed. It must **not**
require a clone to exist — the fast suite is hermetic.

```python
# tests/test_complex_corpus.py
import json
import unittest
from pathlib import Path

MANIFEST = Path("corpus/manifest.json")


class CorpusManifestTests(unittest.TestCase):
    def test_manifest_pins_a_sha_and_test_gate_per_repo(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"wids", "maltese"})
        for name, entry in data.items():
            self.assertRegex(entry["sha"], r"^[0-9a-f]{7,40}$", name)
            self.assertTrue(entry["test_gate"].startswith("Bash("), name)

    def test_rust_is_declared_because_serena_autodetect_missed_it(self) -> None:
        # Serena auto-detected maltese-agent (a Cargo workspace) as typescript-ONLY
        # and refused to extract a single Rust symbol. A benchmark on the
        # auto-detected config measures a crippled serena and blames the tool.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("rust", data["maltese"]["serena_languages"])

    def test_sql_is_never_declared_because_serena_rejects_it(self) -> None:
        # activate_project with `sql` raises `Invalid language: sql`. Declaring it
        # would make the vendored corpus fail to activate at all.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name, entry in data.items():
            self.assertNotIn("sql", entry["serena_languages"], name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_complex_corpus.py -q`
Expected: FAIL — `FileNotFoundError: corpus/manifest.json`

- [ ] **Step 3: Write the manifest, the vendor script, and the gitignore rule**

```json
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

Append to `.gitignore` — the clones are reproducible from the pinned SHA, so
vendoring them into git buys nothing and costs thousands of upstream files:

```gitignore
# Complex-probe corpora: reproduce with corpus/vendor.sh (pinned SHAs in manifest.json)
corpus/*/
```

```bash
#!/usr/bin/env bash
# corpus/vendor.sh -- clone both repos at their pinned SHA and write serena config.
#
# .serena/project.yml is written EXPLICITLY and never left to auto-detection:
# serena detected maltese-agent (a Cargo workspace) as typescript-only and could
# not extract a single Rust symbol. `sql` is deliberately absent -- serena rejects
# it as an invalid language, so declaring it would break activation outright.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import json, pathlib, subprocess

manifest = json.loads(pathlib.Path("manifest.json").read_text())
for name, entry in manifest.items():
    dest = pathlib.Path(name)
    if dest.exists():
        print(f"{name}: present, skipping clone")
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

- [ ] **Step 4: Vendor, then verify the tests pass and nothing untracked leaked**

Run: `chmod +x corpus/vendor.sh && ./corpus/vendor.sh && uv run pytest tests/test_complex_corpus.py -q && git status --short corpus/`
Expected: 3 tests PASS; `git status` shows **only** `corpus/manifest.json` and `corpus/vendor.sh` — no clone contents.

- [ ] **Step 5: Commit**

```bash
git add corpus/manifest.json corpus/vendor.sh .gitignore tests/test_complex_corpus.py
git commit -m "feat(complex): vendor pinned corpora with explicit serena languages

Serena auto-detected a Cargo workspace as typescript-only and refused to
extract any Rust symbol, so the config is written explicitly rather than
detected. sql is omitted deliberately: serena rejects it as an invalid
language. The clones are gitignored -- the pinned SHA is what makes this
reproducible."
```

---

### Task 3: Discover real defects, write patches, derive ground truth

**Files:**
- Create: `probes/complex/<Dn>-<slug>/{defect.patch,prompt.md,truth.json,prediction.md}`
- Modify: `toolbench/complex.py` (add the real `DEFECTS` tuple), `tests/test_complex.py`

**Interfaces:**
- Consumes: `DefectSpec`, `Truth` (Task 1); vendored `corpus/` (Task 2).
- Produces: `DEFECTS: tuple[DefectSpec, ...]` — every value derived from a real patch.

**This task exists before the scoring tasks so that no fabricated `Truth` value is
ever committed.** A defect must be a seam the corpus already had.

- [ ] **Step 1: Find a real precondition for each defect class — do not invent one**

Search the vendored corpus for each seam. Report what you find; a class with **no
real precondition in a repo is dropped for that repo, and you say so.**

```bash
# D1 -- a symbol name shared by many unrelated types (rg drowns, LSP resolves)
rg -n --type ts -o '\b(\w+)\(' corpus/wids/web/src | sort | uniq -c | sort -rn | head -20

# D2 / D5 -- string-keyed calls crossing into SQL (LSP has no edge for a string)
rg -n "\.rpc\(['\"]" corpus/wids/web/src
rg -n 'create (or replace )?function' corpus/wids/migrations

# maltese twins: MCP tool registry keyed by string; cross-crate call chains
rg -n 'tool_name|match .*=> *"' corpus/maltese/falcon-mcp/src
```

A defect designed to prove its own prediction is the "confirmation, not
verification" trap `active-probes.md` records three times. **Only ship defects
whose seam you actually found.** Report the found/not-found list before writing patches.

- [ ] **Step 2: For each real defect, write `prediction.md` — and commit it before any trial**

Git history is the pre-registration ledger: a prediction cannot be retrofitted to
a result it has already seen.

```markdown
# D5 — prediction (pre-registered)

Predicted winner: **native / bash**

Serena cannot index SQL at all (`activate_project` → `Invalid language: sql`), so
its symbolic tools are structurally useless here and it is forced down to
`search_for_pattern` — a more expensive grep. The reference is *also* a string
literal, which an LSP reference graph cannot represent even in an indexed language.

Falsified if: serena reaches a correct LOCATED in fewer context tokens than native.
```

- [ ] **Step 3: Write `defect.patch` and `prompt.md`**

`prompt.md` must read like a real bug report from a colleague. It names no tool,
hints at no mechanism, and never says where the bug is. It must instruct:

> When you believe you have found the cause, emit exactly one line:
> `LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}`
> Then fix it and make the test suite pass.

- [ ] **Step 4: Derive `truth.json` from the patch — never by hand**

```bash
git -C corpus/wids apply --stat ../../probes/complex/D5-sql-rename/defect.patch
```

Record the touched file, symbol, and line range into `truth.json`. Then write the
`DEFECTS` tuple in `toolbench/complex.py` from those files, and add a test that
every shipped defect's `truth.json` matches its `DEFECTS` entry.

- [ ] **Step 5: Verify each oracle is RED with the patch and GREEN without it**

Apply the patch in a scratch worktree; run the repo's test command.
Expected: **FAIL** with the patch applied; **PASS** reverted.
**A defect whose suite is green with the bug applied has no oracle and is not a defect.** Drop it.

- [ ] **Step 6: Commit (predictions in their own commit, before any result exists)**

```bash
git add probes/complex/ toolbench/complex.py tests/test_complex.py
git commit -m "feat(complex): real defect fixtures with pre-registered predictions

Every Truth value is derived from its injection patch. Each defect is a seam
the corpus already had -- none were manufactured to fill the matrix."
```

---

### Task 4: `LOCATED:` parsing and truth matching

**Files:**
- Modify: `toolbench/complex.py`, `tests/test_complex.py`
- Create: `tests/fixtures/complex_session_located.jsonl`

**Interfaces:**
- Produces: `find_located(path) -> tuple[str, dict] | None`, `located_correct(obj, truth) -> bool`.

- [ ] **Step 1: Write the fixture and the failing test**

The fixture is pinned to a **real** Claude transcript shape — one assistant record
per content block, `timestamp` at top level. (`active-probes.md`: four fixtures
once pooled every block of a response into one record, a shape the runtime never
emits, and hid a bug for three revisions.)

```json
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
        hit = find_located(FIXTURE)
        assert hit is not None
        self.assertTrue(
            located_correct(hit[1], Truth("web/src/lib/schedule.ts", "formatSlot", (15, 18)))
        )

    def test_right_symbol_in_the_wrong_file_is_not_a_hit(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        self.assertFalse(
            located_correct(hit[1], Truth("web/src/lib/other.ts", "formatSlot", (15, 18)))
        )

    def test_disjoint_line_ranges_are_not_a_hit(self) -> None:
        hit = find_located(FIXTURE)
        assert hit is not None
        self.assertFalse(
            located_correct(hit[1], Truth("web/src/lib/schedule.ts", "formatSlot", (90, 99)))
        )

    def test_a_session_that_never_locates_returns_none(self) -> None:
        self.assertIsNone(find_located("tests/fixtures/complex_session_agent_escape.jsonl"))
```

(The `agent_escape` fixture is created in Task 5; if it does not yet exist, create
it now with the two lines shown there — it carries no `LOCATED:` line.)

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

    Returns `None` when the agent never claimed a localization -- a real outcome
    (it may still have guessed its way to a passing test), recorded as such and
    never back-filled.
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

    Exact line equality would be brittle: an agent that reports a whole function
    body while the patch touched one line inside it has still localized correctly.
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
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py tests/fixtures/complex_session_located.jsonl
git commit -m "feat(complex): parse the LOCATED: marker and match it against patch ground truth"
```

---

### Task 5: Trial scoring — N1, N2, and the arm audit

**Files:**
- Modify: `toolbench/complex.py`, `tests/test_complex.py`
- Create: `tests/fixtures/complex_session_agent_escape.jsonl`

**Interfaces:**
- Consumes: `find_located`, `located_correct`, `ArmSpec`, `DefectSpec` (Tasks 1, 4).
- Produces: `TrialResult`, `load_calls(path)`, `arm_violations(calls, arm)`, `score_trial(session_path, defect, arm, trial, fixed) -> TrialResult`.

**Implementer note — derive, do not copy.** The expected N1 in the test below is
`output_chars // 4` of the fixture's tool_result payloads (`ToolCall.tokens`).
**Compute it from the fixture; do not trust any number written in this plan.**
Write the test with the value you compute and say what it is in your report.

- [ ] **Step 1: Write the fixture and the failing test**

`tests/fixtures/complex_session_agent_escape.jsonl` — a control arm reaching for a
subagent. Same real record-per-block shape:

```json
{"type":"assistant","timestamp":"2026-07-12T10:00:00Z","requestId":"req_1","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Task","input":{"prompt":"grep for formatSlot"}}]}}
{"type":"user","timestamp":"2026-07-12T10:00:01Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"found it"}]}}
```

```python
# append to tests/test_complex.py
from dataclasses import replace

from toolbench.complex import DefectSpec, arm_violations, load_calls, score_trial

D_FIX = DefectSpec(
    id="DT",
    repo="wids",
    language="typescript",
    truth=Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20)),
    predicted_winner="native",
    rationale="test fixture",
)
GATE = "Bash(npx vitest run:*)"


def _arm(name: str):
    return next(a for a in build_arms(GATE) if a.name == name)


class TrialScoringTests(unittest.TestCase):
    def test_n1_counts_only_calls_before_the_located_line(self) -> None:
        result = score_trial(FIXTURE, D_FIX, _arm("native"), trial=1, fixed=True)
        self.assertTrue(result.located)
        # Grep's tool_result precedes LOCATED:; Edit's follows it.
        # (Assert the value you COMPUTED from the fixture, not one copied here.)
        self.assertGreater(result.n1, 0)
        self.assertEqual(result.n2, 0)

    def test_unlocated_but_fixed_records_no_navigation_number(self) -> None:
        # Guessing its way to green is a real outcome and must stay visible.
        wrong = replace(D_FIX, truth=Truth("nope.ts", "nope", (1, 2)))
        result = score_trial(FIXTURE, wrong, _arm("native"), trial=1, fixed=True)
        self.assertFalse(result.located)
        self.assertIsNone(result.n1)
        self.assertTrue(result.fixed)

    def test_a_call_outside_the_arm_is_a_violation(self) -> None:
        calls = load_calls(FIXTURE)  # fixture uses Grep + Edit
        self.assertEqual(arm_violations(calls, _arm("serena")), ("Edit", "Grep"))

    def test_the_agent_tool_is_a_violation_even_for_the_control_arm(self) -> None:
        # The ban is verified from the transcript, never trusted from the flag.
        calls = load_calls("tests/fixtures/complex_session_agent_escape.jsonl")
        self.assertIn("Task", arm_violations(calls, _arm("control")))
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
    localized has no navigation number, one that never fixed has no edit number.
    They are never back-filled -- an arm that fails is cheap, and its cheapness
    means nothing.
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
        _parser, replayed = detect_parser(handle)
        result = ClaudeParser().parse(
            replayed,
            agent="claude-code",
            source="raw",
            project=session_path.parent.name,
        )
    return result.calls


def arm_violations(calls: list[ToolCall], arm: ArmSpec) -> tuple[str, ...]:
    """Tool names the arm used but was not granted -- plus any banned tool, always.

    The restriction is verified from the transcript, never trusted from the
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
    if located and hit is not None:
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
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py tests/fixtures/complex_session_agent_escape.jsonl
git commit -m "feat(complex): score N1/N2 per trial; audit arm restrictions from the transcript"
```

---

### Task 6: Routing-profile report — solve rate and cost never blended

**Files:**
- Modify: `toolbench/complex.py`, `tests/test_complex.py`

**Interfaces:**
- Consumes: `TrialResult` (Task 5).
- Produces: `ProfileRow`, `build_profile(results) -> list[ProfileRow]`, `render_profile(rows) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_complex.py
from toolbench.complex import ProfileRow, TrialResult, build_profile, render_profile


def _trial(arm, located, fixed, n1, n2, violations=()):
    return TrialResult("D1", "wids", arm, 1, located, fixed, n1, n2, 3, violations)


class ProfileTests(unittest.TestCase):
    def test_median_cost_counts_only_solved_trials(self) -> None:
        rows = build_profile([
            _trial("serena", True, True, 100, 10),
            _trial("serena", True, True, 300, 10),
            _trial("serena", False, False, None, None),  # must not drag the median
        ])
        row = next(r for r in rows if r.arm == "serena")
        self.assertEqual(row.median_n1, 200)
        self.assertAlmostEqual(row.locate_rate, 2 / 3)

    def test_an_arm_that_never_solves_reports_no_cost_at_all(self) -> None:
        # Its cheapness is meaningless; a number here would be a lie.
        rows = build_profile([_trial("bash", False, False, None, None)])
        self.assertIsNone(rows[0].median_n1)

    def test_unsolved_trials_are_named_in_the_report_not_dropped(self) -> None:
        text = render_profile(build_profile([_trial("bash", False, False, None, None)]))
        self.assertIn("Unsolved trials: 1", text)

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

    Solve rate and cost are kept SEPARATE and never blended: an arm that never
    finds the bug is cheap, and cost is uninterpretable without conditioning on
    success.
    """
    grouped: dict[tuple[str, str, str], list[TrialResult]] = defaultdict(list)
    for result in results:
        grouped[(result.repo, result.defect_id, result.arm)].append(result)

    rows: list[ProfileRow] = []
    for (repo, defect_id, arm), trials in sorted(grouped.items()):
        located = [t for t in trials if t.located]
        fixed = [t for t in trials if t.fixed]
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
                violations=tuple(sorted({v for t in trials for v in t.violations})),
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
            f"| {row.locate_rate:.0%} | {row.fix_rate:.0%} | {n1} | {n2} | {row.unsolved} |"
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
            lines.append(
                f"- {row.repo}/{row.defect_id}/{row.arm}: {', '.join(row.violations)}"
            )

    lines.append("")
    lines.append(
        f"Unsolved trials: {sum(r.unsolved for r in rows)} of {sum(r.trials for r in rows)}."
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_complex.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add toolbench/complex.py tests/test_complex.py
git commit -m "feat(complex): routing-profile report; solve rate and cost never blended"
```

---

### Task 7: The trial runner

**Files:**
- Create: `toolbench/complex_runner.py`, `tests/test_complex_runner.py`

**Interfaces:**
- Consumes: `ArmSpec`, `DefectSpec`, `TrialResult`, `score_trial`, `BANNED_TOOLS` (Tasks 1, 5).
- Produces: `build_claude_argv(prompt, arm, cwd) -> list[str]`, `run_trial(defect, arm, trial, workdir, launch, oracle) -> TrialResult`, `shell_oracle(test_cmd, test_cwd) -> Oracle`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_complex_runner.py
import unittest
from pathlib import Path

from toolbench.complex import DefectSpec, Truth, build_arms
from toolbench.complex_runner import build_claude_argv, run_trial

FIXTURE = Path("tests/fixtures/complex_session_located.jsonl")
GATE = "Bash(npx vitest run:*)"

DEFECT = DefectSpec(
    id="DT",
    repo="wids",
    language="typescript",
    truth=Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20)),
    predicted_winner="native",
    rationale="test fixture",
)


def _arm(name: str):
    return next(a for a in build_arms(GATE) if a.name == name)


class ClaudeArgvTests(unittest.TestCase):
    def test_allowed_tools_are_passed_and_agent_never_appears(self) -> None:
        argv = build_claude_argv("find the bug", _arm("serena"), Path("/tmp/wt"))
        self.assertIn("--allowedTools", argv)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertIn("mcp__plugin_serena_serena__find_symbol", allowed)
        self.assertNotIn("Task", allowed)
        self.assertNotIn("Agent", allowed)

    def test_the_ban_is_also_stated_explicitly(self) -> None:
        # --allowedTools alone is an allowlist; --disallowedTools states the ban so
        # a future permissive default cannot quietly reopen the subagent escape.
        argv = build_claude_argv("find the bug", _arm("control"), Path("/tmp/wt"))
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Task", argv[argv.index("--disallowedTools") + 1])


class RunTrialTests(unittest.TestCase):
    def test_oracle_verdict_flows_into_the_scored_result(self) -> None:
        launched: list[str] = []

        def fake_launch(argv: list[str], cwd: Path) -> Path:
            launched.append("launched")
            return FIXTURE

        def fake_oracle(cwd: Path) -> bool:
            return False  # suite still red

        result = run_trial(DEFECT, _arm("native"), 1, Path("/tmp/wt"), fake_launch, fake_oracle)
        self.assertEqual(launched, ["launched"])
        self.assertFalse(result.fixed)
        self.assertTrue(result.located)
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
    """Run one cell and score it. The prompt is the defect's bug report."""
    prompt_path = workdir / "PROMPT.md"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else defect.rationale
    session_path = launch(build_claude_argv(prompt, arm, workdir), workdir)
    return score_trial(session_path, defect, arm, trial, oracle(workdir))


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

### Task 8: Pilot run (operator-driven)

- [ ] **Step 1: Confirm the serena arm precondition per (repo, language)**

Activate each vendored repo in serena; call `get_symbols_overview` on one file per
language. **A serena arm in a language serena cannot index is not a serena arm** —
it is a text search wearing serena's name, and it will still emit a plausible
number. Disqualify that cell; do not score it.

Expected: `corpus/maltese` returns Rust symbols; `corpus/wids` returns TS symbols;
SQL errors — **which is the D5 finding, not a bug.**

- [ ] **Step 2: Pre-warm serena's LSP index outside the measured window**

A fresh worktree per trial would otherwise charge serena a cold-index tax on every
trial that a real user pays once — which could single-handedly manufacture a serena loss.

- [ ] **Step 3: Run the pilot — 1 trial per cell**

Matrix: 2 repos × (defects with a real precondition in that repo, from Task 3) × 4 arms.

**The pilot proves the harness, not the answer.** N=1 is never an answer. Success
is: isolation held, the oracle fired, tokens were attributable, and
`render_profile` reports **zero VIOLATION lines**.

- [ ] **Step 4: Read the variance, then choose trial depth**

Set the real run's trial count from the observed spread — do not guess it now.

- [ ] **Step 5: Commit the report**

```bash
git add reports/2026-07-12-complex-probe-pilot.md
git commit -m "docs(complex): pilot results — harness validation, not an answer"
```

---

## Self-review

**Spec coverage:** corpus → Task 2; defects + pre-registration → Task 3; arms +
Agent ban → Tasks 1, 5, 7; arm precondition check → Tasks 2, 8; isolation → Tasks
7, 8; metrics N1/N2 → Tasks 4, 5; failure handling → Task 6; run size → Task 8.

**Placeholder scan:** clean. The provisional `DEFECTS` tuple was removed in
pre-flight; `DEFECTS` is now built in Task 3 from real patches, so no fabricated
`Truth` value is ever committed. The one number I could not verify (N1 in Task 5)
is explicitly marked *derive, do not copy*.

**Type consistency:** `Truth`, `DefectSpec`, `ArmSpec`, `TrialResult`, `ProfileRow`
are defined once in `toolbench/complex.py` and used with identical names and field
types in `complex_runner.py` and both test modules. `score_trial`'s signature is
identical at definition (Task 5) and call site (Task 7).

**Known gap, carried deliberately:** D3 (call chain) and D4 (moved module) have no
dedicated task. Task 3 Step 1 may find they have no genuine precondition in one or
both repos. They are added by repeating Task 3 once a real seam is confirmed.
Manufacturing a seam to fill the matrix is forbidden.
