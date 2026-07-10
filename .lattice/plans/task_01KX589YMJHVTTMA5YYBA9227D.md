# TB-20: hermes sessions report 100% cache-miss: session-grain cache_read_tokens is never consulted

## Ticket (verbatim summary)

Filed out of TB-18. `_is_cache_hit(usage)` (passive.py:181) consults only
per-call `message.usage`; hermes never carries that (SQLite path:
`ABSENT_BY_SCHEMA`; trace path: `ABSENT_BY_EXPORT`). But hermes DOES record
cache data on the `sessions` row itself: 776/863 sessions (90%) in the live
archive carry `cache_read_tokens > 0`. `messages.token_count` is NULL on all
10,177 rows — the grain is session, not message/call.

**Acceptance**: either hermes cache data reaches the report at a grain it
actually has, or the report states that session-grain cache data exists and
is deliberately not attributed per call. Must not survive: a report implying
hermes achieved a 0% cache-hit rate.

**Hard constraint** (explicit in the ticket): do NOT divide a session-grain
figure by `tool_call_count` to fabricate a per-call rate. That is the exact
class of error TB-18 exists to eliminate.

## Chosen shape

Candidate (a) from the ticket: **a session-grain caveat line, orthogonal to
the per-call cache column, never mixed in one column.**

Rejected candidate (b) — extending `UsageProvenance` with a
`PRESENT_AT_SESSION_GRAIN` member — because `UsageProvenance` is stamped
per-`ToolCall` and answers "can THIS call's usage be measured?". Session-grain
cache data cannot answer that question for any individual call (that's the
whole hard part the ticket names), so forcing it into a per-call enum would
either (a) get attributed to one arbitrary call in the session, which is the
forbidden fabrication, or (b) get stamped on every call in the session, which
overstates support N-fold in any per-call denominator. Composing with S29
without forking it means leaving `UsageProvenance` and the existing four-case
render (`yes`/`no`/`n/a`/`n/a*`) completely untouched, and adding session-grain
as new, clearly-labeled, agent-level data next to it.

## Design

1. **`toolbench/hermes.py`** — `parse_hermes_session`'s session-row query
   gains `cache_read_tokens`:
   `SELECT model, cache_read_tokens FROM sessions WHERE id = ?`.
   The raw column value (SQL `NULL` -> Python `None`; otherwise an `int`,
   including `0`) is threaded onto the returned `ParseResult` as
   `session_cache_read_tokens`. `None` means "not measured" (mirrors the
   `UsageProvenance` philosophy: absence and measured-zero are never the same
   value). This is the ONLY producer that ever populates this field —
   `HermesTraceParser`/`ClaudeParser` leave it at its default of `None`,
   because the trace export drops the cache channel entirely (S29) and a real
   Claude Code transcript already reports cache at the correct (call) grain
   via `_is_cache_hit`, so there is nothing session-grain to add there.

2. **`toolbench/transcript.py`** — `ParseResult` gains one new field:
   `session_cache_read_tokens: int | None = None`. Defaulted so every other
   call site (`ClaudeParser.parse`, `HermesTraceParser` via inheritance, all
   existing tests constructing `ParseResult(calls=..., malformed=...)`)
   is unaffected.

3. **`toolbench/passive.py`**:
   - `AgentStats` gains two counters: `sessions_with_cache_data: int = 0`
     (sessions where `session_cache_read_tokens is not None` — i.e. measured
     at all) and `sessions_with_cache_hit: int = 0` (subset where the value is
     `> 0`).
   - `Reducer.absorb(agent, result)` increments these once per session
     (outside the per-call loop, using `result.session_cache_read_tokens`)
     — never inside the per-call loop, so it cannot be miscounted as N calls.
   - `render_report`'s Agent Breakdown section (S14 §1, the first of the five
     sections) gains a caveat line under the table, emitted only for agents
     whose `sessions_with_cache_data > 0`:
     `- {agent}: {hit}/{measured} sessions carry session-grain cache_read_tokens > 0 (session grain only — not attributable to individual tool calls; S32).`
     This does not add a sixth section (S14 says "five, in order") and does
     not touch the Tool Leaderboard's per-call `cache_assisted` column, which
     stays exactly as TB-18 left it (`n/a` for hermes, since no individual
     call is measurable — still true and still correct).

4. **Docs**: author S32 in `SPEC.md` (new subsection under "Usage
   provenance", following S29/S30), the matching row in `EVALUATION.md`, and
   a `T10` row in `BUILDPLAN.md` (mirroring the retroactive `T7`–`T9` rows).
   Precedent: S31 is TB-19's PR #21 (off main, not yet merged) — S32 will
   neighbor it at merge time; note the adjacency in the PR body per the
   delegator prompt.

## Contradiction found in ticket comments — flagged per "deviate-with-flag"

The `agent:orchestrator-intake` comment on TB-20 says: *"Branch off
origin/main AFTER TB-18's PR #20 merges (rebase onto it if it hasn't)."* This
contradicts (a) the actual repo state — `tb-20-cache-read` is already cut
from `origin/chore/add-hermes-cli-export-plan @ 5c74901`, not from `main`,
and already carries the parent's 20 commits in its own `git log` — and (b)
the delegator prompt's explicit, more specific and more current instruction:
diff base and PR base are the parent branch, and rebase is forbidden
("if the parent branch moves, plain-merge, never rebase").

**Resolution**: follow the prompt (matches actual git state) — keep base
`chore/add-hermes-cli-export-plan`, never rebase. This is called out again in
the completion comment.

