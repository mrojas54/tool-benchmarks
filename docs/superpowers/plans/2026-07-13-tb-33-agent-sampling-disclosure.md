# TB-33 Agent Sampling Disclosure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Agent Breakdown disclose what fraction of each agent's archive it actually sampled, and give an agent the `--limit` window never reached a named row instead of silent absence.

**Architecture:** Discovery gains an `AgentCensus` — per-agent archive totals gathered under the run's own filters, by retasking the TB-31 parent-probe pass (which already drains the index and throws the agent names away) plus one scoped `--limit 1` call per agent to read its `total`. The census is reconciled against the run-scoped archive total so an agent we failed to enumerate is *named*, not assumed away. `report.py` renders it as a `sampled` column, a Summary block, and an uneven-sampling line. The census is discovery-grain and never enters `reducer.py`.

**Tech Stack:** Python ≥3.13, stdlib only, `uv`-managed. Tests are `pytest` + `unittest.TestCase`, with the `FakeRunner` scripted-subprocess double.

## Global Constraints

- **Stdlib only.** No new dependencies. `toolbench` shells out to `agentsview`; it does not import it.
- **Python ≥3.13**, `from __future__ import annotations` at the top of every module.
- **Gate before any PR:** `uv run ruff check .`, `uv run mypy --strict toolbench tests`, `uv run pytest -q`. Never `unittest discover` (TB-19).
- **The census must carry identical `--project` / `--since` / `--agent` filters as discovery.** A denominator gathered under different filters describes a different population than the numerator. All census argv flows through one `_list_argv()` builder so this is structural, not a comment.
- **Never a silent zero, never a dropped column.** Every absence (failed census, frozen replay, unenumerated agent) is *named in the rendered report*, not sent to stderr — stderr is lost the moment the report is redirected to a file, which is how TB-30 hid.
- **Disclosure only.** The sampling fraction never ranks, filters, or scores. It does not change what `--limit` selects.
- `reducer.py` is **not** modified. A denominator is not a call.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `toolbench/sources.py` | Discovery + the new `AgentCensus` acquisition | Modify |
| `toolbench/report.py` | `sampled` column, spread line, Summary block | Modify |
| `toolbench/passive.py` | Wiring; freeze-replay census | Modify |
| `tests/test_sources.py` | Census acquisition, filter inheritance, failure | Modify |
| `tests/test_report.py` | Rendering: unreached rows, spread, residual | Modify |
| `tests/test_passive.py` | Freeze replay disclosure | Modify |
| `toolbench/reducer.py` | — | **Untouched** |

---

### Task 1: `AgentCensus` and its acquisition in `sources.py`

**Files:**
- Modify: `toolbench/sources.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: existing `_agentsview_pages`, `_ALL_INCLUDES`, `_PROBE_INCLUDES`, `Runner`, `SessionRef`.
- Produces:
  - `AgentCensus(totals: dict[str, int], archive_total: int, unavailable_reason: str | None = None)` with property `residual: int`
  - `discover_agentsview(runner, *, agent, project, since, limit) -> tuple[AgentCensus, Iterator[SessionRef]]`
  - `_list_argv(*, agent, project, since, limit, includes, cursor=None) -> list[str]`
  - `iter_agentsview_sessions(...)` keeps its existing signature and return type (refs only).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sources.py`. Note `_page()` (line 40) already emits a `total` key; these tests need a payload whose `total` differs from `len(sessions)`, so add a small helper alongside it.

