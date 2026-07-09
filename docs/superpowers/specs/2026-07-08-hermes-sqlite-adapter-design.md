# Hermes SQLite adapter (TB-11)

Date: 2026-07-08
Ticket: TB-11
Status: approved

## Problem

`agentsview session export hermes:<id>` returns `rc=0` and streams a whole SQLite
database instead of the contracted JSONL transcript. Toolbench defends against this
(TB-10, `NonTranscriptExport`), so runs complete — but every hermes session is demoted
to `skipped_roots` and contributes **zero tool calls**. Hermes never appears in the
agent breakdown.

The payload is `~/.hermes/state.db`, byte-for-byte (37,175,296 bytes; identical sha256
across every session id). The export resolves the session id, validates it, then
streams the default profile's backing store.

## Findings from the spike (2026-07-08, read-only)

All measured against the live archive.

| Question | Answer |
| --- | --- |
| Is the export literally `state.db`? | Yes — exact byte-size match |
| Do the 29 in-corpus hermes ids resolve? | 29/29, but only across **all three** profile DBs |
| Tool calls recoverable | 176 |
| Dangling calls (no matching result) | 0 |
| Agrees with hermes' own `sessions.tool_call_count`? | Yes, on all 29 sessions |

Two of the 29 sessions live in `profiles/aphrodite-mood/state.db`. For those, the
export returns `rc=0` and a database that contains **no rows for the requested
session**. The defect is therefore worse than "returns too much data": for 2 of 29 it
returns the wrong database. Direct SQLite reads recover strictly more than a *fixed*
export would, because a fixed export would still be reading the default profile.

The `tool_call_count` agreement is corroboration, not just a passing check: hermes
computed that column at write time through an independent code path, and it matches a
join we derived ourselves. That is evidence we are joining on the intended key.

### Discovery is NOT reproducible from the database

`state.db` holds 814 sessions (3,106 tool calls) across three profiles. AgentsView
indexes only 89 of them. No column explains the selection:

| Hypothesis | Result |
| --- | --- |
| `archived == 0` | `archived` is 0 on all 814 rows |
| `parent_session_id IS NULL` | set only on 4 *indexed* rows |
| empty sessions (`message_count == 0`) | only 7 of the 725 excluded |
| zero tool calls | 65 of 725 excluded, but 11 *indexed* rows also have zero |
| dedupe on `session_key` / `title` | `session_key` NULL on all 728 cron rows; titles all distinct |

A time-window bound was considered and rejected: the 500-session corpus page reaches
back to 2026-05-23, while hermes' entire archive begins 2026-06-07. Every hermes
session is already inside the window, so the filter is a no-op.

### Correction (2026-07-09): the 89 is an AgentsView defect, not a curation

An earlier draft of this spec explained the 89-of-814 gap as AgentsView presenting a
"curated view" of what counts as a session. **That was a hypothesis, and it is wrong.**

AgentsView's own `stats` subsystem sees the archive nearly in full:

```
agentsview stats --agent hermes --format json  -> totals.sessions_all = 789
agentsview session list --agent hermes         -> total = 89, next_cursor = null
```

Same binary (v0.36.1), same archive, two subsystems disagreeing by 8.9x. The dropped
sessions do not follow a source rule either — 701 cron, 18 tui, 5 cli. `session list`
is very likely *losing* hermes sessions, not selecting them.

