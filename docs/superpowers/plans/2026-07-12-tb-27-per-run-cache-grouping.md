# TB-27 — Per-Run Cache-Token Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute Claude cache tokens to an orchestration *run* by summing per-entry `usage` bucketed by that entry's `gitBranch`, against a branch set supplied by a JSON run-manifest.

**Architecture:** `ClaudeParser` gains one *additive* field, `usage_by_branch`, filled in its existing single pass — the S39 session totals are untouched, so TB-26 cannot regress. A new `run_manifest` module reads the JSON manifest. `Reducer` gains a `RunStats` fold that sums only in-set branches and books the rest as `unattributed`, scoped to *candidate sessions*. The report gains a per-run caveat section. `toolbench/cache_tokens.py` is deleted; its run aggregation moves into the reducer.

**Tech Stack:** Python 3, stdlib only (S20). uv-managed. `unittest` + `pytest` runner. `ruff` + `mypy --strict`.

## Global Constraints

- **Stdlib only (S20).** `toolbench` imports nothing third-party. The manifest is JSON via `json` — no YAML, no TOML.
- **Strict gate (S22).** `uv run ruff check .`, `uv run mypy --strict toolbench tests`, and the full `uv run pytest -q` suite are green before any PR. Run from the repo root (`pyproject.toml` is at root, not in a `python/` subdir).
- **No second JSONL interpreter (CQ 1.2).** Branch bucketing happens inside `ClaudeParser.parse`'s existing loop. Do not add another file reader.
- **Cache read and creation always travel together (S39).** Never surface read alone — a prefix-sharing change trades one for the other, so a read delta read alone misleads.
- **Cache tokens are a caveat, never a ranking (S19/S39).** The run section must not enter any leaderboard or inefficiency ratio.
- **`None` ≠ `0` (S32).** `None` means unmeasured; `0` means measured-and-zero. Never coalesce one into the other.
- **Name the gap (S23/S38).** Anything not counted is reported with a count, never silently dropped.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

**Spec:** `docs/superpowers/specs/2026-07-12-tb-27-per-run-cache-grouping-design.md` (merged `a48ca80`). Read it before Task 1.

---

## File Structure

| File | Responsibility |
|---|---|
| `toolbench/transcript.py` | **Modify.** Add `BranchUsage` dataclass; add `usage_by_branch` to `ParseResult`. |
| `toolbench/parsers.py` | **Modify.** `ClaudeParser.parse` buckets usage by `gitBranch` in its existing loop. |
| `toolbench/run_manifest.py` | **Create.** Read/validate the JSON run-manifest. One responsibility: manifest → `RunManifest`. |
| `toolbench/reducer.py` | **Modify.** Add `RunStats` + the run fold (candidate sessions, unattributed, zero-match branches). |
| `toolbench/report.py` | **Modify.** Render the per-run section. |
| `toolbench/passive.py` | **Modify.** `--run-manifest` / `--tickets` flags; wire manifest → reducer → report. |
| `toolbench/cache_tokens.py` | **Delete.** Retired into the analyzer per its own docstring. |
| `.claude/skills/cache-token-metrics/SKILL.md` | **Modify.** Re-point at `toolbench.passive --run-manifest`. |
| `tests/test_run_manifest.py` | **Create.** Manifest reader evals. |
| `tests/test_cache_tokens.py` | **Delete.** Its evals migrate to the tests above. |
| `SPEC.md` / `EVALUATION.md` / `BUILDPLAN.md` | **Modify.** S40 rows; correct T17. |

---

### Task 1: Bucket usage by `gitBranch` in the parser

The additive field. Everything downstream reads it, nothing existing changes.

**Files:**
- Modify: `toolbench/transcript.py` (add `BranchUsage`; add field to `ParseResult`)
- Modify: `toolbench/parsers.py:245-275,355-366` (`ClaudeParser.parse`)
- Test: `tests/test_parsers.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `BranchUsage(read: int, creation: int, input: int, output: int, messages: int)` — mutable dataclass, all default `0`.
  - `ParseResult.usage_by_branch: dict[str, BranchUsage]` — default `{}`. Key is the entry's `gitBranch`; entries carrying `usage` but no `gitBranch` bucket under `""` (they can never match a run branch, so they land in `unattributed` — never silently dropped).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_parsers.py`:

```python
def test_claude_parser_buckets_usage_by_git_branch() -> None:
    """S40: attribution is per-entry, by that entry's gitBranch. A session that
    straddles branches splits across buckets -- it is not owned by one run."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s1",
                "timestamp": "2026-07-01T00:00:00Z",
                "gitBranch": "feat/tb-21",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "usage": {
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 30,
                        "input_tokens": 5,
                        "output_tokens": 7,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s1",
                "timestamp": "2026-07-01T00:01:00Z",
                "gitBranch": "main",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "usage": {
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 10,
                        "input_tokens": 1,
                        "output_tokens": 2,
                    },
                },
            }
        ),
    ]
    result = ClaudeParser().parse(lines, agent="claude-code", source="raw", project="p")

    assert sorted(result.usage_by_branch) == ["feat/tb-21", "main"]
    assert result.usage_by_branch["feat/tb-21"].read == 300
    assert result.usage_by_branch["feat/tb-21"].creation == 30
    assert result.usage_by_branch["feat/tb-21"].messages == 1
    assert result.usage_by_branch["main"].read == 100
    assert result.usage_by_branch["main"].creation == 10


def test_claude_parser_session_totals_equal_sum_of_branch_buckets() -> None:
    """S40 invariant: the new bucket dict is ADDITIVE. The S39/TB-26 session
    totals must still equal the sum over buckets -- this pins that adding
    usage_by_branch did not regress TB-26."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s1",
                "timestamp": "2026-07-01T00:00:00Z",
                "gitBranch": branch,
                "message": {
                    "role": "assistant",
                    "content": [],
                    "usage": {
                        "cache_read_input_tokens": read,
                        "cache_creation_input_tokens": creation,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            }
        )
        for branch, read, creation in (("a", 300, 30), ("b", 100, 10), ("a", 5, 1))
    ]
    result = ClaudeParser().parse(lines, agent="claude-code", source="raw", project="p")

    assert result.session_cache_read_tokens == sum(
        b.read for b in result.usage_by_branch.values()
    )
    assert result.session_cache_creation_tokens == sum(
        b.creation for b in result.usage_by_branch.values()
    )
    assert result.session_cache_read_tokens == 405


def test_claude_parser_usage_without_git_branch_buckets_under_empty_key() -> None:
    """Never drop billed tokens. An entry with usage but no gitBranch cannot match
    a run branch, so it buckets under "" and lands in `unattributed` downstream."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s1",
                "timestamp": "2026-07-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "usage": {"cache_read_input_tokens": 9, "cache_creation_input_tokens": 1},
                },
            }
        ),
    ]
    result = ClaudeParser().parse(lines, agent="claude-code", source="raw", project="p")

    assert result.usage_by_branch[""].read == 9
    assert result.session_cache_read_tokens == 9


def test_claude_parser_no_usage_leaves_usage_by_branch_empty() -> None:
    """Unmeasured session: no usage anywhere -> no buckets (and S39 fields stay None)."""
    lines = [json.dumps({"type": "user", "sessionId": "s1", "gitBranch": "main"})]
    result = ClaudeParser().parse(lines, agent="claude-code", source="raw", project="p")

    assert result.usage_by_branch == {}
    assert result.session_cache_read_tokens is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py -q -k "branch"`