```python
def _total_page(total: int) -> str:
    """A `--limit 1` census response: we read `total`, never the rows."""
    return json.dumps({"sessions": [], "next_cursor": "", "total": total})


class AgentCensusTests(unittest.TestCase):
    """Per-agent denominators, gathered under the run's own filters (TB-33)."""

    def test_totals_reconcile_to_zero_residual(self) -> None:
        # probe pass: two agents present as non-children.
        probe = _page(
            {"id": "s1", "agent": "claude", "project": "p"},
            {"id": "s2", "agent": "codex", "project": "p"},
        )
        runner = FakeRunner([
            completed(stdout=probe),            # probe pass
            completed(stdout=_total_page(80)),  # census: --agent claude
            completed(stdout=_total_page(20)),  # census: --agent codex
            completed(stdout=_total_page(100)), # census: archive total (agent=all)
            completed(stdout=probe),            # full listing
        ])
        census, refs = discover_agentsview(runner, agent="all", project=None, since=None, limit=500)
        list(refs)

        self.assertEqual(census.totals, {"claude": 80, "codex": 20})
        self.assertEqual(census.archive_total, 100)
        self.assertEqual(census.residual, 0)
        self.assertIsNone(census.unavailable_reason)

    def test_residual_names_an_agent_the_probe_never_saw(self) -> None:
        # The probe listing excludes children, so an agent whose sessions are ALL
        # children is invisible to it. Reconciliation is the only thing that catches it.
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout=_total_page(80)),   # claude
            completed(stdout=_total_page(100)),  # archive
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(runner, agent="all", project=None, since=None, limit=500)
        list(refs)

        self.assertEqual(census.residual, 20)

    def test_census_inherits_project_and_since_filters(self) -> None:
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout=_total_page(5)),
            completed(stdout=_total_page(5)),
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(
            runner, agent="all", project="tool-benchmarks", since="2026-07-01", limit=500
        )
        list(refs)

        # A denominator gathered under different filters describes a different
        # population than the numerator. Every census call must carry both filters.
        census_calls = [c for c in runner.calls if "--limit" in c and c[c.index("--limit") + 1] == "1"]
        self.assertEqual(len(census_calls), 2)
        for argv in census_calls:
            self.assertIn("--project", argv)
            self.assertIn("tool-benchmarks", argv)
            self.assertIn("--date-from", argv)
            self.assertIn("2026-07-01", argv)

    def test_scoped_agent_run_reconciles_to_zero(self) -> None:
        # Under `--agent codex` the run's population IS codex. An UNSCOPED archive total
        # would compute a residual of every other agent's sessions and scream about
        # thousands of "unenumerated" sessions that were never in scope.
        probe = _page({"id": "s1", "agent": "codex", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout=_total_page(183)),  # census: --agent codex
            completed(stdout=_total_page(183)),  # archive total, ALSO scoped to codex
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(runner, agent="codex", project=None, since=None, limit=500)
        list(refs)

        self.assertEqual(census.residual, 0)
        self.assertEqual(census.archive_total, 183)

    def test_census_failure_is_disclosed_not_dropped(self) -> None:
        # A census we cannot take is rendered as "unknown" WITH a reason. Discovery is
        # unaffected: the refs are already ours.
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([
            completed(stdout=probe),
            completed(stdout="", stderr="daemon down", returncode=1),  # census blows up
            completed(stdout=probe),
        ])
        census, refs = discover_agentsview(runner, agent="all", project=None, since=None, limit=500)

        self.assertIsNotNone(census.unavailable_reason)
        assert census.unavailable_reason is not None
        self.assertIn("daemon down", census.unavailable_reason)
        self.assertEqual(census.totals, {})
        self.assertEqual([r.session_id for r in refs], ["s1"])

    def test_iter_agentsview_sessions_takes_no_census(self) -> None:
        # The back-compat wrapper stays census-free: callers that only want refs must
        # not pay for denominators they will not render.
        probe = _page({"id": "s1", "agent": "claude", "project": "p"})
        runner = FakeRunner([completed(stdout=probe), completed(stdout=probe)])

        refs = list(iter_agentsview_sessions(runner=runner))

        self.assertEqual([r.session_id for r in refs], ["s1"])
        self.assertEqual(len(runner.calls), 2)  # probe + full listing, nothing else
```

Add `discover_agentsview` and `AgentCensus` to the `toolbench.sources` import block at the top of the file (currently lines 14-29).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sources.py -k "AgentCensus or census" -q`
Expected: FAIL — `ImportError: cannot import name 'discover_agentsview' from 'toolbench.sources'`

- [ ] **Step 3: Add `AgentCensus` to `sources.py`**

Insert after the `SkipRecord` dataclass (after line 102):

```python
@dataclass(frozen=True)
class AgentCensus:
    """Per-agent archive population, measured at discovery (TB-33).

    Discovery-grain: the reducer counts CALLS, and a denominator is not a call, so this
    never enters `reducer.py`.

    `totals` and `archive_total` are gathered under THIS RUN'S filters. A denominator
    gathered under different filters describes a different population than the numerator,
    and the fraction becomes a lie with a decimal point on it -- the same invariant the
    TB-31 parent probe carries, which is why every census call is built by `_list_argv`
    rather than hand-assembled.

    `unavailable_reason` types the ABSENCE of a denominator (a failed census call, or a
    frozen-corpus replay that recorded none) rather than signalling it with an empty dict.
    The report can then say WHY it cannot disclose a fraction instead of quietly dropping
    the column -- which is the exact sin this ticket exists to close. Same habit as
    `SkipReason` and `UsageProvenance`: type the absence, never imply it.
    """

    totals: dict[str, int]
    archive_total: int
    unavailable_reason: str | None = None

    @property
    def residual(self) -> int:
        """Archive sessions belonging to no agent we enumerated.

        The probe listing excludes children, so the agent universe it yields is "agents
        with >= 1 non-child session"; an agent whose sessions are ALL children is
        invisible to it. Hardcoding a known-agent list to close that hole would rebuild
        the TB-30 failure mode one layer up -- a NEW agent would then silently vanish. So
        we reconcile and name what is left over instead (TB-21/TB-28: report the gap,
        never a silent zero). Zero on the live archive today; the net exists for the day
        it is not.
        """
        return self.archive_total - sum(self.totals.values())
```

- [ ] **Step 4: Factor the argv builder and retask the probe pass**

Replace `_agentsview_pages` (lines 172-202) with the builder plus the paginator:

```python
def _list_argv(
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
    includes: tuple[str, ...],
    cursor: str | None = None,
) -> list[str]:
    """The one place a `session list` argv is built.

    Sole builder BY DESIGN (TB-33): the census denominators and the discovery numerators
    must carry identical filters or they describe different populations. Routing both
    through here makes that invariant structural instead of a comment two functions apart.
    """
    argv = ["agentsview", "session", "list", "--json", "--limit", str(limit), *includes]
    if agent != "all":
        argv += ["--agent", agent]
    if project is not None:
        argv += ["--project", project]
    if since is not None:
        argv += ["--date-from", since]
    if cursor:
        argv += ["--cursor", cursor]
    return argv