## TDD sequence (RED -> GREEN per behavior change)

1. **`hermes.py` session-grain column read**
   - RED: extend `tests/test_hermes.py`'s `_SCHEMA`/`_build_db` to add a
     nullable `cache_read_tokens` column; add
     `ParseHermesSession.test_session_cache_read_tokens_surfaces_when_present`,
     `test_null_cache_read_tokens_is_not_measured`,
     `test_zero_cache_read_tokens_is_a_measured_zero_not_absence`. These fail
     today because `ParseResult` has no such field and the query doesn't
     select it.
   - GREEN: add the field to `ParseResult` (transcript.py) and extend the
     query + threading in `hermes.py`. Also add `cache_read_tokens` to
     `_SESSION_COLS` in the test file (the live-schema-envelope guard) so
     `test_live_archive_schema_envelope` continues to assert the real column
     exists across both `.hermes` (schema v16-ish) and the newer profiles.
2. **`Reducer` session-grain counters**
   - RED: `tests/test_passive.py` — new `AgentStats` fields don't exist yet;
     add tests asserting `absorb()` increments `sessions_with_cache_data`
     and `sessions_with_cache_hit` correctly across multiple sessions
     (measured-zero, measured-hit, unmeasured, and a mix), and that a
     Claude-Code session (`session_cache_read_tokens=None`, the default)
     never touches either counter.
   - GREEN: implement in `passive.py`.
3. **Report caveat line**
   - RED: `render_report` tests asserting the caveat line appears (with
     correct M/N) for an agent with session-grain data, is absent for an
     agent without it, and that the five-section-in-order contract (S14)
     and the existing four-case `cache_assisted` column tests are
     unaffected (regression guard — run the full existing
     `CacheNoteRenderTests`/`UsageMissingCounterTests` suites unchanged).
   - GREEN: implement the caveat line in `render_report`.
4. **Docs (S32)** — SPEC.md, EVALUATION.md, BUILDPLAN.md. Not TDD (no
   executable assertion), but the code self-review step re-reads these
   against the shipped behavior.

## Validation plan

- Full gate: `uv run ruff check .`, `uv run mypy --strict toolbench tests`
  (baseline 38 pre-existing errors — only new errors are failures),
  `uv run python -m unittest discover tests`.
- End-to-end evidence against the live hermes archive (read-only,
  `mode=ro`/`immutable=1` per `_connect`'s contract — never opened
  writable): run `toolbench.passive --agent hermes --all` (or an equivalent
  small script driving `parse_hermes_session` + `Reducer` + `render_report`
  directly over real session ids) before and after the change, showing the
  Agent Breakdown gains a session-grain caveat line with real hit/measured
  counts, while the Tool Leaderboard's per-call column for hermes stays
  exactly `n/a` (unchanged from TB-18, still correct) — proving no per-call
  rate was fabricated.

## PR

Base `chore/add-hermes-cli-export-plan` (stacked on #20, not main — see
contradiction note above). Body's first line: "Stacked on #20 — merge that
first; this rebases onto main after it lands." Body also notes the S31/S32
EVALUATION-tail adjacency (TB-19's PR #21).

## 5. Plan-Review Cycle 1 Resolutions (AUTHORITATIVE — overrides earlier text on conflict)

Self-review against SPEC.md, the ticket's acceptance text, and the parent
branch's actual code (`toolbench/hermes.py`, `transcript.py`, `passive.py`,
`tests/test_hermes.py`, `tests/test_passive.py`) turned up one open risk and
confirmed the rest of the design holds:

1. **Verified empirically (read-only, `mode=ro`/`immutable=1`, never
   writable) against all four live profile DBs** — `~/.hermes/state.db`
   (803 sessions), `profiles/aphrodite-mood` (34), `profiles/light-mood`
   (28), `profiles/tech-interviewing` (8) — that `sessions.cache_read_tokens`
   exists as a column on every one of them, with **zero NULLs observed** in
   any profile. This retires the risk that `SELECT model, cache_read_tokens
   FROM sessions` throws `sqlite3.OperationalError` on some older-schema
   profile. `session_cache_read_tokens: int | None` is still typed and
   handled as nullable (defensive; matches the `UsageProvenance`
   absence-vs-measured-zero philosophy and protects against a future/other
   profile where the column genuinely is NULL) even though no live row
   exercises the `None` arm today — the RED/GREEN test sequence in Step 1
   still fixtures a NULL case explicitly so that arm has coverage the live
   data can't provide.
   - Live counts drifted slightly from the numbers pinned in the ticket body
     (803/34/28/8 sessions here vs. 795/34/28/6 in the ticket, and the
     tech-interviewing profile went 6 -> 8) — expected, the archive is live
     and grows between the ticket's measurement and this session; the
     Validate phase re-measures and reports the actual before/after numbers
     rather than reusing the ticket's stale snapshot.
2. **No other contradictions found.** The chosen shape (session-grain caveat
   line, orthogonal to `UsageProvenance`) composes cleanly with S29/S30 as
   written; S14's "five sections, in order" contract is satisfied by adding
   a line inside Section 1 rather than a new section; `_is_cache_hit` and the
   Tool Leaderboard's four-case column are correctly left untouched, since
   per-call attribution genuinely isn't possible and TB-18 already renders
   that correctly (`n/a`).
3. **Verdict: plan proceeds as designed, no shape change.** Only the
   defensive-nullability note above is new; everything else in the design
   section stands unchanged.
