# TB-33 — Disclose per-agent sampling fractions; never drop an unreached agent

**Status:** approved (2026-07-13)
**Ticket:** TB-33 (high, bug) — spawned by TB-30
**Files:** `toolbench/sources.py`, `toolbench/report.py`, `toolbench/passive.py`, `tests/*`

## Problem

`--limit N` caps the number of discovered refs (`passive.py:253`), and the AgentsView
listing returns sessions in **recency order across the whole archive**. Agents do not
produce sessions at the same rate, so the window's per-agent composition bears no
relation to each agent's share of the archive. Each agent ends up sampled at a wildly
different fraction of its own history, and the report says nothing about it.

Two distinct failures follow:

1. **The Agent Breakdown is not comparable as rendered.** Rows sit side by side as if
   like-for-like, but each is a different sampling fraction of a different-sized
   population. Any ratio a reader forms across rows — calls/session, tokens/call, error
   rate — silently mixes sampling depth into the comparison.

2. **An agent can vanish with no note.** An agent whose sessions all fall outside the
   recency window is simply absent from the table. Nothing distinguishes *"this agent has
   no sessions"* from *"this agent has sessions we did not look at."*

This is the same failure **mode** as TB-30 (a hidden, per-agent-uneven sampling skew) from
a different **cause**: TB-30 was a hidden default exclusion, this is visible truncation
that is nonetheless never surfaced.

### Measured severity (live archive, 2026-07-13)

The ticket understated the bug. At `--limit 200` the report renders **5 of 8 agents**:

| agent | scanned | archive | fraction | |
|---|---|---|---|---|
| claude | 135 | 8595 | 1.57% | |
| **claude-ai** | **0** | **1150** | **0.00%** | **absent from report** |
| codex | 40 | 183 | 21.86% | |
| cowork | 5 | 946 | 0.53% | |
| **cursor** | **0** | **73** | **0.00%** | **absent from report** |
| hermes | 19 | 978 | 1.94% | |
| warp | 1 | 49 | 2.04% | |
| **antigravity** | **0** | **2** | **0.00%** | **absent from report** |

The ticket caught `cursor` (73 sessions). It missed **`claude-ai`: 1,150 sessions, 9.6% of
the entire archive, wholly invisible.** True spread among *present* agents is **41.4×**
(codex 21.9% vs cowork 0.53%), not the 11.5× the ticket estimated.

Whether an agent appears at all is a coin-flip on recency, not a fact about the archive:
`warp` (49 sessions) survives because it happened to run once inside the window;
`claude-ai` (1,150 sessions) vanishes because all of its work is older. **Presence is an
accident; absence must therefore be stated, never inferred.**

## Non-goals

- Changing what `--limit` selects. Recency-ordered truncation stays; this ticket makes it
  **disclosed**, not different. A sampling strategy that is fair across agents
  (e.g. stratified per-agent quotas) is a separate ticket if ever wanted.
- Ranking, scoring, or filtering on the sampling fraction. It is disclosure only.

## Design

### 1. Data — `AgentCensus` (discovery-grain)

New frozen dataclass in `sources.py`. It is **discovery-grain**, so it never enters
`reducer.py` — the reducer counts calls, and a denominator is not a call.

```python
@dataclass(frozen=True)
class AgentCensus:
    totals: dict[str, int]   # agent -> sessions in the archive UNDER THIS RUN'S FILTERS
    archive_total: int       # unscoped total under the same filters

    @property
    def residual(self) -> int:
        """archive_total - sum(totals). > 0 means an agent exists that we never
        enumerated -- the one hole in the probe-derived universe (see §2)."""
```

**Load-bearing invariant** (inherited from the TB-31 parent probe): *the census must carry
identical `--project` / `--since` filters as discovery.* A denominator gathered under
different filters describes a different population than the numerator, and the fraction
becomes a lie with a decimal point on it.

