# tool-benchmarks Implementation Plan

> **Historical.** This plan shipped the initial harness. For current install,
> run, and quality-gate commands, follow [`README.md`](../../README.md)
> (Usage + Quality gate). Commands below that say
> `uv run python -m unittest discover tests` are stale — that gate under-collects
> tests (TB-19); use `uv run pytest -q` instead.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a re-runnable, stdlib-only Python harness that analyzes tooling inefficiencies across Claude Code, Hermes, Codex, and other inspectable agent session sources, then emits a markdown report.

**Architecture:** Source adapters turn raw agent sessions into normalized, id-joined `ToolCall` and inefficiency-signal records. Raw data can come from direct filesystem scans such as Claude Code `~/.claude/projects/**/*.jsonl`, or from `agentsview session export <id>` after AgentsView CLI session listing across agents. Two consumers sit on top: `passive.py` streams history into per-agent/per-tool inefficiency reports, and `probe.py` scores sentinel-marked active probes into controlled tool-vs-Bash comparison tables.

## V2 Design Changes

These changes supersede the v1 snippets below where they conflict. They come from the implementation-plan assessment plus AgentsView corpus counts and the corrected repo goal: analyze agent tooling inefficiencies, not Claude.ai web behavior.

- **Observed scale:** design for **8,103 sessions**, **101,919 messages**, **86 projects**, with recent windows around **4,579 sessions**. `passive --all` must stream and reduce incrementally; do not collect all `ToolCall`s from the corpus into one list.
- **Multi-agent scope:** normalized records carry `agent`, `source`, `project`, and `session_id` so reports can compare Claude Code, Hermes, Codex, and other supported runtimes without collapsing them into one bucket.
- **AgentsView CLI index:** `/opt/homebrew/bin/agentsview` is available (`agentsview v0.36.1`). Add `--agent all|claude|codex|hermes|...` and `--index-source auto|raw|agentsview` to `passive.py`. `auto` tries AgentsView session listing/export first, then falls back to raw filesystem scanning if the CLI is missing or exits nonzero; `agentsview` is strict and errors clearly.
- **AgentsView commands:** use `agentsview stats --agent all --json` for count cross-checks, `agentsview projects --json` for project counts, `agentsview session list --agent AGENT_OR_ALL --json --limit 500` with cursor pagination for session ids, and `agentsview session export <id>` for raw session data. `agentsview session tool-calls <id> --json` is validation/debug only, not the benchmark's primary context-cost source.
- **Real result payloads:** parse both top-level `toolUseResult` and real Claude Code `message.content[].type == "tool_result"` blocks. The joined payload is often block-local `content`, not top-level `toolUseResult`.
- **Subagents:** include nested `subagents/*.jsonl` by default and report that choice. Add `--exclude-subagents` for comparisons against UI views that collapse or omit subagent calls.
- **Smoke controls:** add `--limit N` and `--verbose` to `passive.py`; progress goes to stderr every fixed number of files.
- **Since semantics:** v2 `--since` is file-mtime based unless separately upgraded to message timestamp filtering; the generated report must say which one was used.
- **Active probes:** every arm gets a unique sentinel that is not a substring of any other sentinel, and scoring checks both sentinel and expected tool name.
- **Active corpus:** list the exact five probe files before implementation. `/Users/michellerojas/c11-sidequests` contains many files; do not assume a root `README.md`.
- **Metrics:** passive ranking includes context-cost (`chars / 4`), failures, slow calls when timing exists, repeated calls/retries, edit churn signals, context pressure signals, and subagent fan-out. Active probe output reports context-cost and uses real `usage` tokens when a sentinel call is an isolable single-tool turn.
- **Workflow gate:** current worktree is detached `HEAD`; create/switch to a `codex/...` branch before implementation commits. Follow RED -> GREEN -> DOCS commits, then run `ruff`, `mypy --strict`, and the full test suite before push/PR.

**Tech Stack:** Python 3 standard library only at *runtime* (`json`, `dataclasses`, `pathlib`, `statistics`, `argparse`, `datetime`, `unittest`) — the shipped `toolbench` package imports nothing third-party. The *project* is managed with **uv**: `pyproject.toml` + `uv.lock` pin the interpreter and the dev-only toolchain (`ruff`, `mypy`, `pytest`), installed via `uv add --dev` and run via `uv run`. Runtime deps stay empty.

## Global Constraints

Every task's requirements implicitly include these. Values copied verbatim from the design spec (`docs/2026-07-07-tool-benchmarks-design.md`):

- **Python standard library only (runtime)** — the shipped `toolbench` package has zero third-party imports, so the harness runs anywhere `python3` exists. Dev tooling (`ruff`, `mypy`, `pytest`) lives under `[dependency-groups] dev` in `pyproject.toml`, never in runtime deps.
- **uv-managed project** — `uv init` bootstraps `pyproject.toml`; deps added only via `uv add --dev <pkg>` (never `pip install`); tools run via `uv run <cmd>`. `pyproject.toml` and `uv.lock` are committed.
- **Read-only** over all agent session sources — no transcript/session mutation.
- **AgentsView is optional index/export only** — the raw JSONL parser remains authoritative. If `agentsview` reports the local daemon is running but not responding, `--index-source auto` must continue with raw scanning and include the fallback reason in the report.
- **Markdown output only** — no HTML report (that stays owned by the `session-report` skill).
- **No live token-API calls** — all numbers derive from on-disk transcripts.
- **No Claude.ai web benchmark** — this repo analyzes agentic tooling surfaces with inspectable sessions, transcripts, or AgentsView exports.
- **Token estimate convention:** `est_tokens(chars) = chars / 4`, applied identically to inputs and outputs.
- **Fixed active-probe corpus:** five explicitly listed files under `/Users/michellerojas/c11-sidequests` — no unrelated refactors of that corpus.
- **Runnable from repo root** as `uv run python -m toolbench.passive` / `uv run python -m toolbench.probe` (or bare `python3 -m ...` outside the uv env); tests via `uv run python -m unittest discover tests`.
- **Malformed input is never fatal** — bad JSONL lines are counted, skipped, and surfaced in the report footer.
- **Strict quality gate:** `uv run ruff check .`, `uv run mypy --strict toolbench tests`, and `uv run python -m unittest discover tests -v` must pass before final commit/PR.

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `pyproject.toml` | uv project manifest: interpreter pin, empty runtime deps, `dev` group (`ruff`, `mypy`, `pytest`), ruff/mypy config. |
| `uv.lock` | Locked dev toolchain (committed). |
| `toolbench/__init__.py` | Package marker (empty). |
| `toolbench/transcript.py` | Shared substrate: `ToolCall`, `ParseResult`, `SessionRef`, `InefficiencySignal`, `result_len`, Claude Code parser helpers. |
| `toolbench/sources.py` | Source discovery and adapters for raw roots plus AgentsView listing/export across agents. |
| `toolbench/passive.py` | Targets #3 + #2-passive: aggregate history → leaderboard + ToolSearch callout + summary; CLI. |
| `toolbench/probe.py` | Targets #1 + #2-active: find sentinel-marked probes in one session → tool-vs-Bash table; CLI. |
| `protocols/active-probes.md` | Fixed probe definitions with sentinel markers the operator executes. |
| `tests/fixtures/sample.jsonl` | Hand-crafted transcript exercising parser edge cases. |
| `tests/fixtures/probe_session.jsonl` | Hand-crafted session with sentinel-marked probe calls. |
| `tests/test_transcript.py` | Parser unit tests (stdlib `unittest`). |
| `tests/test_passive.py` | Aggregation + report unit tests. |
| `tests/test_probe.py` | Probe-scoring unit tests. |
| `README.md` | How to run each entry point. |