Expected: FAIL — `AttributeError: 'ParseResult' object has no attribute 'usage_by_branch'`

- [ ] **Step 3: Add `BranchUsage` and the `ParseResult` field**

In `toolbench/transcript.py`, add above `ParseResult`:

```python
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
```

Then add the field to `ParseResult` (after `session_usage_messages`, before `unjoinable`):

```python
    # S40: per-entry usage bucketed by gitBranch. ADDITIVE beside the S39 session
    # totals -- the invariant `session total == sum of buckets` is an eval. Entries
    # with usage but no gitBranch bucket under "" so no billed token is dropped.
    usage_by_branch: dict[str, BranchUsage] = field(default_factory=dict)
```

- [ ] **Step 4: Bucket in `ClaudeParser.parse`**

In `toolbench/parsers.py`, import `BranchUsage` from `toolbench.transcript`. Replace the usage-accumulation block (currently `parsers.py:267-274`):

```python
            if isinstance(message, dict):
                usage = message.get("usage")
                if isinstance(usage, dict):
                    usage_messages += 1
                    entry_read = _as_usage_int(usage.get("cache_read_input_tokens"))
                    entry_creation = _as_usage_int(usage.get("cache_creation_input_tokens"))
                    entry_input = _as_usage_int(usage.get("input_tokens"))
                    entry_output = _as_usage_int(usage.get("output_tokens"))
                    cache_read += entry_read
                    cache_creation += entry_creation
                    input_tokens += entry_input
                    output_tokens += entry_output
                    # S40: same pass, no second interpreter (CQ 1.2). Bucket by the
                    # ENTRY's branch, not the session's -- sessions straddle.
                    branch = entry.get("gitBranch")
                    bucket = usage_by_branch.setdefault(
                        branch if isinstance(branch, str) else "", BranchUsage()
                    )
                    bucket.read += entry_read
                    bucket.creation += entry_creation
                    bucket.input += entry_input
                    bucket.output += entry_output
                    bucket.messages += 1
```

Initialize the dict alongside the existing counters (`parsers.py:245`):

```python
        cache_read = cache_creation = input_tokens = output_tokens = usage_messages = 0
        usage_by_branch: dict[str, BranchUsage] = {}
```

And thread it into the measured return (`parsers.py:357-366`) by adding one kwarg:

```python
            session_usage_messages=usage_messages,
            usage_by_branch=usage_by_branch,
```

Leave the `if usage_messages == 0:` early return exactly as it is — an unmeasured session correctly returns no buckets.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py -q`
Expected: PASS, including the four new tests.

- [ ] **Step 6: Prove the invariant test can fail (mutation check)**

Temporarily change `bucket.read += entry_read` to `bucket.read += entry_read // 2`.
Run: `uv run pytest tests/test_parsers.py -q -k "session_totals_equal"`
Expected: FAIL. **Revert the mutation.** A test that cannot fail is not a test.

- [ ] **Step 7: Add the date-range survival test**

Append to `tests/test_passive_cli.py::DateRangeFilterTests` (this is the standing habit from TB-25/TB-26 — every new `ParseResult` field gets one):

```python
    def test_usage_by_branch_survives_date_filtering(self) -> None:
        """S40 inherits the TB-25 invariant: usage_by_branch is session-grain, not a
        per-call value, so it passes through --date-from/--date-to intact even when
        every call is filtered out."""
        result = ParseResult(
            calls=[make_call(ts="2026-06-01T00:00:00Z")],
            malformed=0,
            usage_by_branch={"feat/tb-21": BranchUsage(read=300, creation=30, messages=1)},
        )
        filtered = _apply_date_range(result, "2026-07-01", None)
        self.assertEqual(len(filtered.calls), 0)
        self.assertEqual(filtered.usage_by_branch["feat/tb-21"].read, 300)
        self.assertEqual(filtered.usage_by_branch["feat/tb-21"].creation, 30)
```

Add `BranchUsage` to the `from toolbench.transcript import ParseResult` line.

- [ ] **Step 8: Run the full gate**

Run: `uv run ruff check . && uv run mypy --strict toolbench tests && uv run pytest -q`
Expected: all clean; test count up by 5 from 356.

- [ ] **Step 9: Commit**