def _agentsview_pages(
    runner: Runner,
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
    includes: tuple[str, ...],
) -> Iterator[tuple[Any, str]]:
    """Yield `(payload, stderr)` for each cursor page of one `session list` pass."""
    cursor: str | None = None
    while True:
        result = runner(
            _list_argv(
                agent=agent,
                project=project,
                since=since,
                limit=limit,
                includes=includes,
                cursor=cursor,
            )
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"agentsview session list failed ({result.returncode}): {result.stderr.strip()}"
            )
        payload = json.loads(result.stdout)
        yield payload, result.stderr
        cursor = payload.get("next_cursor") or None
        if not cursor:
            break


def _probe_pass(
    runner: Runner,
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
) -> tuple[set[str], set[str]]:
    """One drain of the child-excluded listing -> `(parent_ids, agents_seen)`.

    This pass ALREADY ran -- TB-31 needs `parent_ids` to classify children -- and it threw
    the agent names on the floor. Returning them is what makes the TB-33 census cost zero
    extra pagination.
    """
    parent_ids: set[str] = set()
    agents_seen: set[str] = set()
    for payload, _ in _agentsview_pages(
        runner, agent=agent, project=project, since=since, limit=limit, includes=_PROBE_INCLUDES
    ):
        for entry in payload.get("sessions", []):
            parent_ids.add(entry["id"])
            agents_seen.add(entry["agent"])
    return parent_ids, agents_seen


def _list_total(
    runner: Runner, *, agent: str, project: str | None, since: str | None
) -> int:
    """The `total` for one scoped listing. `--limit 1` because we want the COUNT, not rows."""
    result = runner(
        _list_argv(agent=agent, project=project, since=since, limit=1, includes=_ALL_INCLUDES)
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agentsview session list failed ({result.returncode}): {result.stderr.strip()}"
        )
    total = json.loads(result.stdout).get("total")
    if not isinstance(total, int):
        raise RuntimeError(f"agentsview session list returned no usable `total`: {total!r}")
    return total


def _agent_census(
    runner: Runner,
    agents_seen: set[str],
    *,
    agent: str,
    project: str | None,
    since: str | None,
) -> AgentCensus:
    """One scoped `--limit 1` per agent, plus the run-scoped archive total (TB-33).

    `archive_total` inherits the run's `--agent`, and that is load-bearing: under
    `--agent codex` the run's population IS codex, so an UNSCOPED archive total would
    compute a residual of every other agent's sessions and scream about thousands of
    "unenumerated" sessions that were never in scope.
    """
    totals = {
        a: _list_total(runner, agent=a, project=project, since=since) for a in sorted(agents_seen)
    }
    archive_total = _list_total(runner, agent=agent, project=project, since=since)
    return AgentCensus(totals=totals, archive_total=archive_total)
```

- [ ] **Step 5: Split discovery into `discover_agentsview` + `_yield_refs`**

Replace `iter_agentsview_sessions` (lines 205-271) with:

```python
def _yield_refs(
    runner: Runner,
    parent_ids: set[str],
    *,
    agent: str,
    project: str | None,
    since: str | None,
    limit: int,
) -> Iterator[SessionRef]:
    """The full listing, stamped with TB-31's child classification."""
    warned = False
    for payload, stderr in _agentsview_pages(
        runner, agent=agent, project=project, since=since, limit=limit, includes=_ALL_INCLUDES
    ):
        if not warned and (excluded := _EXCLUSION_BANNER.search(stderr)):
            # We opted into every exclusion AgentsView documents, so a banner here means
            # it dropped sessions we did not ask it to drop -- a new default, silently
            # shrinking the corpus. Discarding this banner is precisely how TB-30 hid for
            # as long as it did, so it never goes unsaid again.
            warned = True
            warnings.warn(
                f"agentsview excluded {excluded.group(1)} sessions from the corpus despite "
                f"--include-children/--include-automated/--include-one-shot; the benchmark "
                f"population is incomplete: {stderr.strip()}",
                AgentsViewExclusionWarning,
                stacklevel=2,
            )
        for entry in payload.get("sessions", []):
            yield SessionRef(
                agent=entry["agent"],
                source="agentsview",
                project=entry["project"],
                session_id=entry["id"],
                path=None,
                is_subagent=entry["id"] not in parent_ids,
            )