**Spec-signature refinement (applies to Tasks 1–2):** the spec lists `parse_session(path) -> list[ToolCall]`, but also requires the malformed-line count to reach the report footer (spec §Robustness, §passive report section 3). A bare list can't carry that count, so `parse_session` returns a `ParseResult(calls, malformed)` dataclass instead. This is the one deliberate deviation from the spec's stated signature; it is additive and documented here.

---

### Task 1: Package scaffold + `ToolCall` and `result_len`

The pure, dependency-free core: the record type and the result-length normalizer. No file I/O yet.

**Files:**
- Create: `pyproject.toml` + `uv.lock` (via `uv init` / `uv add --dev`)
- Create: `toolbench/__init__.py` (empty)
- Create: `toolbench/transcript.py`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ToolCall` dataclass with fields `agent: str`, `source: str`, `project: str`, `name: str`, `input_json: str`, `output_chars: int`, `session_id: str`, `ts: str`, `usage: dict | None`, `duration_ms: int | None = None`, `error: bool = False`, `no_result: bool = False`; and read-only properties `input_chars -> int` (= `len(input_json)`), `tokens -> float` (= `output_chars / 4`), `input_tokens -> float` (= `input_chars / 4`).
  - `InefficiencySignal` dataclass with `agent, project, session_id, kind, detail, severity` for source-provided or derived signals such as slow calls, failures, retry loops, edit churn, context pressure, and subagent fan-out.
  - `result_len(payload) -> int` — normalizes a `toolUseResult` (dict, str, list-of-blocks, or None) to a character count.

- [ ] **Step 0: Scaffold the uv project**

From the repo root, bootstrap the uv-managed project and add the dev-only toolchain:

```bash
uv init --bare --name toolbench          # writes pyproject.toml, no src/ layout, no sample code
uv add --dev ruff mypy pytest            # dev group only — runtime deps stay empty
```

Then edit `pyproject.toml` so it declares the interpreter floor, an empty runtime dependency list, and tool config:

```toml
[project]
name = "toolbench"
version = "0.1.0"
description = "Analyze tooling inefficiencies across inspectable agent sessions."
requires-python = ">=3.11"
dependencies = []                        # runtime is stdlib-only — keep this empty

[dependency-groups]
dev = ["ruff", "mypy", "pytest"]

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
python_version = "3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Run `uv sync` to materialize the locked env. Commit `pyproject.toml` and `uv.lock`.

Verify: `uv run python -c "import sys; print(sys.version)"` prints ≥3.11, and `uv run ruff --version` / `uv run mypy --version` / `uv run pytest --version` all resolve.

- [ ] **Step 1: Create the empty package marker**

Create `toolbench/__init__.py` with no content (0 bytes).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_transcript.py`:

```python
import json
import unittest

from toolbench.transcript import ToolCall, result_len


class ResultLenTests(unittest.TestCase):
    def test_string_result(self):
        self.assertEqual(result_len("hello"), 5)

    def test_dict_result(self):
        payload = {"stdout": "abc", "exit": 0}
        self.assertEqual(result_len(payload), len(json.dumps(payload)))

    def test_block_list_sums_text(self):
        payload = [{"type": "text", "text": "ab"}, {"type": "text", "text": "cde"}]
        self.assertEqual(result_len(payload), 5)

    def test_block_list_non_text_falls_back_to_json(self):
        block = {"type": "image", "source": {"data": "xx"}}
        self.assertEqual(result_len([block]), len(json.dumps(block)))

    def test_none_result_is_zero(self):
        self.assertEqual(result_len(None), 0)