```bash
git add toolbench/transcript.py toolbench/parsers.py tests/test_parsers.py tests/test_passive_cli.py
git commit -m "$(cat <<'EOF'
feat(tb-27): bucket Claude usage by gitBranch (S40)

Entry-grain attribution: usage is bucketed by the ENTRY's gitBranch, because a
session is not owned by one run (29/158 straddle >1 branch). Verified lossless --
1834/1834 usage-bearing entries carry gitBranch; the rest bucket under "" and
surface as unattributed rather than vanishing.

Additive beside the S39 session totals, so TB-26 cannot regress. The invariant
`session total == sum of buckets` is pinned as an eval and mutation-checked.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The run-manifest reader

**Files:**
- Create: `toolbench/run_manifest.py`
- Test: `tests/test_run_manifest.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `RunManifest(run: str, tickets: tuple[str, ...], branches: frozenset[str], worktrees: tuple[str, ...])`
  - `read_run_manifest(path: str) -> RunManifest`
  - `class MalformedRunManifest(RuntimeError)`
  - `RunManifest.ticket_count -> int` (`len(self.tickets)`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_manifest.py`:

```python
"""Evals for the S40 run-manifest reader (toolbench.run_manifest).

JSON, following the S37 freeze-manifest precedent -- no new format, stdlib only.
The orchestrator emits this at DISPATCH, while branch data is still live: agents.md
discards its Branch column on run completion, which is why it cannot serve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolbench.run_manifest import MalformedRunManifest, read_run_manifest


def _write(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_reads_run_tickets_and_branches(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "run": "2",
            "tickets": ["TB-18", "TB-19", "TB-20"],
            "branches": ["feat/tb-18", "tb-19-pytest-gate", "tb-20-cache-read"],
            "worktrees": ["~/wt/tb-19"],
        },
    )
    manifest = read_run_manifest(path)

    assert manifest.run == "2"
    assert manifest.tickets == ("TB-18", "TB-19", "TB-20")
    assert manifest.branches == frozenset(
        {"feat/tb-18", "tb-19-pytest-gate", "tb-20-cache-read"}
    )
    assert manifest.ticket_count == 3


def test_worktrees_optional(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"], "branches": ["b"]})
    assert read_run_manifest(path).worktrees == ()


def test_empty_branches_is_malformed(tmp_path: Path) -> None:
    """A manifest with no branches can attribute nothing -- refuse it loudly rather
    than emit a confident zero for every ticket."""
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"], "branches": []})
    with pytest.raises(MalformedRunManifest, match="branches"):
        read_run_manifest(path)


def test_missing_branches_key_is_malformed(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"]})
    with pytest.raises(MalformedRunManifest, match="branches"):
        read_run_manifest(path)


def test_non_json_is_malformed(tmp_path: Path) -> None:
    """Named because the ticket originally pointed --run-manifest at agents.md, a
    markdown file. Feeding one in must fail with a clear message, not a stack trace."""
    path = tmp_path / "agents.md"
    path.write_text("# Agents\n\n| Role | Ticket |\n", encoding="utf-8")
    with pytest.raises(MalformedRunManifest, match="not valid JSON"):
        read_run_manifest(str(path))


def test_branch_list_must_be_strings(tmp_path: Path) -> None:
    path = _write(tmp_path, {"run": "1", "tickets": ["TB-1"], "branches": [17]})
    with pytest.raises(MalformedRunManifest, match="branches"):
        read_run_manifest(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_run_manifest.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'toolbench.run_manifest'`

- [ ] **Step 3: Write the implementation**

Create `toolbench/run_manifest.py`:

```python
"""The S40 run-manifest: which branches constitute one orchestration run.

JSON, following the `--freeze` manifest precedent (S37, `toolbench/freeze.py`) --
no new format, stdlib only (S20).

The orchestrator emits this **at dispatch**, while the branch data is still live.
`.lattice/orchestration/agents.md` cannot serve: its Active table (the only one
with Branch/Worktree columns) is overwritten each dispatch tick and collapses to
"(none -- dispatch complete)" on finish, while the surviving Archived table has no
branch column at all. By the time a run is measurable, agents.md has discarded the
key we filter on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class MalformedRunManifest(RuntimeError):
    """The run-manifest is unreadable or cannot define a run's branch set."""


@dataclass(frozen=True)
class RunManifest:
    """One orchestration run: its tickets and the branches its delegators worked on."""

    run: str
    tickets: tuple[str, ...]
    branches: frozenset[str]
    worktrees: tuple[str, ...] = ()

    @property
    def ticket_count(self) -> int:
        return len(self.tickets)


def _str_tuple(data: dict[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MalformedRunManifest(f"`{key}` must be a list of strings")
    return tuple(str(v) for v in value)


def read_run_manifest(path: str) -> RunManifest:
    """Read a run-manifest. Raises MalformedRunManifest on anything unusable."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedRunManifest(
            f"{path} is not valid JSON (the run-manifest is JSON, not markdown): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MalformedRunManifest(f"{path} must contain a JSON object")

    branches = _str_tuple(data, "branches")
    if not branches:
        # A run with no branches attributes nothing; every ticket would read as
        # costing zero. Refuse loudly rather than emit a confident wrong number.
        raise MalformedRunManifest(
            f"{path} defines no `branches`; a run with no branch set can attribute nothing"
        )

    run = data.get("run", "")
    return RunManifest(
        run=str(run) if isinstance(run, (str, int)) else "",
        tickets=_str_tuple(data, "tickets"),
        branches=frozenset(branches),
        worktrees=_str_tuple(data, "worktrees"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_run_manifest.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the gate and commit**

Run: `uv run ruff check . && uv run mypy --strict toolbench tests && uv run pytest -q`

```bash
git add toolbench/run_manifest.py tests/test_run_manifest.py
git commit -m "$(cat <<'EOF'
feat(tb-27): add the S40 run-manifest reader

JSON, per the S37 freeze-manifest precedent -- no new format, stdlib only. The
orchestrator emits it at dispatch, while branch data is still live; agents.md
discards its Branch column on run completion and so cannot serve as the input the
ticket named. Feeding a markdown file in fails with a clear message, which is an
explicit eval.

An empty branch set is refused: a run that can attribute nothing would otherwise
report every ticket as costing zero.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: The `RunStats` fold in the reducer

The correctness core. **The counter-trap test in Step 1 is the reason this task exists** — it is the over-count the ticket's original "session set" framing would have shipped.

**Files:**
- Modify: `toolbench/reducer.py` (add `RunStats`; add `run` field + fold to `Reducer.absorb`)
- Test: `tests/test_reducer.py`

**Interfaces:**
- Consumes: `BranchUsage`, `ParseResult.usage_by_branch` (Task 1); `RunManifest` (Task 2).
- Produces:
  - `RunStats(read, creation, input, output, candidate_sessions, unattributed_read, unattributed_creation, branches_seen: set[str])`
  - `RunStats.per_ticket(tickets: int) -> dict[str, float]` — raises `ValueError` if `tickets <= 0`.
  - `RunStats.missing_branches(manifest: RunManifest) -> list[str]` — sorted; manifest branches that matched zero entries.
  - `Reducer.run: RunManifest | None = None` and `Reducer.run_stats: RunStats` (always present; only folded when `run` is not `None`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_reducer.py` (import `Reducer`, `RunStats` from `toolbench.reducer`; `BranchUsage`, `ParseResult` from `toolbench.transcript`; `RunManifest` from `toolbench.run_manifest`):

```python
def _manifest(*branches: str, tickets: tuple[str, ...] = ("TB-1", "TB-2")) -> RunManifest:
    return RunManifest(
        run="2", tickets=tickets, branches=frozenset(branches), worktrees=()
    )


def test_run_fold_sums_only_in_set_branches() -> None:
    reducer = Reducer(run=_manifest("feat/tb-18", "tb-19-pytest-gate"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={
                "feat/tb-18": BranchUsage(read=300, creation=30, messages=1),
                "tb-19-pytest-gate": BranchUsage(read=100, creation=10, messages=1),
                "main": BranchUsage(read=999, creation=99, messages=1),
            },
        ),
    )
    assert reducer.run_stats.read == 400
    assert reducer.run_stats.creation == 40
    assert reducer.run_stats.unattributed_read == 999
    assert reducer.run_stats.unattributed_creation == 99


def test_straddling_session_does_not_donate_its_whole_total() -> None:
    """S40 COUNTER-TRAP. A session that touches a run branch for ONE entry must
    contribute only that entry's usage -- not its session total. This is precisely
    the over-count the ticket's original 'fold the run's session set' framing would
    have shipped, and 29/158 real sessions straddle, so it is not hypothetical."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            # Session total is 10_400 read; only 400 of it was spent on the run.
            usage_by_branch={
                "feat/tb-21": BranchUsage(read=400, creation=40, messages=1),
                "main": BranchUsage(read=10_000, creation=1_000, messages=40),
            },
            session_cache_read_tokens=10_400,
            session_cache_creation_tokens=1_040,
        ),
    )
    assert reducer.run_stats.read == 400  # NOT 10_400
    assert reducer.run_stats.creation == 40  # NOT 1_040
    assert reducer.run_stats.candidate_sessions == 1


def test_non_candidate_session_contributes_nothing_not_even_unattributed() -> None:
    """`unattributed` is scoped to CANDIDATE sessions (those touching >=1 run branch).
    A session that never touches the run is simply not part of it -- counting its
    usage as `unattributed` would drown the figure in unrelated main-branch work and
    make it alarming noise on every run."""
    reducer = Reducer(run=_manifest("feat/tb-21"))
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"main": BranchUsage(read=5_000, creation=500, messages=9)},
        ),
    )
    assert reducer.run_stats.read == 0
    assert reducer.run_stats.unattributed_read == 0
    assert reducer.run_stats.candidate_sessions == 0