**The decision is unchanged; its justification is not.** Discovery stays with
AgentsView because every other agent is sampled through that same `session list` path,
and unilaterally reinterpreting it for one agent is precisely what would destroy
cross-agent comparability. We do not enumerate the archive ourselves — but not because
AgentsView's 89 is *right*. It is because the corpus is *defined* as what `session
list` returns, and correcting that definition is upstream's job, not a thing to fork
silently into one agent's adapter.

Filed upstream. Until it is resolved, hermes is under-sampled in this corpus, and that
is a known, named limitation rather than a hidden one.

## Design

### Architecture

New module `toolbench/hermes.py`. Stdlib only (`sqlite3` is stdlib, so the
zero-dependency constraint in `pyproject.toml` holds).

```
hermes.py
  hermes_home()          -> Path         HERMES_HOME env override, default ~/.hermes
  iter_profile_dbs()     -> list[Path]   [state.db, profiles/*/state.db], sorted
  resolve_session(id)    -> Path | None  first profile db containing the id
  parse_hermes_session(session_id, *, agent, source, project) -> ParseResult

passive.py::_parse_ref
  if ref.agent == "hermes" and ref.path is None:
      return parse_hermes_session(...)
  # existing branches unchanged
```

Every connection opens `file:{path}?mode=ro` with `uri=True`. Read-only; a live app's
database is never mutated. Session ids arrive prefixed (`hermes:cron_...`); the prefix
is stripped before lookup.

### Data flow

1. Resolve `session_id` to the first profile DB whose `sessions` table contains it.
2. One query for results: `role='tool' AND tool_call_id IS NOT NULL` → dict keyed by
   `tool_call_id`. Uses `idx_messages_session`.
3. One ordered query for assistant rows where `tool_calls IS NOT NULL`.
4. Join in Python on `tool_calls[].id` (falling back to `call_id`).

Field mapping onto `ToolCall`:

| ToolCall field | Source |
| --- | --- |
| `name` | `tool_calls[].function.name` |
| `input_chars` | `len(tool_calls[].function.arguments)` |
| `output_chars` | `result_len(content)` of the matching `role='tool'` row |
| `ts` | `messages.timestamp` (epoch REAL) → ISO 8601 |
| `session_id` | the bare (unprefixed) id |
| `model` | `sessions.model` |
| `usage` | `None` |
| `error` | `"tool_error"` when the result parses to a dict with non-null `error` |
| `no_result` | `True` when a call has no matching result row (S6 semantics) |

`usage` is `None` deliberately. Hermes records `token_count` per *message*, not per
tool call; there is no honest per-call usage record, and fabricating one would corrupt
token statistics downstream.

### The error convention

Hermes tool results are JSON dicts carrying an `error` key that is `null` on success
(669 rows) and a message string on failure (628 rows). The key is **always present**,
so a substring match on `"error"` matches 1,527 of 2,368 rows and would mark nearly
every successful call as a failure. The signal is the *value*.

409 results are not JSON at all — plain strings. These get no error signal. Absence of
evidence, not evidence of success.

### Error handling

- Missing `~/.hermes`, missing profile DB, or an unresolvable session id raises
  `NonTranscriptExport`. `passive.main` already demotes that to `skipped_roots`, so a
  hermes failure degrades the run instead of aborting it.
- A corrupt `tool_calls` JSON blob is skipped, consistent with the malformed-line
  tolerance of `parse_session` (S5).
- The TB-10 NUL sniff stays exactly as it is. It defends every consumer of
  `open_session_jsonl`, and remains correct regardless of any upstream fix.

### Schema drift

The three profile DBs report `schema_version` 19, 16, and 16. Only seven columns are
read (`id`, `source`, `model`, `started_at`, `tool_call_count` on `sessions`;
`session_id`, `role`, `content`, `tool_call_id`, `tool_calls`, `timestamp` on
`messages`), all present in every version. A test asserts this compatibility envelope
against the real DBs rather than trusting it.

### Testing

Hermetic, matching the existing suite (which fakes the `agentsview` CLI and never
shells out):

- Build a fixture SQLite DB in `tmp_path` with the minimal schema.
- Join correctness: a call with a matching result; `input_chars` / `output_chars`.
- `no_result=True` for a call whose result row is absent.
- Error detection: `error: null` → no error; `error: "msg"` → `"tool_error"`;
  non-JSON string content → no error.
- Multi-profile resolution: session present only in `profiles/x/state.db`.
- Unresolvable id → `NonTranscriptExport`.
- Malformed `tool_calls` JSON is skipped, not fatal.
- Read-only: the adapter never writes (assert mtime unchanged).
- `_parse_ref` dispatches `agent=="hermes"` to the adapter.

One live-archive test, skipped when `~/.hermes` is absent, asserts the schema
compatibility envelope across whatever profile DBs exist.

## Out of scope

- The `claude-ai` `rc=1` empty export. Fails cleanly; separate question.
- Removing the NUL sniff.
- Owning hermes discovery. Explicitly rejected above.
- The upstream AgentsView bug report. Tracked separately on TB-11.

## Expected outcome

Hermes moves from 0 tool calls to 176, across 29 sessions, including 16 `mcp_dash0_*`
calls that are the corpus's only MCP-tool data. The corpus contract is unchanged.