class ToolCallTests(unittest.TestCase):
    def test_derived_token_properties(self):
        call = ToolCall(
            name="Read",
            input_json='{"file_path": "/x"}',  # 19 chars
            output_chars=400,
            session_id="s1",
            ts="2026-07-07T00:00:00Z",
            usage=None,
        )
        self.assertEqual(call.input_chars, 19)
        self.assertEqual(call.tokens, 100.0)
        self.assertAlmostEqual(call.input_tokens, 19 / 4)

    def test_no_result_defaults_false(self):
        call = ToolCall("X", "{}", 0, "s", "t", None)
        self.assertFalse(call.no_result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_transcript -v`
Expected: FAIL / ERROR with `ImportError: cannot import name 'ToolCall'` (module not written yet).

- [ ] **Step 4: Write the minimal implementation**

Create `toolbench/transcript.py`:

```python
"""Shared substrate: raw transcript JSONL -> normalized, id-joined ToolCall records."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    input_json: str
    output_chars: int
    session_id: str
    ts: str
    usage: dict | None
    no_result: bool = False

    @property
    def input_chars(self) -> int:
        return len(self.input_json)

    @property
    def tokens(self) -> float:
        """Context cost: what the tool result dumps into context, chars/4."""
        return self.output_chars / 4

    @property
    def input_tokens(self) -> float:
        return self.input_chars / 4


def result_len(payload) -> int:
    """Normalize a toolUseResult (dict / str / list-of-blocks / None) to a char count."""
    if payload is None:
        return 0
    if isinstance(payload, str):
        return len(payload)
    if isinstance(payload, dict):
        return len(json.dumps(payload))
    if isinstance(payload, list):
        total = 0
        for block in payload:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                total += len(block["text"])
            else:
                total += len(json.dumps(block))
        return total
    return len(str(payload))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_transcript -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock toolbench/__init__.py toolbench/transcript.py tests/test_transcript.py
git commit -m "feat: uv scaffold + ToolCall record and result_len normalizer"
```

---

### Task 2: `parse_session` — the id-join

Turn one session file into `ToolCall`s: join each `tool_use` block to its `toolUseResult` by id, keep interrupted calls, count malformed lines.

**Files:**
- Modify: `toolbench/transcript.py` (add `ParseResult`, `parse_session`, and `_result_id`/`_result_payload` helpers)
- Create: `tests/fixtures/sample.jsonl`
- Modify: `tests/test_transcript.py` (add `ParseSessionTests`)

**Interfaces:**
- Consumes: `ToolCall`, `result_len` (Task 1).
- Produces:
  - `ParseResult` dataclass: `calls: list[ToolCall]`, `malformed: int`.
  - `parse_session(path) -> ParseResult` — streams one JSONL session file.

**V2 join/payload note for the implementer:** the tool-use `id` on the assistant side (`message.content[].id` where `type == "tool_use"`) is the join key. On the user side the same id appears either as top-level `toolUseID` **or** inside a `tool_result` block at `message.content[].tool_use_id`. The payload can be top-level `toolUseResult`, but real Claude Code transcripts commonly store the useful result in the matching block's `content`. Implement `_result_payload(entry, tid)` and prefer matching block-local `content` when present. Do not trust a parser that only calls `entry.get("toolUseResult")`.

**V2 fixture additions:** add one sanitized real-shaped `user` line like:

```jsonl
{"type": "user", "message": {"role": "user", "content": [{"tool_use_id": "tu-real", "type": "tool_result", "content": "real block payload", "is_error": false}]}}
```

The test must assert that `output_chars == len("real block payload")`.

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/sample.jsonl` (exactly these 6 lines; line 5 is intentionally malformed):

```jsonl
{"type": "assistant", "sessionId": "sess-A", "timestamp": "2026-07-07T10:00:00Z", "message": {"content": [{"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}}], "usage": {"input_tokens": 10, "output_tokens": 5}}}
{"type": "user", "toolUseID": "tu-1", "toolUseResult": "file-a\nfile-b\n"}
{"type": "assistant", "sessionId": "sess-A", "timestamp": "2026-07-07T10:01:00Z", "message": {"content": [{"type": "tool_use", "id": "tu-2", "name": "Grep", "input": {"pattern": "x"}}], "usage": {"input_tokens": 8, "output_tokens": 4}}}
{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-2"}]}, "toolUseResult": [{"type": "text", "text": "hit-1"}, {"type": "text", "text": "hit-2"}]}
{"type": "assistant", "sessionId": "sess-A", "timestamp": "2026-07-07T10:02:00Z", "message": {"content": [{"type": "tool_use", "id": "tu-3", "name": "Read", "input": {"file_path": "/gone"}}], "usage"
{"type": "assistant", "sessionId": "sess-A", "timestamp": "2026-07-07T10:03:00Z", "message": {"content": [{"type": "tool_use", "id": "tu-4", "name": "Read", "input": {"file_path": "/interrupted"}}], "usage": {"input_tokens": 3, "output_tokens": 1}}}
```

Notes on what each line exercises: line 2 = **string** result; line 4 = **MCP block-list** result joined via the in-block `tool_use_id`; line 5 = **malformed** JSON (truncated); line 6 = `tu-4` has **no matching result** (interrupted). `tu-3` never appears because its own line is the malformed one — so only `tu-1`, `tu-2`, `tu-4` become calls.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_transcript.py` (add the import line at top too):

```python
from pathlib import Path

from toolbench.transcript import ParseResult, parse_session

FIXTURE = Path(__file__).parent / "fixtures" / "sample.jsonl"


class ParseSessionTests(unittest.TestCase):
    def setUp(self):
        self.result = parse_session(FIXTURE)

    def test_returns_parse_result(self):
        self.assertIsInstance(self.result, ParseResult)

    def test_one_malformed_line_counted(self):
        self.assertEqual(self.result.malformed, 1)

    def test_three_calls_kept(self):
        names = [c.name for c in self.result.calls]
        self.assertEqual(names, ["Bash", "Grep", "Read"])

    def test_string_result_join(self):
        bash = self.result.calls[0]
        self.assertEqual(bash.output_chars, len("file-a\nfile-b\n"))
        self.assertFalse(bash.no_result)

    def test_block_list_result_join(self):
        grep = self.result.calls[1]
        self.assertEqual(grep.output_chars, len("hit-1") + len("hit-2"))

    def test_interrupted_call_kept_with_zero_output(self):
        read = self.result.calls[2]
        self.assertTrue(read.no_result)
        self.assertEqual(read.output_chars, 0)

    def test_session_and_usage_captured(self):
        self.assertEqual(self.result.calls[0].session_id, "sess-A")
        self.assertEqual(self.result.calls[0].usage, {"input_tokens": 10, "output_tokens": 5})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_transcript -v`
Expected: FAIL with `ImportError: cannot import name 'ParseResult'`.

- [ ] **Step 4: Write the implementation**

Append to `toolbench/transcript.py`:

```python
from pathlib import Path


@dataclass
class ParseResult:
    calls: list[ToolCall]
    malformed: int


def _result_id(entry: dict) -> str | None:
    """Find the tool_use id an assistant call joins to, from a user entry."""
    if isinstance(entry.get("toolUseID"), str):
        return entry["toolUseID"]
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if isinstance(tid, str):
                    return tid
    return None


def _result_payload(entry: dict, tid: str):
    """Prefer the matching block-local tool_result payload, then top-level fallback."""
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tid
                and "content" in block
            ):
                return block["content"]
    return entry.get("toolUseResult")


def parse_session(path) -> ParseResult:
    """Stream one session JSONL; join tool_use blocks to their results by id."""
    pending: dict[str, ToolCall] = {}
    malformed = 0
    with Path(path).expanduser().open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            etype = entry.get("type")
            if etype == "assistant":
                message = entry.get("message", {})
                usage = message.get("usage")
                session_id = entry.get("sessionId", "")
                ts = entry.get("timestamp", "")
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id")
                        if not isinstance(tid, str):
                            continue
                        pending[tid] = ToolCall(
                            name=block.get("name", ""),
                            input_json=json.dumps(block.get("input", {})),
                            output_chars=0,
                            session_id=session_id,
                            ts=ts,
                            usage=usage,
                            no_result=True,
                        )
            elif etype == "user":
                tid = _result_id(entry)
                if tid is not None and tid in pending:
                    call = pending[tid]
                    call.output_chars = result_len(_result_payload(entry, tid))
                    call.no_result = False
    return ParseResult(calls=list(pending.values()), malformed=malformed)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_transcript -v`
Expected: PASS (14 tests total).

- [ ] **Step 6: Commit**

```bash
git add toolbench/transcript.py tests/test_transcript.py tests/fixtures/sample.jsonl
git commit -m "feat: parse_session id-join with malformed/interrupted handling"
```

---

### Task 3: `sources.py` — multi-agent discovery with filters

Yield session references from raw roots and AgentsView, optionally filtered to one agent, project, and/or start timestamp.

**Files:**
- Modify: `toolbench/transcript.py` (keep/add Claude raw `iter_session_files`)
- Create: `toolbench/sources.py`
- Modify: `tests/test_transcript.py` (add `IterSessionFilesTests`)
- Create: `tests/test_sources.py`

**Interfaces:**
- Consumes: nothing from prior tasks for raw filesystem walk; uses `subprocess` only for AgentsView CLI adapters.
- Produces:
  - `iter_session_files(root="~/.claude/projects", project=None, since=None) -> Iterator[Path]`. This remains the Claude Code raw adapter. `project` matches session files whose parent directory name contains the string. `since` is an ISO-8601 string; files whose modification time is strictly before `since` are skipped. Raises `FileNotFoundError` if `root` does not exist.
  - `SessionRef` dataclass in `toolbench/sources.py`: `agent, source, project, session_id, path`.
  - `iter_agentsview_sessions(agent="all", project=None, date_from=None, date_to=None, limit=None) -> Iterator[SessionRef]`, using `agentsview session list --agent <agent> --json --limit 500` with cursor pagination.
  - `open_session_jsonl(ref) -> Iterable[str]`, streaming from `ref.path` for raw refs or `agentsview session export <id>` for AgentsView refs.
  - `iter_sources(agent="all", index_source="auto", ...) -> Iterator[SessionRef]`, the passive CLI's single discovery entry point.

**V2 replacement requirement:** keep the small filesystem tests below for the Claude raw adapter, but add `tests/test_sources.py` with a fake `agentsview` command runner so cursor pagination and `--agent` argument construction are tested without requiring the daemon.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript.py`:

```python
import os
import tempfile
from datetime import datetime, timezone

from toolbench.transcript import iter_session_files


class IterSessionFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "proj-alpha").mkdir()
        (root / "proj-beta").mkdir()
        self.a = root / "proj-alpha" / "s1.jsonl"
        self.b = root / "proj-beta" / "s2.jsonl"
        self.a.write_text("{}\n")
        self.b.write_text("{}\n")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_yields_all_by_default(self):
        found = sorted(p.name for p in iter_session_files(self.root))
        self.assertEqual(found, ["s1.jsonl", "s2.jsonl"])

    def test_project_filter(self):
        found = [p.name for p in iter_session_files(self.root, project="alpha")]
        self.assertEqual(found, ["s1.jsonl"])

    def test_since_filter_skips_old_files(self):
        old = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(self.a, (old, old))
        found = [p.name for p in iter_session_files(self.root, since="2020-01-01T00:00:00Z")]
        self.assertEqual(found, ["s2.jsonl"])

    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            list(iter_session_files(self.root / "nope"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_transcript -v`
Expected: FAIL with `ImportError: cannot import name 'iter_session_files'`.

- [ ] **Step 3: Write the implementation**

Append to `toolbench/transcript.py` (add `from datetime import datetime` and `from typing import Iterator` near the top imports):

```python
def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string, tolerating a trailing 'Z'."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iter_session_files(root="~/.claude/projects", project=None, since=None) -> "Iterator[Path]":
    """Yield session JSONL paths, optionally filtered by project dir and start time."""
    base = Path(root).expanduser()
    if not base.exists():
        raise FileNotFoundError(f"transcript root not found: {base}")
    since_ts = _parse_iso(since).timestamp() if since else None
    for path in sorted(base.glob("**/*.jsonl")):
        if project is not None and project not in path.parent.name:
            continue
        if since_ts is not None and path.stat().st_mtime < since_ts:
            continue
        yield path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_transcript -v`
Expected: PASS (18 tests total).

Run: `uv run python -m unittest tests.test_sources -v`
Expected: PASS for the AgentsView argument construction, cursor pagination, and `SessionRef` tests.

- [ ] **Step 5: Commit**

```bash
git add toolbench/transcript.py toolbench/sources.py tests/test_transcript.py tests/test_sources.py
git commit -m "feat: multi-agent source discovery adapters"
```

---

### Task 4: `passive.py` — leaderboard, ToolSearch callout, report, CLI

Aggregate `ToolCall`s and inefficiency signals across a multi-agent session selection into the repeatable markdown report.

**Files:**
- Create: `toolbench/passive.py`
- Create: `tests/test_passive.py`

**Interfaces:**
- Consumes: `ToolCall`, `InefficiencySignal`, `ParseResult`, `parse_session`, source iterators/adapters (Tasks 1–3).
- Produces:
  - `AgentStats` dataclass: `agent: str`, `sessions: int`, `tool_calls: int`, `total_context_tokens: float`, `failures: int`, `slow_calls: int`.
  - `ToolStats` dataclass: `agent: str`, `name: str`, `count: int`, `total_context_tokens: float`, `median_context_tokens: float`, `total_input_tokens: float`, `failures: int`, `slowest_ms: int | None`.
  - `aggregate(calls: list[ToolCall]) -> list[ToolStats]` — sorted by `(agent, total_context_tokens)` with report rendering ranking globally and per agent.
  - `toolsearch_callout(calls: list[ToolCall]) -> tuple[int, float, float]` — returns `(count, total_context_tokens, avg_per_load)`; `avg_per_load` is `0.0` when count is `0`.
  - `build_report(stats, callout, total_calls, total_context_tokens, malformed, scope_label, run_meta) -> str` — the full markdown document, including index source and fallback state.
  - `main(argv=None) -> int` — CLI entry point.

**V2 replacement requirement:** the `aggregate(calls: list[ToolCall])` helper can remain for unit tests and small slices, but `main()` must not accumulate `all_calls` for the full corpus. Add a reducer object/function that consumes each parsed `ToolCall` as files are parsed and keeps only per-agent/per-tool counters, medians input data per tool, ToolSearch totals, failure counts, slow-call counts, total call count, scanned session count, malformed count, and subagent include/exclude state. Add CLI flags:

- `--agent all|claude|codex|hermes|...`, default `all`.
- `--limit N` for smoke-test caps.
- `--exclude-subagents` to skip paths containing `/subagents/`.
- `--index-source auto|raw|agentsview`, default `auto`.
- `--verbose` for periodic progress to stderr.

AgentsView CLI handling for Task 4:

- Use `/opt/homebrew/bin/agentsview` if present, otherwise search `PATH`.
- For `--index-source agentsview`, list ids with `agentsview session list --agent <agent> --json --limit 500`, follow cursor pagination, then stream each session through `agentsview session export <id>` and feed those lines into the appropriate source parser.
- For `--index-source auto`, try the AgentsView path first; if any setup/listing/export preflight fails, fall back to raw `iter_session_files` and save the exception text as the fallback reason.
- Do not compute benchmark metrics from `agentsview session tool-calls`; it may be used only for spot-check validation because it is already parsed/normalized by AgentsView.

The report must include agent breakdown, per-agent/per-tool leaderboard, inefficiency callouts, scanned/exported session count, joined tool calls, malformed lines, whether subagents were included, whether `--since` used file mtime, the index source actually used, and any AgentsView fallback reason.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_passive.py`:

```python
import unittest

from toolbench.passive import ToolStats, aggregate, build_report, toolsearch_callout
from toolbench.transcript import ToolCall


def mk(name, output_chars, input_json="{}"):
    return ToolCall(name, input_json, output_chars, "s", "t", None)


class AggregateTests(unittest.TestCase):
    def test_ranks_by_total_context_tokens_desc(self):
        calls = [mk("Read", 400), mk("Read", 800), mk("Bash", 40)]
        stats = aggregate(calls)
        self.assertEqual(stats[0].name, "Read")
        self.assertEqual(stats[0].count, 2)
        self.assertEqual(stats[0].total_context_tokens, 300.0)  # (400+800)/4
        self.assertEqual(stats[0].median_context_tokens, 150.0)  # median(100,200)
        self.assertEqual(stats[1].name, "Bash")

    def test_empty_input(self):
        self.assertEqual(aggregate([]), [])


class ToolSearchCalloutTests(unittest.TestCase):
    def test_accumulates_deferral_tax(self):
        calls = [mk("ToolSearch", 4000), mk("ToolSearch", 8000), mk("Read", 40)]
        count, total, avg = toolsearch_callout(calls)
        self.assertEqual(count, 2)
        self.assertEqual(total, 3000.0)  # (4000+8000)/4
        self.assertEqual(avg, 1500.0)

    def test_no_toolsearch_calls(self):
        self.assertEqual(toolsearch_callout([mk("Read", 40)]), (0, 0.0, 0.0))


class BuildReportTests(unittest.TestCase):
    def test_report_has_sections(self):
        stats = aggregate([mk("Read", 800), mk("ToolSearch", 4000)])
        callout = toolsearch_callout([mk("Read", 800), mk("ToolSearch", 4000)])
        report = build_report(stats, callout, total_calls=2,
                              total_context_tokens=1200.0, malformed=3,
                              scope_label="--all",
                              run_meta={"index_source": "raw",
                                        "agentsview_fallback": ""})
        self.assertIn("# tool-usage report", report)
        self.assertIn("## Leaderboard", report)
        self.assertIn("## ToolSearch deferral tax", report)
        self.assertIn("## Summary", report)
        self.assertIn("index source: raw", report)
        self.assertIn("skipped malformed lines: 3", report)
        self.assertIn("Read", report)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_passive -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'toolbench.passive'`.

- [ ] **Step 3: Write the implementation**

Create `toolbench/passive.py`:

```python
"""Targets #3 + #2-passive: aggregate transcript history into a markdown report."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from toolbench.transcript import ToolCall, iter_session_files, parse_session


@dataclass
class ToolStats:
    name: str
    count: int
    total_context_tokens: float
    median_context_tokens: float
    total_input_tokens: float


def aggregate(calls: list[ToolCall]) -> list[ToolStats]:
    buckets: dict[str, list[ToolCall]] = {}
    for call in calls:
        buckets.setdefault(call.name, []).append(call)
    stats = [
        ToolStats(
            name=name,
            count=len(group),
            total_context_tokens=sum(c.tokens for c in group),
            median_context_tokens=median([c.tokens for c in group]),
            total_input_tokens=sum(c.input_tokens for c in group),
        )
        for name, group in buckets.items()
    ]
    stats.sort(key=lambda s: s.total_context_tokens, reverse=True)
    return stats


def toolsearch_callout(calls: list[ToolCall]) -> tuple[int, float, float]:
    hits = [c for c in calls if c.name == "ToolSearch"]
    total = sum(c.tokens for c in hits)
    avg = total / len(hits) if hits else 0.0
    return len(hits), total, avg


def build_report(stats, callout, total_calls, total_context_tokens, malformed,
                 scope_label, run_meta) -> str:
    ts_count, ts_total, ts_avg = callout
    lines = [
        "# tool-usage report",
        "",
        f"_Scope: {scope_label}_",
        "",
        "## Leaderboard",
        "",
        "| Tool | Calls | Total context-tokens | Median context-tokens | Total input-tokens |",
        "|------|------:|---------------------:|----------------------:|-------------------:|",
    ]
    for s in stats:
        lines.append(
            f"| {s.name} | {s.count} | {s.total_context_tokens:.0f} | "
            f"{s.median_context_tokens:.0f} | {s.total_input_tokens:.0f} |"
        )
    lines += [
        "",
        "## ToolSearch deferral tax",
        "",
        f"- ToolSearch calls: {ts_count}",
        f"- Total context-tokens spent on schema dumps: {ts_total:.0f}",
        f"- Average per load: {ts_avg:.0f}",
        "",
        "## Summary",
        "",
        f"- index source: {run_meta.get('index_source', 'unknown')}",
        f"- AgentsView fallback: {run_meta.get('agentsview_fallback') or 'none'}",
        f"- Total tool calls: {total_calls}",
        f"- Total tool-output context-tokens: {total_context_tokens:.0f}",
        "- Top-5 cost drivers: "
        + ", ".join(f"{s.name} ({s.total_context_tokens:.0f})" for s in stats[:5]),
        f"- skipped malformed lines: {malformed}",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="toolbench.passive")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="every project (default)")
    scope.add_argument("--project", help="narrow to one project directory")
    parser.add_argument("--since", help="ISO-8601 lower bound on session mtime")
    parser.add_argument("--out", help="output markdown path")
    args = parser.parse_args(argv)

    try:
        files = list(iter_session_files(project=args.project, since=args.since))
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 1

    if not files:
        print("no sessions matched the selection; nothing to report.")
        return 0

    all_calls: list[ToolCall] = []
    malformed = 0
    for path in files:
        result = parse_session(path)
        all_calls.extend(result.calls)
        malformed += result.malformed

    stats = aggregate(all_calls)
    callout = toolsearch_callout(all_calls)
    total_context = sum(c.tokens for c in all_calls)
    scope_label = args.project and f"--project {args.project}" or "--all"
    report = build_report(stats, callout, len(all_calls), total_context, malformed,
                          scope_label, {"index_source": "raw",
                                        "agentsview_fallback": ""})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out).expanduser() if args.out else Path("reports") / f"{today}-tool-usage.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out} ({len(all_calls)} calls across {len(files)} sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_passive -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Smoke-test the CLI end-to-end**