def test_missing_branches_are_reported_not_silently_zero() -> None:
    """A manifest branch that matches zero entries is the signature of a typo'd or
    renamed branch. Silent, it reads as 'this ticket cost nothing' (S23/S38)."""
    manifest = _manifest("feat/tb-18", "typo/nonexistent")
    reducer = Reducer(run=manifest)
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"feat/tb-18": BranchUsage(read=10, creation=1, messages=1)},
        ),
    )
    assert reducer.run_stats.missing_branches(manifest) == ["typo/nonexistent"]


def test_run_fold_is_inert_without_a_manifest() -> None:
    """No --run-manifest -> no run accounting. The existing report is unchanged."""
    reducer = Reducer()
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"main": BranchUsage(read=5, creation=1, messages=1)},
        ),
    )
    assert reducer.run_stats.read == 0
    assert reducer.run_stats.candidate_sessions == 0


def test_per_ticket_normalizes_and_rejects_zero() -> None:
    stats = RunStats(read=900, creation=90, input=30, output=60)
    assert stats.per_ticket(3)["cache_read"] == 300.0
    assert stats.per_ticket(3)["cache_creation"] == 30.0
    with pytest.raises(ValueError, match="tickets"):
        stats.per_ticket(0)


def test_prefix_sharing_trap_read_drop_offset_by_creation_rise() -> None:
    """S39/S40: read and creation travel together. A 'win' that drops read 500 while
    raising creation 500 moved no tokens -- reading the read delta alone would call
    it a 50% improvement."""
    before = RunStats(read=1_000, creation=100)
    after = RunStats(read=500, creation=600)
    assert after.read < before.read  # looks like a win
    assert after.total_cache == before.total_cache  # it is not
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reducer.py -q -k "run_fold or straddling or candidate or missing_branches or per_ticket or prefix_sharing"`
Expected: FAIL — `ImportError: cannot import name 'RunStats'`

- [ ] **Step 3: Write the implementation**

In `toolbench/reducer.py`, import `RunManifest`:

```python
from toolbench.run_manifest import RunManifest
```

Add the dataclass after `AgentStats`:

```python
@dataclass
class RunStats:
    """One orchestration run's cache cost (S40) — caveat only, never ranked (S19).

    Attribution is per-ENTRY, by that entry's `gitBranch`. `unattributed` is the
    usage on non-run branches *within candidate sessions* (sessions with >=1 entry
    on a run branch) — the straddle spillover, and nothing else. Scoped corpus-wide
    it would be dominated by unrelated `main` work and read as noise on every run.
    """

    read: int = 0
    creation: int = 0
    input: int = 0
    output: int = 0
    candidate_sessions: int = 0
    unattributed_read: int = 0
    unattributed_creation: int = 0
    branches_seen: set[str] = field(default_factory=set)

    @property
    def total_cache(self) -> int:
        """read + creation. The prefix-sharing invariant: a read drop offset by a
        creation rise moved no tokens, so read alone is never the metric (S39)."""
        return self.read + self.creation

    def per_ticket(self, tickets: int) -> dict[str, float]:
        """Normalize by ticket count so runs of different size compare."""
        if tickets <= 0:
            raise ValueError("tickets must be > 0 to normalize per ticket")
        return {
            "cache_read": self.read / tickets,
            "cache_creation": self.creation / tickets,
            "total_cache": self.total_cache / tickets,
        }

    def missing_branches(self, manifest: RunManifest) -> list[str]:
        """Manifest branches that matched zero entries — a typo'd or renamed branch
        would otherwise read as a ticket that cost nothing (S23/S38: name the gap)."""
        return sorted(manifest.branches - self.branches_seen)