**The census must also inherit `--exclude-subagents`** — a hole this spec originally
missed, found by the final whole-branch review and proven with a live repro. `filter_subagents`
runs *after* discovery, so under that flag the numerator counts **parents only** while a
naive denominator (`_ALL_INCLUDES`) counts **parents + children**. Different populations.
The report then mis-discloses rather than merely failing to disclose: with **no `--limit` at
all**, an agent whose archive is mostly children renders `1 of 10 (10.0%)` and fires
*"Sampling is uneven (10.0x spread) … Re-run without --limit"* — declaring a perfectly
comparable table incomparable and prescribing a remedy that changes nothing. Under `--limit`
it is worse: it reports `0 of 5 — "zeros because we did not look"` for sessions it *did* look
at and deliberately excluded, contradicting its own `Subagents included: no (4 of 5 …)` line
two rows down. A confidently wrong column is worse than no column.

The fix is structural, not a special case: `filter_subagents` keeps exactly the refs whose
ids are in `parent_ids` — which is exactly the `_PROBE_INCLUDES` listing. So under
`--exclude-subagents` the census gathers both the per-agent totals and `archive_total` with
`_PROBE_INCLUDES`, and the denominator becomes exactly right rather than approximately
wrong. `include_subagents` threads from `CliArgs` → `iter_sessions` → `discover_agentsview`
→ `_agent_census` → `_list_total`, and `_raw_census` inherits it via the path-derived
`is_subagent` flag. Verified live: claude's denominator moves 8659 → 7998 and cowork's
946 → 654 under the flag, matching what the numerator counts.

### 2. Acquisition — reuse the pass we already pay for

The parent-probe pass in `iter_agentsview_sessions` already drains the entire index to
build `parent_ids`, and **throws the agent names away**. It is retasked to return them.

```
probe pass (already paid)  -> (parent_ids, agents_seen)
  + one `session list --agent X --limit 1 --include-*` per agent  -> exact total
  + one unscoped `session list --limit 1 --include-*`             -> archive_total
  reconcile: archive_total - sum(totals) == residual
```

~9 extra subprocess calls on the live archive. No extra pagination.

**The hole, and why it is named rather than assumed away.** The probe listing excludes
children, so the agent universe it yields is *"agents with ≥1 non-child session."* An agent
whose sessions are **all** children would not appear — and hardcoding a known-agent list to
cover that would rebuild the TB-30 failure mode one layer up, since a *new* agent would
then silently vanish. Instead we reconcile against the unscoped archive total and **name
any residual**. On the live archive today the residual is **0** (sum of 8 agent totals =
11,976 = archive total), so the universe is provably complete; the net exists to catch the
day it is not. This is the house rule from TB-21 and TB-28: *report the gap, never a silent
zero.*

### 3. Structure — `discover_agentsview`

A generator cannot both `return` a value and `yield`, so acquisition splits:

```python
def discover_agentsview(runner, *, agent, project, since, limit
                        ) -> tuple[AgentCensus, Iterator[SessionRef]]:
    parent_ids, agents_seen = _probe_pass(...)     # one drain, two products
    census = _agent_census(runner, agents_seen, project=project, since=since)
    return census, _yield_refs(runner, parent_ids, ...)
```

`iter_agentsview_sessions` remains as a thin back-compat wrapper returning only the
iterator (tests and docs import it by name). `iter_sessions` grows the census as a third
tuple member: `(refs, fallback_reason, census)`.

**Resulting subprocess call order** (a contract the tests script against):
probe pages → census calls → full-listing pages.

The census must be computed **eagerly**, before the ref iterator is consumed: the caller
breaks out of the ref loop early precisely when `--limit` is set, so a census collected
lazily during iteration would be missing exactly when it is needed most.

### 4. Raw path

`--index-source raw` truncates too, and *more* arbitrarily: `iter_session_files` sorts by
**path**, not recency, so `--limit` takes an alphabetical slice of the project tree. Only
one agent (`claude-code`) is discovered there, so there is no cross-agent skew — but
"you scanned 50 of 8,000 sessions" is undisclosed today. The denominator is a free
filesystem count:

```python
def _raw_census(root, project, since) -> AgentCensus:
    n = sum(1 for _ in iter_session_files(root, project, since))
    return AgentCensus(totals={"claude-code": n}, archive_total=n)
```

### 5. Rendering (`report.py`)

`render_report` takes `census: AgentCensus | None` and iterates
`sorted(set(reducer.agents) | set(census.totals))` — so **an agent the window never reached
still gets a row.**

**Agent Breakdown gains a `sampled` column:**