def discover_agentsview(
    runner: Runner,
    *,
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> tuple[AgentCensus, Iterator[SessionRef]]:
    """Census + refs (TB-30, TB-31, TB-33).

    Three questions, and AgentsView is the only thing that can answer any of them.

    WHAT WE ARE ALLOWED TO SEE (TB-30). AgentsView excludes one-shot, automated, and child
    sessions BY DEFAULT. Omitting the three `--include-*` flags cost 70% of the live
    archive, and -- fatally for a benchmark whose whole purpose is comparing agents -- it
    cost each agent a DIFFERENT fraction. Every cross-agent number was computed over
    incomparable populations, and nothing said so.

    WHICH OF THEM ARE SUBAGENTS (TB-31). The session-list row exposes no parent/child
    field, and every field-derived predicate is wrong. So we do not guess: the parent probe
    repeats the listing with `--include-children` withheld, and anything in the full listing
    but not in the probe is a child BY AGENTSVIEW'S OWN DEFINITION.

    HOW MUCH OF EACH AGENT WE ACTUALLY LOOKED AT (TB-33). `--limit` truncates the full
    listing in RECENCY order across the whole archive, so each agent lands at a wildly
    different fraction of its own history -- and an agent whose work is all older than the
    window disappears from the report with no note at all. The census is the denominator
    that makes the rendered rows comparable, and the roll-call that makes absence sayable.

    The census is computed EAGERLY, before the caller consumes a single ref: the caller
    breaks out of the ref loop early precisely when `--limit` is set, so a census gathered
    lazily during iteration would be missing exactly when it is needed most. A generator
    cannot both `return` a value and `yield`, which is why this is not one.
    """
    parent_ids, agents_seen = _probe_pass(
        runner, agent=agent, project=project, since=since, limit=limit
    )
    try:
        census = _agent_census(
            runner, agents_seen, agent=agent, project=project, since=since
        )
    except (RuntimeError, ValueError) as exc:
        # A census we cannot take is disclosed as UNKNOWN, never dropped -- a quietly
        # missing column is the sin this ticket exists to close. Discovery itself is
        # unaffected: the refs are already ours. (json.JSONDecodeError subclasses
        # ValueError, so a garbled payload lands here too.)
        census = AgentCensus(totals={}, archive_total=0, unavailable_reason=str(exc))
    refs = _yield_refs(
        runner, parent_ids, agent=agent, project=project, since=since, limit=limit
    )
    return census, refs


def iter_agentsview_sessions(
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    runner: Runner = _run_agentsview,
) -> Iterator[SessionRef]:
    """Refs only, no census (S8). Retained for callers that render no denominators, so
    they do not pay for the scoped `total` calls they would never use."""
    parent_ids, _agents_seen = _probe_pass(
        runner, agent=agent, project=project, since=since, limit=limit
    )
    return _yield_refs(
        runner, parent_ids, agent=agent, project=project, since=since, limit=limit
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_sources.py -q`
Expected: PASS. The existing `IterAgentsviewSessionsTests` must still pass untouched — the back-compat wrapper makes no census calls, so their scripted `FakeRunner` queues are unchanged.

- [ ] **Step 7: Commit**

```bash
git add toolbench/sources.py tests/test_sources.py
git commit -m "feat(tb-33): AgentCensus — per-agent archive denominators at discovery

Retasks the TB-31 parent-probe pass, which already drained the index and threw the
agent names away, to also return the agent universe. One scoped --limit 1 call per
agent reads its exact total; the run-scoped archive total reconciles them, so an agent
the probe could not see (all-children) is named as a residual rather than assumed away.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Raw-path census and the `iter_sessions` seam

**Files:**
- Modify: `toolbench/sources.py` (`iter_sessions`, new `_raw_census`)
- Modify: `tests/test_sources.py` (existing `iter_sessions` call sites now unpack 3)

**Interfaces:**
- Consumes: `AgentCensus`, `discover_agentsview`, `iter_session_files` from Task 1.
- Produces: `iter_sessions(...) -> tuple[Iterator[SessionRef], str | None, AgentCensus]` — a **3-tuple**; every caller must be updated. Also `RAW_AGENT = "claude-code"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sources.py`:

```python
class RawCensusTests(unittest.TestCase):
    """`--limit` truncates the raw path too, and MORE arbitrarily (TB-33)."""

    def test_raw_census_counts_every_discoverable_file(self) -> None:
        with TemporaryDirectory() as tmp:
            for name in ("a", "b", "c"):
                proj = Path(tmp) / "proj"
                proj.mkdir(exist_ok=True)
                (proj / f"{name}.jsonl").write_text("{}\n")

            _refs, _reason, census = iter_sessions(
                index_source="raw", root=tmp, runner=FakeRunner([])
            )

            # iter_session_files sorts by PATH, so --limit takes an alphabetical slice of
            # the project tree -- not even a recency window. One agent, so no cross-agent
            # skew; but "you scanned 1 of 3" still has to be sayable.
            self.assertEqual(census.totals, {"claude-code": 3})
            self.assertEqual(census.archive_total, 3)
            self.assertEqual(census.residual, 0)
            self.assertIsNone(census.unavailable_reason)

    def test_raw_census_on_a_missing_root_is_unavailable_not_a_crash(self) -> None:
        _refs, _reason, census = iter_sessions(
            index_source="raw", root="/nonexistent/root", runner=FakeRunner([])
        )

        self.assertIsNotNone(census.unavailable_reason)
        self.assertEqual(census.totals, {})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sources.py -k RawCensus -q`
Expected: FAIL — `ValueError: too many values to unpack (expected 2)`, because `iter_sessions` still returns a 2-tuple.

- [ ] **Step 3: Add `_raw_census` and widen `iter_sessions`**

In `sources.py`, add near `_raw_session_refs`:

```python
# The only agent the raw filesystem path can discover; `_raw_session_refs` stamps it.
RAW_AGENT = "claude-code"


def _raw_census(root: str, project: str | None, since: str | None) -> AgentCensus:
    """Denominator for the raw path: a filesystem count, no subprocess (TB-33).

    A missing root is an UNAVAILABLE census, not an exception: `iter_sessions` is called
    eagerly, and the `auto` path reaches here precisely when AgentsView is down and the
    raw root may not exist either. `_discover_refs` still surfaces the FileNotFoundError
    from the ref iterator as a MISSING_SOURCE skip -- this must not pre-empt it.
    """
    try:
        count = sum(1 for _ in iter_session_files(root=root, project=project, since=since))
    except FileNotFoundError as exc:
        return AgentCensus(totals={}, archive_total=0, unavailable_reason=str(exc))
    return AgentCensus(totals={RAW_AGENT: count}, archive_total=count)
```

Replace `iter_sessions` (lines 373-399) with:

```python
def iter_sessions(
    index_source: IndexSource = "auto",
    agent: str = "all",
    project: str | None = None,
    since: str | None = None,
    limit: int = 500,
    root: str = "~/.claude/projects",
    runner: Runner | None = None,
) -> tuple[Iterator[SessionRef], str | None, AgentCensus]:
    """Resolve the `--index-source` policy; return (refs, fallback_reason, census) (S10).

    The census rides along rather than being fetched separately so it cannot drift from
    the refs: same source, same filters, same call (TB-33).
    """
    run = runner if runner is not None else _run_agentsview
    if index_source == "raw":
        return (
            _raw_session_refs(root, project, since),
            None,
            _raw_census(root, project, since),
        )
    if index_source == "agentsview":
        census, refs = discover_agentsview(
            run, agent=agent, project=project, since=since, limit=limit
        )
        return refs, None, census
    if index_source == "auto":
        reason = _probe_agentsview(run)
        if reason is None:
            census, refs = discover_agentsview(
                run, agent=agent, project=project, since=since, limit=limit
            )
            return refs, None, census
        return (
            _raw_session_refs(root, project, since),
            reason,
            _raw_census(root, project, since),
        )
    raise ValueError(f"unknown index_source: {index_source!r}")
```

- [ ] **Step 4: Update the existing `iter_sessions` call sites in the tests**

Six call sites in `tests/test_sources.py` unpack a 2-tuple (lines ~286, 308, 331, 348, 361, 374). Change each `refs, reason = iter_sessions(...)` to `refs, reason, _census = iter_sessions(...)`.

One of them (~line 348) drives the **agentsview auto path** with `FakeRunner([completed(stdout=json.dumps(payload))] * 3)` — the availability probe, the parent probe, and the full listing. That path now also makes census calls, in this order:

```
[0] _probe_agentsview   (session list --limit 1)
[1] _probe_pass         (child-excluded listing)
[2..] _agent_census     (one --limit 1 per agent seen, sorted, then the archive total)
[N] _yield_refs         (full listing)
```

Give it enough responses. If `payload` lists one agent, that is **5** responses, not 3. Extend the multiplier and assert the shape rather than guessing:

```python
runner = FakeRunner([completed(stdout=json.dumps(payload))] * 5)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_sources.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add toolbench/sources.py tests/test_sources.py
git commit -m "feat(tb-33): raw-path census; iter_sessions carries the census

The raw path truncates under --limit too, and more arbitrarily than AgentsView:
iter_session_files sorts by PATH, so the window is an alphabetical slice of the project
tree rather than a recency window. Denominator is a free filesystem count.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Render the disclosure in `report.py`

**Files:**
- Modify: `toolbench/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `AgentCensus` (Task 1), `Reducer`, `AgentStats` from `toolbench.reducer`.
- Produces: `render_report(..., census: AgentCensus)` — a **required keyword arg**; `SPREAD_THRESHOLD = 4.0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_report.py` (import `AgentCensus` from `toolbench.sources`, `AgentStats`/`Reducer` from `toolbench.reducer`):

```python
def _reducer_with(**sessions_by_agent: int) -> Reducer:
    r = Reducer()
    for agent, n in sessions_by_agent.items():
        r.agents[agent] = AgentStats(sessions=n, calls=n * 10)
    r.calls_joined = sum(n * 10 for n in sessions_by_agent.values())
    return r


def _render(reducer: Reducer, census: AgentCensus) -> str:
    return render_report(
        reducer,
        index_source="agentsview",
        fallback_reason=None,
        skips=[],
        include_subagents=True,
        subagents_found=0,
        sessions_discovered=sum(s.sessions for s in reducer.agents.values()),
        since_note=None,
        census=census,
    )


class SamplingDisclosureTests(unittest.TestCase):
    """The Agent Breakdown must never render an incomparable table in silence (TB-33)."""

    def test_unreached_agent_gets_a_named_row(self) -> None:
        # cursor is in the archive with 73 sessions and was never scanned. A four-agent
        # table that simply omits it is the bug.
        reducer = _reducer_with(claude=135)
        census = AgentCensus(totals={"claude": 8595, "cursor": 73}, archive_total=8668)

        out = _render(reducer, census)

        self.assertIn("| cursor |", out)
        self.assertIn("0 of 73", out)
        self.assertIn("135 of 8595", out)
        # Absence is STATED, never inferred from a zero.
        self.assertIn("not reached", out.lower())

    def test_uneven_sampling_line_fires_above_threshold(self) -> None:
        # codex 40/183 = 21.9%; claude 135/8595 = 1.6%. Spread ~13.9x.
        reducer = _reducer_with(claude=135, codex=40)
        census = AgentCensus(totals={"claude": 8595, "codex": 183}, archive_total=8778)

        out = _render(reducer, census)

        self.assertIn("Sampling is uneven", out)
        self.assertIn("not comparable", out)

    def test_even_sampling_emits_no_warning_line(self) -> None:
        # Both at ~1.6%: the table IS comparable, so say nothing.
        reducer = _reducer_with(claude=100, codex=10)
        census = AgentCensus(totals={"claude": 6250, "codex": 625}, archive_total=6875)

        out = _render(reducer, census)

        self.assertNotIn("Sampling is uneven", out)

    def test_residual_is_named(self) -> None:
        reducer = _reducer_with(claude=100)
        census = AgentCensus(totals={"claude": 8595}, archive_total=8700)

        out = _render(reducer, census)

        self.assertIn("105", out)
        self.assertIn("belong to no agent", out)

    def test_unavailable_census_says_why_and_renders_unknown(self) -> None:
        reducer = _reducer_with(claude=100)
        census = AgentCensus(
            totals={}, archive_total=0, unavailable_reason="agentsview exited 1: daemon down"
        )

        out = _render(reducer, census)

        self.assertIn("unknown", out)
        self.assertIn("daemon down", out)
        # A dropped column would be the exact sin this ticket closes.
        self.assertIn("| claude |", out)

    def test_summary_lists_every_agents_sampling_fraction(self) -> None:
        reducer = _reducer_with(claude=135)
        census = AgentCensus(totals={"claude": 8595, "cursor": 73}, archive_total=8668)

        out = _render(reducer, census)
        summary = out.split("## Summary")[1]

        self.assertIn("claude: 135 of 8595", summary)
        self.assertIn("cursor: 0 of 73", summary)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_report.py -k Sampling -q`
Expected: FAIL — `TypeError: render_report() got an unexpected keyword argument 'census'`

- [ ] **Step 3: Implement the rendering helpers**

In `report.py`, extend the imports and add the helpers above `render_report`:

```python
from toolbench.reducer import OVERSIZED_OUTPUT_TOKENS, AgentStats, Reducer
from toolbench.sources import AgentCensus, SkipReason, SkipRecord

# Ratio of the largest per-agent sampling fraction to the smallest, above which
# cross-agent numbers stop being comparable and the report says so. Arbitrary but
# STATED -- and it can only fire when a `--limit` actually truncated the corpus, so it
# never nags a full run.
SPREAD_THRESHOLD = 4.0


def _sampled_cell(scanned: int, total: int | None, unavailable: bool) -> str:
    """One agent's `sampled` cell: the numerator, its denominator, and the fraction."""
    if unavailable:
        return "unknown"
    if total is None:
        # Scanned, but absent from the census universe -- i.e. an agent the child-excluded
        # probe never saw. `residual` names it in aggregate; this names it in place.
        return f"{scanned} of unknown"
    if total == 0:
        return "0 of 0"
    return f"{scanned} of {total} ({scanned / total * 100:.1f}%)"


def _sampling_spread(reducer: Reducer, census: AgentCensus) -> float | None:
    """max/min sampling fraction across agents with >= 1 scanned session.

    Agents with zero scanned sessions are excluded: their fraction is 0, which would send
    the ratio to infinity and drown the real signal. They are disclosed by name instead.
    """
    fractions = [
        stats.sessions / total
        for agent, stats in reducer.agents.items()
        if stats.sessions and (total := census.totals.get(agent))
    ]
    if len(fractions) < 2:
        return None
    return max(fractions) / min(fractions)


def _sampling_notes(reducer: Reducer, census: AgentCensus) -> list[str]:
    """Disclosure that belongs BESIDE the table, not forty lines below it (TB-33).

    A reader forming a calls/session ratio across two rows never scrolls to the Summary,
    so the qualification has to sit where the comparison is made.
    """
    if census.unavailable_reason is not None:
        return [
            f"- Sampling fractions unavailable: {census.unavailable_reason}. Each row above "
            "may rest on a different fraction of its agent's archive; this run cannot say."
        ]

    notes: list[str] = []
    unreached = sorted(
        agent
        for agent, total in census.totals.items()
        if total > 0 and reducer.agents.get(agent, AgentStats()).sessions == 0
    )
    if unreached:
        named = ", ".join(f"{a} ({census.totals[a]} sessions)" for a in unreached)
        notes.append(
            f"- Present in the archive, not reached by this window: {named}. Their rows are "
            "zeros because we did not look, not because they did no work."
        )

    spread = _sampling_spread(reducer, census)
    if spread is not None and spread >= SPREAD_THRESHOLD:
        notes.append(
            f"- **Sampling is uneven ({spread:.1f}x spread).** Each row is a different "
            "fraction of a different-sized population, so any ratio formed ACROSS rows "
            "(calls/session, tokens/call, error rate) mixes sampling depth into the "
            "comparison and is not comparable. Re-run without --limit for a like-for-like "
            "table."
        )

    if census.residual > 0:
        notes.append(
            f"- Reconciliation: {census.residual} archive sessions belong to no agent we "
            "enumerated. The census universe comes from the child-excluded probe listing, "
            "so an agent whose sessions are ALL children is invisible to it -- the "
            "denominators above are incomplete."
        )
    return notes
```

- [ ] **Step 4: Wire the column and the Summary block into `render_report`**

Add `census: AgentCensus` to the keyword-only params. Replace the Agent Breakdown block (lines 124-144) with:

```python
    lines.append("## Agent Breakdown")
    lines.append("")
    lines.append(
        "| agent | sampled | sessions | calls | output_tokens | input_tokens | errors | no_result |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    cache_caveats: list[str] = []
    # Union, not `reducer.agents`: an agent the window never reached has no AgentStats at
    # all, and dropping its row is the headline bug (TB-33).
    for agent in sorted(set(reducer.agents) | set(census.totals)):
        s = reducer.agents.get(agent, AgentStats())
        sampled = _sampled_cell(
            s.sessions, census.totals.get(agent), census.unavailable_reason is not None
        )
        lines.append(
            f"| {agent} | {sampled} | {s.sessions} | {s.calls} | {s.output_tokens} | "
            f"{s.input_tokens} | {s.errors} | {s.no_result} |"
        )
        if s.sessions_with_cache_data > 0:
            # S32: session-grain only, orthogonal to the per-call `cache_assisted` column
            # below -- never mixed into that column, never a sixth section.
            cache_caveats.append(
                f"- {agent}: {s.sessions_with_cache_hit} of {s.sessions_with_cache_data} "
                "sessions carry session-grain `cache_read_tokens` > 0 "
                "(S32: session grain only — not attributable to individual tool calls)."
            )
    lines.extend(cache_caveats)
    lines.extend(_sampling_notes(reducer, census))
    lines.append("")
```

In the Summary section, immediately after the `Sessions discovered` line (currently lines 223-225), add:

```python
    if census.unavailable_reason is None and census.totals:
        lines.append("- Sampling (scanned of each agent's own archive):")
        for agent in sorted(census.totals):
            total = census.totals[agent]
            scanned = reducer.agents.get(agent, AgentStats()).sessions
            pct = f"{scanned / total * 100:.1f}%" if total else "n/a"
            tail = " — not reached by this window" if scanned == 0 else ""
            lines.append(f"  - {agent}: {scanned} of {total} ({pct}){tail}")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_report.py -q`
Expected: PASS. Other `render_report` call sites in `tests/test_report.py` now need `census=AgentCensus(totals={}, archive_total=0)`; add it to each.

- [ ] **Step 6: Commit**

```bash
git add toolbench/report.py tests/test_report.py
git commit -m "feat(tb-33): render sampling fractions, name unreached agents

Agent Breakdown gains a \`sampled\` column and iterates the UNION of scanned agents and
census agents, so an agent the --limit window never reached gets a named row instead of
silent absence. An uneven-sampling line fires at >=4x spread -- as a report line, not a
warnings.warn(), because stderr is lost the moment the report is redirected to a file,
which is how TB-30 hid.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire it through `passive.py`, including freeze replay

**Files:**
- Modify: `toolbench/passive.py`
- Test: `tests/test_passive.py`

**Interfaces:**
- Consumes: `iter_sessions` 3-tuple (Task 2), `render_report(census=...)` (Task 3).
- Produces: `_discover_refs(...) -> tuple[list[SessionRef], str | None, list[SkipRecord], AgentCensus]` — a **4-tuple**.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_passive.py`:

```python
class FreezeReplayCensusTests(unittest.TestCase):
    """A frozen corpus bypasses discovery, so it has no denominator -- say so (TB-33)."""

    def test_replay_discloses_that_sampling_is_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects" / "proj"
            root.mkdir(parents=True)
            (root / "s1.jsonl").write_text("{}\n")
            manifest = str(Path(tmp) / "freeze.json")

            argv = ["--index-source", "raw", "--all", "--freeze", manifest]
            # First run discovers and writes the manifest.
            main(argv, root=str(Path(tmp) / "projects"))
            # Second run replays it -- discovery, and therefore the census, is bypassed.
            out = io.StringIO()
            with redirect_stdout(out):
                main(argv, root=str(Path(tmp) / "projects"))

            report = out.getvalue()
            self.assertIn("Sampling fractions unavailable", report)
            self.assertIn("frozen corpus", report)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_passive.py -k FreezeReplayCensus -q`
Expected: FAIL — `ValueError: too many values to unpack`, because `_discover_refs` still returns a 3-tuple.

- [ ] **Step 3: Widen `_discover_refs`**

In `passive.py`, import `AgentCensus` from `toolbench.sources`. Change `_discover_refs` (lines 232-269): its return annotation becomes
`tuple[list[SessionRef], str | None, list[SkipRecord], AgentCensus]`, the `iter_sessions` call unpacks three values, and both `return` paths carry the census:

```python
    refs_iter, fallback_reason, census = iter_sessions(
        index_source=args.index_source,
        agent=args.agent,
        project=project,
        since=args.since,
        limit=page_limit,
        root=root,
        runner=runner,
    )
```

and the final line becomes `return refs, fallback_reason, skips, census`.

- [ ] **Step 4: Set the replay census and pass it to the report**

In `main`, replace the discovery/replay block (lines 298-315):

```python
    refs: list[SessionRef]
    fallback_reason: str | None
    skips: list[SkipRecord]
    census: AgentCensus
    if replaying:
        assert freeze_path is not None
        manifest = read_manifest(freeze_path)
        refs, fallback_reason, skips = manifest.refs, None, []
        # A freeze pins the REF LIST, not the archive it was drawn from (TB-22), so no
        # denominator exists on replay. Persisting one into the manifest would be a
        # format change this ticket does not own -- and an unstated "unknown" is exactly
        # the silence TB-33 exists to break, so it is stated instead.
        census = AgentCensus(
            totals={},
            archive_total=0,
            unavailable_reason=(
                f"frozen corpus replay ({freeze_path}): no denominator was recorded at "
                "freeze time"
            ),
        )
    else:
        try:
            refs, fallback_reason, skips, census = _discover_refs(args, root, runner)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"toolbench.passive: fatal source error: {exc}", file=sys.stderr)
            return 1
        if freeze_path is not None:
            write_manifest(
                freeze_path, refs, corpus_fingerprint(r.session_id for r in refs).digest
            )
```

Add `census=census,` to the `render_report(...)` call (currently lines 392-405).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Any remaining `render_report` or `iter_sessions` call site in `tests/` that still unpacks the old arity fails loudly here — fix each by passing `census=AgentCensus(totals={}, archive_total=0)` or unpacking the extra value.

- [ ] **Step 6: Commit**

```bash
git add toolbench/passive.py tests/test_passive.py
git commit -m "feat(tb-33): wire the census through passive; disclose it on freeze replay

A freeze pins the ref list, not the archive it came from, so replay has no denominator.
Rather than silently dropping the column, replay renders 'unknown' and names the reason.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Verify against the live archive, run the gate, close the ticket

**Files:**
- Modify: `AGENTS.md` (one bullet under the AgentsView notes)

- [ ] **Step 1: Run the gate**

```bash
uv run ruff check .
uv run mypy --strict toolbench tests
uv run pytest -q
```
Expected: clean on all three. `mypy --strict` is the one that catches a missed call-site arity change.

- [ ] **Step 2: Verify against the real archive — the numbers the ticket measured**

```bash
uv run python -m toolbench.passive --agent all --all --limit 200 --index-source agentsview --out /tmp/tb33.md
grep -A 14 "## Agent Breakdown" /tmp/tb33.md
```

Expected: **eight** agent rows (`antigravity`, `claude`, `claude-ai`, `codex`, `cowork`, `cursor`, `hermes`, `warp`), not five. `claude-ai` and `cursor` and `antigravity` each carry `0 of N` with a `not reached` note. The uneven-sampling line fires (measured spread was 41.4x). The residual line is **absent**, because the live residual is 0.

This is the falsifiable claim the whole ticket rests on: before the fix this table has five rows and no denominators.

- [ ] **Step 3: Confirm an unlimited run still reconciles**

```bash
uv run python -m toolbench.passive --agent codex --all --index-source agentsview --out /tmp/tb33-codex.md
grep "of 183" /tmp/tb33-codex.md
```

Expected: codex's `sampled` cell reads `N of 183 (X%)` where N is close to but may be below 183 — the shortfall is *skipped* sessions, which is the per-agent skip attrition the column now discloses for free. No residual line (scoped runs reconcile to 0).

- [ ] **Step 4: Document the flag in `AGENTS.md`**

Add under the existing AgentsView bullets:

```markdown
- **Sampling disclosure (TB-33):** `--limit` caps total refs in RECENCY order across the
  whole archive, so each agent lands at a different fraction of its own history and an
  agent whose work is all older than the window vanishes entirely. The Agent Breakdown's
  `sampled` column carries each agent's denominator; agents present in the archive but
  never scanned still get a row. Cross-agent ratios are only comparable when the report
  emits no uneven-sampling line. A frozen corpus (`--freeze`) has no denominator and
  says so.
```

- [ ] **Step 5: Commit, push, open the PR**

```bash
git add AGENTS.md
git commit -m "docs(tb-33): note the sampling disclosure in AGENTS.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push -u origin fix/tb-33-agent-sampling-disclosure
```

Then open the PR with `gh pr create`, and move the ticket:

```bash
lattice status TB-33 review --actor agent:Vulcan-1
```

---

## Self-Review

**Spec coverage.** §1 `AgentCensus` → Task 1. §2 acquisition + reconciliation → Task 1. §3 `discover_agentsview` restructure → Task 1. §4 raw path → Task 2. §5 rendering (column, spread line, Summary) → Task 3. §6 census-failure and freeze-replay disclosure → Tasks 1 and 4. Test plan → distributed across Tasks 1-4, with the `FakeRunner` ordering migration in Task 2 Step 4. Live verification → Task 5.

**Deviations from the spec, deliberate:**
1. Spec said `census: AgentCensus | None`. The plan uses a required `AgentCensus` carrying `unavailable_reason: str | None`. `None` would force a fourth tuple member to carry *why* the census is missing, and losing the why is precisely how a dropped column becomes silent. Typing the absence is the codebase's existing habit (`SkipReason`, `UsageProvenance`).
2. Spec implied an unscoped `archive_total`. The plan scopes it to the run's `--agent`, because under `--agent codex` an unscoped total would report a residual of every other agent's sessions — thousands of "unenumerated" sessions that were never in scope.
3. `iter_agentsview_sessions` is kept **census-free** rather than delegating to `discover_agentsview`. This costs nothing, and it keeps the existing `IterAgentsviewSessionsTests` FakeRunner queues valid without modification — the migration burden lands only on `iter_sessions` call sites.

**Type consistency.** `AgentCensus` field names (`totals`, `archive_total`, `unavailable_reason`, `.residual`) are used identically in Tasks 1-4. `iter_sessions` returns a 3-tuple everywhere; `_discover_refs` a 4-tuple. `render_report` takes `census=` as a required keyword in Tasks 3 and 4. `RAW_AGENT` matches the `agent="claude-code"` that `_raw_session_refs` already stamps.