```

Add two fields to `Reducer`:

```python
    run: RunManifest | None = None
    run_stats: RunStats = field(default_factory=RunStats)
```

Add the fold to `Reducer.absorb`, immediately after the existing S32/S39 session-grain block and **before** the `for call in result.calls:` loop:

```python
        # S40: entry-grain run attribution. Kept out of the per-call loop -- cache
        # tokens are billed per message, not per tool call.
        if self.run is not None:
            self._absorb_run(result)
```

And the method on `Reducer`:

```python
    def _absorb_run(self, result: ParseResult) -> None:
        """Fold one session into the run totals (S40). Only *candidate* sessions --
        those with at least one entry on a run branch -- contribute anything."""
        assert self.run is not None
        in_set = {b for b in result.usage_by_branch if b in self.run.branches}
        if not in_set:
            return  # not part of this run; contributes to neither total
        self.run_stats.candidate_sessions += 1
        self.run_stats.branches_seen |= in_set
        for branch, usage in result.usage_by_branch.items():
            if branch in self.run.branches:
                self.run_stats.read += usage.read
                self.run_stats.creation += usage.creation
                self.run_stats.input += usage.input
                self.run_stats.output += usage.output
            else:
                # Straddle spillover: work done in the same session on another branch.
                self.run_stats.unattributed_read += usage.read
                self.run_stats.unattributed_creation += usage.creation
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reducer.py -q`
Expected: PASS

- [ ] **Step 5: Prove the counter-trap can fail (mutation check)**

Temporarily replace the `for branch, usage in ...` loop body's in-set branch with a session-total fold:
`self.run_stats.read += result.session_cache_read_tokens or 0` (i.e. implement the naive "fold the session set" reading of the ticket).
Run: `uv run pytest tests/test_reducer.py -q -k straddling`
Expected: **FAIL — `assert 10400 == 400`.** This is the bug the plan exists to prevent. **Revert the mutation.**

- [ ] **Step 6: Run the gate and commit**

```bash
git add toolbench/reducer.py tests/test_reducer.py
git commit -m "$(cat <<'EOF'
feat(tb-27): fold run cache totals by branch set (S40)

Entry-grain: only usage on branches in the run's set counts toward the run.
`unattributed` captures the straddle spillover within candidate sessions (those
touching >=1 run branch) -- not the whole corpus, which would be dominated by
unrelated main-branch work and read as noise on every run.

The counter-trap eval is the point: a session touching a run branch for ONE entry
must not donate its whole session total. Mutation-checked against the naive
"fold the session set" reading the ticket originally called for (asserts 400, the
naive fold yields 10_400).

Manifest branches matching zero entries are reported, not silently zero.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: The per-run report section

**Files:**
- Modify: `toolbench/report.py:106-117` (signature), Summary region ~`:241-250`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `RunStats`, `Reducer.run` / `Reducer.run_stats` (Task 3).
- Produces: `render_report(..., run_tickets: int | None = None)` — new keyword-only param, default `None`. When `reducer.run is None` the report is **byte-identical to today**.

- [ ] **Step 1: Write the failing tests**

Add these to the existing Summary-rendering `unittest.TestCase` class in
`tests/test_report.py` (the one holding
`test_leaderboard_ranked_by_output_tokens_not_call_count_or_cache`). Add to that
file's imports: `BranchUsage`, `ParseResult` from `toolbench.transcript`,
`Reducer` from `toolbench.reducer`, `RunManifest` from `toolbench.run_manifest`.