```
| agent     | sampled                     | sessions | calls | output_tokens | ... |
|-----------|-----------------------------|----------|-------|---------------|-----|
| claude    | 135 of 8595 (1.6%)          |      135 |  1401 |        587033 |     |
| claude-ai | 0 of 1150 (not reached)     |        0 |     0 |             0 |     |
| codex     | 40 of 183 (21.9%)           |       40 |   369 |        721451 |     |
```

An unreached agent's cell reads `0 of 1150 (not reached by --limit)` — never a bare `0`,
which a reader would take as *"this agent did no work."*

**Spread line**, emitted below the table when max/min sampling fraction across agents with
≥1 scanned session exceeds **4×**:

> **Sampling is uneven (41.4× spread):** codex is sampled at 21.9% of its archive, cowork
> at 0.5%. Ratios formed ACROSS rows (calls/session, tokens/call, error rate) mix sampling
> depth and are not comparable. Re-run without `--limit` for a like-for-like table.

Rendered as a **report line, not a `warnings.warn()`**: a warning goes to stderr and is lost
the moment the report is redirected to a file — which is precisely how TB-30 hid. The 4×
threshold is arbitrary but stated; it only fires when a `--limit` truncated the corpus.

**Summary block** repeats the per-agent fractions, names any unreached agents explicitly,
and renders the reconciliation residual when non-zero.

### 6. Two consequences, both wanted

1. **The column is not vacuous on an unlimited run.** It renders e.g. `8000 of 8595 (93%)`,
   because *skipped* sessions (dead index entries, binary exports) also never reach the
   reducer. So the column additionally discloses **per-agent skip attrition**, which today
   exists only as a corpus-wide tally. "The corpus is complete" becomes a falsifiable claim
   rather than an asserted one — TB-31's *earned, not asserted* ethos. The Summary's
   existing skip-by-reason tally remains the place that explains *why* a session was lost;
   the column reports only *that* the agent's numbers rest on a fraction of its archive.

2. **Census failure degrades loudly.** If the scoped calls error, `census=None`, the cell
   renders `unknown`, and the Summary **names the failure**. Same on freeze replay, where a
   pinned manifest bypasses discovery and no denominator exists. A quietly-dropped column
   would be the exact sin this ticket exists to close. Freeze manifests are **not** extended
   to persist denominators — that is scope creep, and the explicit note is honest.

## Test plan

`FakeRunner` (`tests/fakes.py`) is a **strict ordered queue** that raises on exhaustion, and
census calls land *between* the probe pages and the full-listing pages. Every existing
agentsview test that scripts an exact page count therefore shifts. The repair is
centralized: the shared `_av()` helper (`tests/test_sources.py:44`) already encodes the
pass order, so teaching it to inject census responses fixes most call sites at once.
`_page()` already emits a `total` key, so the fake is already shaped for this.

**The reconciliation residual cannot catch a wrong `includes` tuple**, and this is worth
stating because the design leans on it as a safety net. `_list_total` builds *both* the
per-agent totals and `archive_total`, so if its `includes` were wrong they would move
together and `residual` would stay `0`. The final review proved it: mutating `_list_total`
to `_PROBE_INCLUDES` — re-importing the exact TB-30 bug into the denominator — left the
whole suite green. The `includes` tuple must therefore be pinned by *direct argv assertions*,
not inferred from a clean reconciliation.

Cases:

- every census argv carries all three `--include-*` flags on a default run, and omits
  **only** `--include-children` under `--exclude-subagents` (asserted on the argv itself)
- under `--exclude-subagents`, a fully-scanned corpus does NOT fire the uneven-sampling line
- an agent present in the census but with zero scanned sessions gets a row reading
  `not reached`, not a bare `0`
- `residual > 0` is named in the Summary
- spread ≥ 4× emits the uneven-sampling line; spread < 4× does not
- census failure → `unknown` cell + named reason in Summary, run still exits 0
- raw-path census counts files and reports the `claude-code` fraction
- freeze replay → `unknown` + explicit note, no crash
- census inherits `--project` / `--since` (assert the scoped argv carries the filters)
- an unlimited run renders `scanned of total` including skip attrition

Gate (from `README.md`): `uv run ruff check .`, `uv run mypy --strict toolbench tests`,
`uv run pytest -q`.