Run: `uv run python -m toolbench.passive --agent all --all`
Expected: prints `wrote reports/<today>-tool-usage.md (...)` and the file exists with agent breakdown, tool leaderboard, inefficiency callouts, and summary sections. If no configured sources are available, it prints the "no sessions matched" message and exits 0 for `--index-source auto`; strict raw source selection should name the missing root.

- [ ] **Step 6: Commit**

```bash
git add toolbench/passive.py tests/test_passive.py
git commit -m "feat: passive report — leaderboard, ToolSearch tax, summary, CLI"
```

---

### Task 5: `protocols/active-probes.md` + `probe.py` — controlled comparison

Define sentinel-marked probes and score them from one session into a tool-vs-Bash table (targets #1 and #2-active).

**Files:**
- Create: `protocols/active-probes.md`
- Create: `toolbench/probe.py`
- Create: `tests/fixtures/probe_session.jsonl`
- Create: `tests/test_probe.py`

**Interfaces:**
- Consumes: `ToolCall`, `parse_session` (Tasks 1–2).
- Produces:
  - `SENTINELS: dict[str, str]` mapping arm key → marker substring.
  - `BASELINES: dict[str, tuple[float, float]]` — the #8376 seed figures `{"search": (723.0, 794.0), "find": (68.0, 89.0)}` (tool-arm tokens, Bash-arm tokens).
  - `find_probe_calls(calls, sentinel) -> list[ToolCall]` — calls whose `input_json` contains the sentinel.
  - `score(calls) -> dict[str, float]` — arm key → context-tokens for the matched call (0.0 if unmatched).
  - `build_table(scored) -> str` — markdown comparison table.
  - `main(argv=None) -> int` — CLI: `--session PATH`.

**V2 replacement requirement:** replace the sentinel table below before implementation. Use unique, non-overlapping sentinels such as `TB_PROBE_READ_TOOL_V2`, `TB_PROBE_READ_BASH_V2`, `TB_PROBE_SEARCH_TOOL_V2`, `TB_PROBE_SEARCH_BASH_V2`, `TB_PROBE_FIND_TOOL_V2`, `TB_PROBE_FIND_BASH_V2`, and `TB_PROBE_TOOLSEARCH_V2`. `find_probe_calls` must also verify the expected tool name to prevent a Bash arm from satisfying a tool-arm row. The active table should show context tokens for every matched arm and real usage tokens when the call is isolated enough to use `ToolCall.usage`.

**V2 corpus requirement:** before authoring `protocols/active-probes.md`, list the exact five probe file paths under `/Users/michellerojas/c11-sidequests`. Do not use `.../c11-sidequests/README.md` unless that file actually exists.

- [ ] **Step 1: Author the probe protocol**

Create `protocols/active-probes.md`:

```markdown
# Active probes

Fixed, sentinel-marked probe pairs over the 5-file corpus at
`/Users/michellerojas/c11-sidequests`. Each row is a matched pair: the tool
this build ships vs. its Bash equivalent. Execute all probes in one Claude
Code session, then score with:

    uv run python -m toolbench.probe --session <path-to-that-session.jsonl>

The scorer locates each call by a unique sentinel substring in the call input.
Where a tool cannot carry an arbitrary marker, the sentinel is a stable,
probe-specific token in the natural input (a path or pattern) — do not vary it.

| Arm key      | Sentinel substring        | Tool arm (run this)                                  | Bash arm (run this)                              |
|--------------|---------------------------|------------------------------------------------------|--------------------------------------------------|
| `read_tool`  | `c11-sidequests/README`   | `Read` the file `.../c11-sidequests/README.md`       | —                                                |
| `read_bash`  | `TB_PROBE_READ`           | —                                                    | `Bash: sed -n '1,999p' .../README.md  # TB_PROBE_READ` |
| `search_tool`| `TB_PROBE_SEARCH_rename`  | serena `search_for_pattern` pattern `rename`, and pass a marker in an allowed arg so `TB_PROBE_SEARCH_rename` lands in the call input | — |
| `search_bash`| `TB_PROBE_SEARCH`         | —                                                    | `Bash: rg -n rename .../c11-sidequests  # TB_PROBE_SEARCH` |
| `find_tool`  | `TB_PROBE_FIND`           | serena `find_file` / `list_dir` for `*.md`, marker `TB_PROBE_FIND` in an allowed arg | — |
| `find_bash`  | `TB_PROBE_FIND`           | —                                                    | `Bash: rg --files -g '*.md' .../c11-sidequests  # TB_PROBE_FIND` |
| `toolsearch` | `TB_PROBE_TOOLSEARCH`     | one `ToolSearch` query containing `TB_PROBE_TOOLSEARCH`, then its first call | — (baseline is the pre-loaded call) |

Seeded baselines from claude-mem observation #8376 (used when an arm is absent):
content `rename` ≈ 723 tok serena / 794 tok Bash; file-find `*.md` ≈ 68 tok
serena / 89 tok Bash.
```

- [ ] **Step 2: Create the probe fixture**

Create `tests/fixtures/probe_session.jsonl` (each result line joins by an in-block `tool_use_id`):

```jsonl
{"type": "assistant", "sessionId": "probe", "timestamp": "2026-07-07T12:00:00Z", "message": {"content": [{"type": "tool_use", "id": "p1", "name": "Read", "input": {"file_path": "/Users/michellerojas/c11-sidequests/README.md"}}], "usage": {"input_tokens": 5, "output_tokens": 2}}}
{"type": "user", "toolUseID": "p1", "toolUseResult": "0123456789012345678901234567890123456789"}
{"type": "assistant", "sessionId": "probe", "timestamp": "2026-07-07T12:01:00Z", "message": {"content": [{"type": "tool_use", "id": "p2", "name": "Bash", "input": {"command": "rg -n rename /Users/michellerojas/c11-sidequests  # TB_PROBE_SEARCH"}}], "usage": {"input_tokens": 6, "output_tokens": 3}}}
{"type": "user", "toolUseID": "p2", "toolUseResult": "01234567"}
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_probe.py`:

```python
import unittest
from pathlib import Path

from toolbench.probe import (
    BASELINES,
    build_table,
    find_probe_calls,
    score,
)
from toolbench.transcript import parse_session

FIXTURE = Path(__file__).parent / "fixtures" / "probe_session.jsonl"


class FindProbeCallsTests(unittest.TestCase):
    def setUp(self):
        self.calls = parse_session(FIXTURE).calls

    def test_matches_by_sentinel_in_input(self):
        found = find_probe_calls(self.calls, "TB_PROBE_SEARCH")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "Bash")

    def test_no_match_returns_empty(self):
        self.assertEqual(find_probe_calls(self.calls, "TB_PROBE_ABSENT"), [])