```python
def test_run_with_zero_matching_sessions_reports_clearly(self) -> None:
    """S23: an empty run set is a clear message, not a crash and not a silent
    blank. Every manifest branch is named as matching nothing, which is the
    signature of a manifest pointed at the wrong corpus."""
    manifest = RunManifest(
        run="9", tickets=("TB-1",), branches=frozenset({"never/existed"}), worktrees=()
    )
    reducer = Reducer(run=manifest)
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"main": BranchUsage(read=10, creation=1, messages=1)},
        ),
    )
    report = render_report(
        reducer,
        index_source="raw",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        since_note=None,
    )
    self.assertIn("0 candidate sessions", report)
    self.assertIn("matched no entries: never/existed", report)


def test_run_section_absent_without_a_manifest(self) -> None:
    """No --run-manifest -> the report is exactly what it is today."""
    report = render_report(
        Reducer(),
        index_source="raw",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        since_note=None,
    )
    self.assertNotIn("Run cache tokens", report)


def test_run_section_prints_read_and_creation_together(self) -> None:
    """S39/S40: never read alone -- a prefix-sharing change trades one for the other."""
    manifest = RunManifest(
        run="2", tickets=("TB-1", "TB-2"), branches=frozenset({"b1"}), worktrees=()
    )
    reducer = Reducer(run=manifest)
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={
                "b1": BranchUsage(read=900, creation=90, messages=2),
                "main": BranchUsage(read=50, creation=5, messages=1),
            },
        ),
    )
    report = render_report(
        reducer,
        index_source="raw",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        since_note=None,
    )
    self.assertIn("Run cache tokens (run 2): read=900 creation=90", report)
    self.assertIn("unattributed: read=50 creation=5", report)
    self.assertIn("1 candidate session", report)


def test_run_section_normalizes_per_ticket(self) -> None:
    manifest = RunManifest(
        run="2", tickets=("TB-1", "TB-2"), branches=frozenset({"b1"}), worktrees=()
    )
    reducer = Reducer(run=manifest)
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"b1": BranchUsage(read=900, creation=90, messages=2)},
        ),
    )
    report = render_report(
        reducer,
        index_source="raw",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        since_note=None,
    )
    self.assertIn("per ticket (2): read=450.0 creation=45.0", report)


def test_run_section_names_zero_match_branches(self) -> None:
    """A branch matching nothing must be named -- silent, it reads as a free ticket."""
    manifest = RunManifest(
        run="2",
        tickets=("TB-1",),
        branches=frozenset({"b1", "typo/never-pushed"}),
        worktrees=(),
    )
    reducer = Reducer(run=manifest)
    reducer.absorb(
        "claude-code",
        ParseResult(
            calls=[],
            malformed=0,
            usage_by_branch={"b1": BranchUsage(read=10, creation=1, messages=1)},
        ),
    )
    report = render_report(
        reducer,
        index_source="raw",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        since_note=None,
    )
    self.assertIn("matched no entries: typo/never-pushed", report)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py -q -k run_section`
Expected: FAIL — `AssertionError: 'Run cache tokens...' not found`

- [ ] **Step 3: Write the implementation**

In `toolbench/report.py`, add the keyword-only param to `render_report`:

```python
    run_tickets: int | None = None,
```

Then, in the Summary section immediately **after** the existing `Session-grain cache tokens` block, append:

```python
    if reducer.run is not None:
        # S40: per-run cache cost. Caveat only -- never ranked, never folded into an
        # inefficiency ratio (S19). Read and creation always together (S39).
        stats = reducer.run_stats
        lines.append(
            f"- Run cache tokens (run {reducer.run.run}): "
            f"read={stats.read} creation={stats.creation} "
            f"({stats.candidate_sessions} candidate session"
            f"{'' if stats.candidate_sessions == 1 else 's'}; S40 caveat, not ranked)"
        )
        tickets = run_tickets if run_tickets is not None else reducer.run.ticket_count
        if tickets > 0:
            norm = stats.per_ticket(tickets)
            lines.append(
                f"  - per ticket ({tickets}): "
                f"read={norm['cache_read']:.1f} creation={norm['cache_creation']:.1f}"
            )
        if stats.unattributed_read or stats.unattributed_creation:
            # Straddle spillover: same-session work on branches outside the run. A
            # large value means the run total is a narrow slice of what was spent.
            lines.append(
                f"  - unattributed: read={stats.unattributed_read} "
                f"creation={stats.unattributed_creation} "
                f"(same-session work off the run's branches)"
            )
        missing = stats.missing_branches(reducer.run)
        if missing:
            lines.append(f"  - matched no entries: {', '.join(missing)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py -q`
Expected: PASS

- [ ] **Step 5: Run the gate and commit**

```bash
git add toolbench/report.py tests/test_report.py
git commit -m "$(cat <<'EOF'
feat(tb-27): render the per-run cache section (S40)

Read + creation together, per-ticket normalized, with the unattributed spillover
and any zero-match manifest branches named. Caveat only -- never ranked (S19).

Absent entirely without --run-manifest: the existing report is unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire `--run-manifest` / `--tickets` into the CLI

**Files:**
- Modify: `toolbench/passive.py` (`CliArgs:127-141`, `parse_args:192+`, `main:268+`, `render_report` call `:360`)
- Test: `tests/test_passive_cli.py`

**Interfaces:**
- Consumes: `read_run_manifest`, `MalformedRunManifest` (Task 2); `Reducer(run=...)` (Task 3); `render_report(run_tickets=...)` (Task 4).
- Produces: `CliArgs.run_manifest: str | None`, `CliArgs.tickets: int | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_passive_cli.py::CliParsingTests`:

```python
    def test_run_manifest_and_tickets_flags(self) -> None:
        args = parse_args(["--run-manifest", "run.json", "--tickets", "3"])
        self.assertEqual(args.run_manifest, "run.json")
        self.assertEqual(args.tickets, 3)

    def test_run_manifest_defaults_to_none(self) -> None:
        args = parse_args([])
        self.assertIsNone(args.run_manifest)
        self.assertIsNone(args.tickets)

    def test_tickets_zero_is_rejected(self) -> None:
        """S39/S40: `--tickets 0` cannot normalize. Reject it at the CLI rather than
        silently skipping normalization -- a per-ticket figure quietly missing from
        the report is how a benchmark comparison gets made against the wrong number."""
        with self.assertRaises(SystemExit):
            parse_args(["--tickets", "0"])
```

And a new class in the same file:

```python
class RunManifestMainTests(unittest.TestCase):
    def test_malformed_run_manifest_exits_1_with_a_clear_message(self) -> None:
        """The ticket originally pointed --run-manifest at agents.md (markdown).
        Feeding one in must fail clearly, not with a stack trace."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.md"
            path.write_text("# Agents\n\n| Role | Ticket |\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main(["--run-manifest", str(path)])
            self.assertEqual(code, 1)
            self.assertIn("not valid JSON", err.getvalue())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_passive_cli.py -q -k "run_manifest or tickets"`
Expected: FAIL — `AttributeError: 'CliArgs' object has no attribute 'run_manifest'`

- [ ] **Step 3: Write the implementation**

In `toolbench/passive.py`:

Import at top:

```python
from toolbench.run_manifest import MalformedRunManifest, RunManifest, read_run_manifest
```

Add to `CliArgs` (after `freeze`):

```python
    run_manifest: str | None
    tickets: int | None
```

Add to `parse_args` (beside the other flags):

```python
    parser.add_argument("--run-manifest", default=None)
    parser.add_argument("--tickets", type=_positive_int, default=None)
```

with the validator beside the other module-level helpers:

```python
def _positive_int(raw: str) -> int:
    """`--tickets 0` cannot normalize (S39). Reject at parse rather than silently
    dropping the per-ticket line from the report."""
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("--tickets must be > 0 to normalize per ticket")
    return value
```

and thread both into the `CliArgs(...)` construction at the end of `parse_args`:

```python
        run_manifest=_optional_str(ns.run_manifest),
        tickets=ns.tickets,
```

In `main`, **before** the `Reducer()` is constructed:

```python
    run: RunManifest | None = None
    if args.run_manifest is not None:
        try:
            run = read_run_manifest(args.run_manifest)
        except (MalformedRunManifest, OSError) as exc:
            # S23: a bad manifest is a hard stop -- silently scanning without a run
            # would print a corpus report the operator would read as a run report.
            print(f"error: {exc}", file=sys.stderr)
            return 1
```

Construct the reducer with it:

```python
    reducer = Reducer(run=run)
```

And pass the ticket override to the report call at `passive.py:360`:

```python
    report = render_report(
        reducer,
        index_source=args.index_source,
        fallback_reason=fallback_reason,
        ...
        run_tickets=args.tickets,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_passive_cli.py -q`
Expected: PASS

- [ ] **Step 5: Run the gate and commit**

```bash
git add toolbench/passive.py tests/test_passive_cli.py
git commit -m "$(cat <<'EOF'
feat(tb-27): add --run-manifest / --tickets to the passive analyzer (S40)

A malformed manifest is a hard stop (exit 1), not a warning: silently scanning
without a run would print a corpus report the operator would read as a run report.
Feeding in agents.md -- the input the ticket originally named -- fails clearly.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Retire `cache_tokens.py`

Its docstring scopes it as holding run aggregation *"until TB-27's `--run-manifest` lands on the passive analyzer."* That condition is now met. Two paths computing one number is the drift TB-26 was just bitten by.

**Files:**
- Delete: `toolbench/cache_tokens.py`, `tests/test_cache_tokens.py`
- Modify: `.claude/skills/cache-token-metrics/SKILL.md`
- Check: `SPEC.md:242` references `toolbench.cache_tokens` (S21) — update.

**Interfaces:**
- Consumes: the CLI from Task 5. Its evals are already re-homed (per-session sums → Task 1; run aggregation + per-ticket + prefix-sharing trap → Task 3).

- [ ] **Step 1: Confirm every migrated eval has a home**

Run: `uv run pytest tests/test_reducer.py tests/test_parsers.py -q -k "per_ticket or prefix_sharing or branch or session_totals"`
Expected: PASS. Each of `test_cache_tokens.py`'s contracts (per-session sum, run aggregation, per-ticket normalization, zero-is-measured, prefix-sharing trap) is now covered by Tasks 1 and 3. **Do not delete anything until this passes.**

- [ ] **Step 2: Delete the module and its tests**

```bash
git rm toolbench/cache_tokens.py tests/test_cache_tokens.py
git rm -r tests/fixtures/cache_tokens
```

- [ ] **Step 3: Re-point the skill**

In `.claude/skills/cache-token-metrics/SKILL.md`, replace every `toolbench.cache_tokens` invocation with the analyzer, e.g.:

```bash
uv run python -m toolbench.passive \
  --agent claude \
  --run-manifest .lattice/orchestration/run-2.json \
  --tickets 3
```

Update the skill's description line to say the engine is `toolbench.passive --run-manifest` (S40), a run-grain grouping dimension on the analyzer — not a standalone reader.

- [ ] **Step 4: Update the S21 CLI list in `SPEC.md`**

`SPEC.md:242` lists `… toolbench.cache_tokens` as a third CLI. Remove it; the analyzer now owns run grain.

- [ ] **Step 5: Verify nothing still imports it**

Run: `rg -n "cache_tokens" --no-ignore .`
Expected: no hits in `toolbench/`, `tests/`, `SPEC.md`, or the skill. (Hits in `docs/` history and this plan are fine.)

- [ ] **Step 6: Run the gate and commit**

Run: `uv run ruff check . && uv run mypy --strict toolbench tests && uv run pytest -q`

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(tb-27): retire cache_tokens.py into the passive analyzer (S40)

Its own docstring scoped it as holding run aggregation "until TB-27's
--run-manifest lands on the passive analyzer". That condition is now met.

Keeping both would leave two code paths computing one number -- exactly the drift
TB-26 was just bitten by, where the board and the code disagreed about what was
built. Every eval it carried is re-homed first (per-session sums to the parser
tests, run aggregation + per-ticket + the prefix-sharing trap to the reducer
tests), verified before deletion.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Author the contract rows and correct the premise

The build contract must record S40 — **and correct the two places that name an input which does not hold.**

**Files:**
- Modify: `SPEC.md` (add S40 after S39, ~line 194)
- Modify: `EVALUATION.md` (add the S40 eval row after S39, ~line 73)
- Modify: `BUILDPLAN.md:82` (correct T17)
- Modify: the `TB-27` board description (via `lattice`)
- Test: `tests/test_gate_completeness.py` — check whether it asserts every `S<N>` has an eval row; if so it will fail until both rows exist.

**Interfaces:** none (documentation).

- [ ] **Step 1: Run the gate-completeness test to see what it demands**

Run: `uv run pytest tests/test_gate_completeness.py -q -v`
Expected: PASS today. If it enumerates spec IDs, adding S40 to `SPEC.md` without an `EVALUATION.md` row will fail it — add both in the same commit.

- [ ] **Step 2: Add S40 to `SPEC.md`** (after the S39 block)

```markdown
- **S40 — per-run cache-token grouping, entry-grain.**
  A run's cache cost is the sum of `usage` on every transcript **entry** whose
  `gitBranch` is in the run's branch set, supplied by a JSON run-manifest
  (`--run-manifest`, format per the S37 freeze precedent) that the orchestrator
  emits **at dispatch**. Attribution is per-entry, not per-session: a session is
  not owned by one run (29/158 straddle >1 branch), and delegators do not always
  run in worktrees (one is logged as having "Ran in ROOT checkout"), so neither
  branch nor `cwd` partitions sessions cleanly. Verified lossless — 1834/1834
  usage-bearing entries carry `gitBranch`. `ClaudeParser` buckets into
  `usage_by_branch` in its existing pass (no second interpreter, CQ 1.2), additive
  beside the S39 session totals, so `session total == sum of buckets` is an
  invariant. `unattributed` is the usage on non-run branches **within candidate
  sessions** (those with >=1 entry on a run branch) — the straddle spillover;
  scoped corpus-wide it would be dominated by unrelated `main` work. A manifest
  branch matching zero entries is reported, never a silent zero (S23/S38). The run
  section renders read + creation together, normalized per ticket, as a Summary
  caveat — never a ranking column (S19). `.lattice/orchestration/agents.md` cannot
  serve as the manifest: it discards its Branch column on run completion (TB-27;
  builds on the session-grain sums of S39/TB-26).
```

- [ ] **Step 3: Add the S40 eval row to `EVALUATION.md`**

```markdown
| S40 | Claude buckets per-entry `usage` by `gitBranch` into `usage_by_branch`, additive beside the S39 session totals (`session total == sum of buckets`); a straddling session splits across buckets and does **not** donate its session total to a run it merely touched (counter-trap); `unattributed` is scoped to candidate sessions; a manifest branch matching zero entries is named; a markdown file passed to `--run-manifest` fails with a clear message; read + creation rendered together, per-ticket normalized, never ranked; `usage_by_branch` survives `--date-from`/`--date-to` via `replace()` | `autonomous` / `operator-assisted` (live lattice run diff) | `test` (`test_parsers.py` branch bucketing + additivity invariant; `test_run_manifest.py` JSON reader + markdown refusal + empty-branch-set refusal; `test_reducer.py` in-set fold, straddle counter-trap, candidate scoping, zero-match branches, per-ticket, prefix-sharing trap; `test_report.py` run section; `test_passive_cli.py` flags + exit 1 on a bad manifest + date-range survival) |
```

- [ ] **Step 4: Correct `BUILDPLAN.md:82` (T17)**

Replace the T17 row's description. The current text names `--run-manifest <agents.md>` and "folds a lattice run's session set", **both of which are wrong**:

```markdown
| **T17 — per-run cache grouping via `--run-manifest`** (lattice `TB-27`) | `--run-manifest <run.json>` supplies a run's branch set (JSON, per the S37 freeze precedent; emitted by the orchestrator at dispatch). Cache tokens are attributed **per entry** by that entry's `gitBranch` — not per session, because sessions straddle branches (29/158) and delegators do not always run in worktrees. `ClaudeParser` buckets into `usage_by_branch` additively beside the S39 totals; the reducer folds in-set branches into `RunStats` with the straddle spillover booked as `unattributed` (scoped to candidate sessions); the Summary renders read + creation per run, normalized per ticket. `cache_tokens.py` retires into the analyzer. **Premise correction:** the original row named `agents.md` as the manifest — it cannot serve, as it discards its Branch column on run completion | S40 | T16 |
```

- [ ] **Step 5: Correct the TB-27 board description**

```bash
lattice update TB-27 --actor "agent:claude" --description "Per-run cache-token grouping (S40): --run-manifest <run.json> supplies a run's branch set; cache read+creation are attributed PER ENTRY by that entry's gitBranch, normalized per ticket. Entry-grain, not session-grain: sessions straddle branches (29/158) and delegators do not always run in worktrees, so no clean session->run partition exists. PREMISE CORRECTION: the original description named agents.md as the manifest input; it discards its Branch column on run completion and cannot serve. Spec: S40. Depends on TB-26."
```

(If `lattice update` does not take `--description`, run `lattice --help` and use the equivalent; failing that, record the correction as a `lattice comment`.)

- [ ] **Step 6: Run the full gate**

Run: `uv run ruff check . && uv run mypy --strict toolbench tests && uv run pytest -q`
Expected: all green, including `test_gate_completeness.py`.

- [ ] **Step 7: Commit**

```bash
git add SPEC.md EVALUATION.md BUILDPLAN.md .lattice plans/
git commit -m "$(cat <<'EOF'
docs(tb-27): author S40 contract rows and correct the T17 premise

S40 in SPEC.md, its eval row in EVALUATION.md, and a corrected T17 in BUILDPLAN.md.

The correction matters: T17 and the TB-27 board description both named
`--run-manifest <agents.md>` and "the run's session set". Neither holds. agents.md
discards its Branch column on run completion, and sessions do not partition by run
(29/158 straddle >1 branch; one delegator is logged as having run in the root
checkout). Leaving the rows as written would have left the contract asserting
something the build deliberately does not do.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] `uv run ruff check .` → clean
- [ ] `uv run mypy --strict toolbench tests` → clean
- [ ] `uv run pytest -q` → all green
- [ ] `rg -n "cache_tokens" toolbench/ tests/ SPEC.md` → no hits
- [ ] **Live smoke (S40, operator-assisted).** Hand-write a run-manifest for a past run and run the analyzer against the real corpus:
  ```bash
  uv run python -m toolbench.passive --agent claude --project tool-benchmarks \
    --run-manifest .lattice/orchestration/run-2.json
  ```
  Confirm the Summary prints `Run cache tokens (run 2): read=… creation=…`, that `candidate sessions` is non-zero, and that no manifest branch is reported as matching zero entries. A read figure with a zero creation figure on a real run is a red flag — check the bucketing, not the run.
- [ ] Open the PR; it closes TB-27.