class ScoreTests(unittest.TestCase):
    def test_scores_present_arms_and_zeros_absent(self):
        scored = score(parse_session(FIXTURE).calls)
        self.assertEqual(scored["read_tool"], 10.0)     # 40 chars / 4
        self.assertEqual(scored["search_bash"], 2.0)    # 8 chars / 4
        self.assertEqual(scored["find_tool"], 0.0)      # not in fixture


class BuildTableTests(unittest.TestCase):
    def test_table_includes_baselines_and_header(self):
        table = build_table(score(parse_session(FIXTURE).calls))
        self.assertIn("| Task | Tool arm | Bash arm |", table)
        self.assertIn("723", table)  # seeded serena search baseline
        self.assertIn("Read", table)


class BaselineTests(unittest.TestCase):
    def test_baseline_seeds_present(self):
        self.assertEqual(BASELINES["search"], (723.0, 794.0))
        self.assertEqual(BASELINES["find"], (68.0, 89.0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_probe -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'toolbench.probe'`.

- [ ] **Step 5: Write the implementation**

Create `toolbench/probe.py`:

```python
"""Targets #1 + #2-active: score sentinel-marked probes into a tool-vs-Bash table."""

from __future__ import annotations

import argparse
from pathlib import Path

from toolbench.transcript import ToolCall, parse_session

SENTINELS: dict[str, str] = {
    "read_tool": "c11-sidequests/README",
    "read_bash": "TB_PROBE_READ",
    "search_tool": "TB_PROBE_SEARCH_rename",
    "search_bash": "TB_PROBE_SEARCH",
    "find_tool": "TB_PROBE_FIND",
    "find_bash": "TB_PROBE_FIND",
    "toolsearch": "TB_PROBE_TOOLSEARCH",
}

BASELINES: dict[str, tuple[float, float]] = {
    "search": (723.0, 794.0),
    "find": (68.0, 89.0),
}


def find_probe_calls(calls: list[ToolCall], sentinel: str) -> list[ToolCall]:
    return [c for c in calls if sentinel in c.input_json]


def score(calls: list[ToolCall]) -> dict[str, float]:
    scored: dict[str, float] = {}
    for arm, sentinel in SENTINELS.items():
        matched = find_probe_calls(calls, sentinel)
        scored[arm] = matched[0].tokens if matched else 0.0
    return scored


def _cell(measured: float, baseline: float) -> str:
    """Prefer a measured value; fall back to the seeded #8376 baseline."""
    return f"{measured:.0f}" if measured else f"{baseline:.0f} (seed)"


def build_table(scored: dict[str, float]) -> str:
    rows = [
        ("Read a file", scored.get("read_tool", 0.0), scored.get("read_bash", 0.0), (0.0, 0.0)),
        ("Content search (rename)", scored.get("search_tool", 0.0),
         scored.get("search_bash", 0.0), BASELINES["search"]),
        ("File find (*.md)", scored.get("find_tool", 0.0),
         scored.get("find_bash", 0.0), BASELINES["find"]),
        ("ToolSearch deferral", scored.get("toolsearch", 0.0), 0.0, (0.0, 0.0)),
    ]
    lines = [
        "# active-probe comparison",
        "",
        "Context-tokens per arm (tool this build ships vs. its Bash equivalent).",
        "Seeded values from claude-mem observation #8376 where an arm was absent.",
        "",
        "| Task | Tool arm | Bash arm |",
        "|------|---------:|---------:|",
    ]
    for label, tool_tok, bash_tok, (tool_seed, bash_seed) in rows:
        tool_cell = _cell(tool_tok, tool_seed) if tool_seed else f"{tool_tok:.0f}"
        bash_cell = _cell(bash_tok, bash_seed) if bash_seed else f"{bash_tok:.0f}"
        lines.append(f"| {label} | {tool_cell} | {bash_cell} |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="toolbench.probe")
    parser.add_argument("--session", required=True, help="path to the probe session JSONL")
    args = parser.parse_args(argv)

    path = Path(args.session).expanduser()
    if not path.exists():
        print(f"error: session not found: {path}")
        return 1

    result = parse_session(path)
    print(build_table(score(result.calls)))
    if result.malformed:
        print(f"\n_skipped malformed lines: {result.malformed}_")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_probe -v`
Expected: PASS (6 tests).

- [ ] **Step 7: Smoke-test the probe CLI**

Run: `uv run python -m toolbench.probe --session tests/fixtures/probe_session.jsonl`
Expected: prints the `# active-probe comparison` table with a measured `Read` row (10) and a seeded search row (`723 (seed)` for the tool arm).

- [ ] **Step 8: Commit**

```bash
git add protocols/active-probes.md toolbench/probe.py tests/fixtures/probe_session.jsonl tests/test_probe.py
git commit -m "feat: active-probe scorer with sentinel matching and #8376 baselines"
```

---

### Task 6: `README.md` — run instructions and full-suite gate

Document each entry point and confirm the whole suite passes together.

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the CLIs and test layout from Tasks 1–5.
- Produces: operator-facing documentation. No code.

- [ ] **Step 1: Run the full test suite**

Run: `uv run python -m unittest discover tests -v`
Expected: PASS — 30 tests (14 transcript + 6 passive + 6 probe + the 4 iter cases counted within transcript). All green before writing the README.

V2 gate before documentation/final commit:

```bash
uv run ruff check .
uv run mypy --strict toolbench tests
uv run python -m unittest discover tests -v
uv run python -m toolbench.passive --agent all --all --limit 200 --out /tmp/toolbench-scale.md --verbose
uv run python -m toolbench.passive --agent all --all --index-source auto --limit 20 --out /tmp/toolbench-agentsview.md --verbose
```

- [ ] **Step 2: Write the README**

Create `README.md`:

```markdown
# tool-benchmarks

Analyze tooling inefficiencies across Claude Code, Hermes, Codex, and other
inspectable agent session sources. The harness is read-only, Python standard
library only at runtime, and emits markdown reports.

## Targets

1. **Cross-agent tool cost** — which tools, agents, and projects dump the most context back into sessions?
2. **Tooling inefficiency patterns** — failures, slow calls, retries, edit churn, context pressure, and subagent fan-out.
3. **Deferral/discovery tax** — what deferred tool loading and search costs across agent surfaces.
4. **Controlled tool-vs-shell probes** — when native tools are cheaper or more reliable than shell commands.

## Run

From the repo root:

```bash
# Passive: multi-agent inefficiency report
uv run python -m toolbench.passive --agent all --all
uv run python -m toolbench.passive --agent claude --project c11-sidequests --since 2026-07-01T00:00:00Z
uv run python -m toolbench.passive --agent codex --all --out reports/codex.md
# -> writes reports/YYYY-MM-DD-tool-usage.md by default

# Active: score sentinel-marked probes from one session
#   1. Execute the probes in protocols/active-probes.md in a Claude session
#   2. Score that session's transcript:
uv run python -m toolbench.probe --session ~/.claude/projects/<proj>/<session>.jsonl
```

## Test

```bash
uv run python -m unittest discover tests
```

## Layout

- `toolbench/transcript.py` — normalized records and Claude Code parser helpers.
- `toolbench/sources.py` — source discovery and AgentsView adapters across agents.
- `toolbench/passive.py` — multi-agent history aggregation + inefficiency report.
- `toolbench/probe.py` — active-probe scorer (targets #1, #2-active).
- `protocols/active-probes.md` — the fixed probe definitions to execute.
- `docs/` — design spec and this plan.
- `reports/` — generated markdown reports.

## Metric convention

`est_tokens(chars) = chars / 4`, applied identically to inputs and outputs.
Context cost = joined tool-result payload tokens (`chars / 4`) — what a tool
dumps into context. The joined payload may come from top-level `toolUseResult`
or block-local `message.content[].content`. Failure counts, slow-call counts,
retry/edit churn, context pressure, and subagent fan-out are reported as
inefficiency signals where a source exposes enough data. Cache flags are a caveat only,
never used for per-tool ranking.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README with run instructions and layout"
```

- [ ] **Step 4: Push the branch and open a PR**

```bash
git push
gh pr create --fill
```

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:

- Data substrate / record shapes → Task 2 (`parse_session`, `_result_id`).
- `transcript.py` public interface (`iter_session_files`, `parse_session`, `ToolCall`, `result_len`) → Tasks 1–3.
- Robustness (malformed counted, no-result kept) → Task 2 fixture lines 5–6 + tests.
- `passive.py` three report sections (leaderboard, ToolSearch callout, summary) → Task 4.
- `probe.py` + `active-probes.md`, matched pairs, sentinels, #8376 baselines, optional bonus arm → Task 5.
- Metric definitions (context cost, real tokens, cache flag) → context cost is the ranking basis in Tasks 4–5; cache flag is documented as caveat-only in the README (Task 6), never used for ranking, matching the spec.
- Testing (4 fixture edge cases, unittest, `discover`) → Task 2 fixture + Task 6 full-suite gate.
- Error handling (empty selection exit 0, missing root exit 1) → Task 4 `main`.
- Directory layout / runnable as `-m` modules → File Structure + all CLIs.
- Open follow-ups (HTML, trend tracking, per-model) → correctly left out of scope.

**2. Placeholder scan** — no `TBD`/`TODO`/"handle edge cases"/"similar to Task N"; every code step carries complete, runnable code.

**3. Type consistency** — `ToolCall` fields and its `tokens`/`input_chars`/`input_tokens` properties are defined once in Task 1 and used unchanged in Tasks 2–5. `ParseResult(calls, malformed)` is defined in Task 2 and consumed identically in Tasks 4–5. `score()` returns `dict[str, float]` keyed by the same arm strings that `SENTINELS` defines and `build_table` reads. The `real tokens`/`usage` cross-check is captured on `ToolCall.usage` (available for probe refinement) but the deterministic comparison uses context-tokens, matching #8376's payload-size methodology.

**One flagged risk for the executor:** the assistant↔user join-key location (`toolUseID` vs. in-block `tool_use_id`) is handled by `_result_id` covering both, but should be confirmed against one real transcript before trusting `passive --all` numbers (noted in Task 2).
