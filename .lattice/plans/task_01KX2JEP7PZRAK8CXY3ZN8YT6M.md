# TB-13: transcript.py grows a schema-dispatch seam; unknown schemas raise instead of returning zero calls

Resolves the open design question left at the bottom of TB-12. That ticket said
"pick one and say why" between a schema-dispatch seam and sibling adapter
modules. Decision: the seam. This ticket owns it, so TB-12 can ship a codex
parser without also relitigating the architecture.

THE PROBLEM

Three transcript schemas are already in the corpus, with a fourth pending:

  agent      schema                              join key           parsed by
  claude     assistant msg -> tool_use block     tool_use_id        transcript.py
  cowork     same as claude                      tool_use_id        transcript.py
  hermes     SQLite messages table               (row-level)        hermes.py
  codex      response_item -> function_call      payload.call_id    NOBODY (TB-12)
  cursor     unknown; most lines lack `type`     unknown            NOBODY

Dispatch today is one ad-hoc conditional in the consumer, at passive.py:311:

    if ref.agent == "hermes" and ref.path is None:
        return parse_hermes_session(...)
    if ref.path is not None:
        ...
        return parse_session(...)          # assumes Claude schema, always

`_parse_ref` is the de-facto seam and it does not know it is one. Adding codex
means a third branch; cursor a fourth. Each new agent edits a function whose
job is supposed to be "open a session," and the Claude schema stays wired in as
the unnamed default that every unrecognized transcript silently falls into.

That default is the whole bug class. TB-12's codex sessions do not error --
they flow into `parse_session`, match nothing, and return `ParseResult(calls=[],
malformed=0)`. A parser that cannot recognize a schema must not be the fallback
for schemas it has never seen.

WHY DISPATCH ON CONTENT, NOT ON AGENT NAME

Tempting to key a registry on `ref.agent`. Do not. `cowork` is a distinct agent
that emits Claude's exact schema and parses correctly today; an agent-keyed
registry would need an entry for every present and future agent that happens to
speak a schema we already handle, and would break the moment AgentsView renames
one. Schema is a property of the payload, not of the producer.

Dispatch should sniff the transcript: read the first non-empty JSON line and
match on its discriminating shape.

  claude/cowork   entry has `message.content` list containing type=="tool_use"
  codex           entry["type"] == "response_item"
  cursor          TBD -- see TB-12 out-of-scope note

Hermes stays keyed on source, not content, and is correctly separate: it is not
a JSONL transcript at all, it is a SQLite read. The seam must not force it into
a line-oriented interface just for symmetry.

IN SCOPE

- Define the parser protocol. Every adapter is `(path, *, agent, source,
  project) -> ParseResult`. Both `parse_session` and codex's parser already fit
  this signature; `parse_hermes_session` takes a session_id instead of a path
  and is the deliberate exception.
- Add a `detect_schema(path) -> str` that reads only the first non-empty line
  and returns a schema tag. Cheap, streaming, no full-file read.
- Add a registry mapping schema tag -> parser, and a `parse_any(ref)` that
  resolves it.
- Reduce `passive.py:_parse_ref` to: hermes source-check, then `parse_any`.
- UNKNOWN SCHEMA MUST RAISE, not return empty. Introduce `UnknownSchema` and
  degrade it to `skipped_roots` the way `NonTranscriptExport` already is, so an
  unparseable agent is named in the Summary rather than reported as a healthy
  zero. This single change would have surfaced TB-12 on the run that created it.
- Rename `parse_session` -> `parse_claude_session` (keep a deprecated alias if
  anything outside toolbench imports it) so no parser is the anonymous default.

PRESERVE EXACTLY -- these are existing acceptance criteria, not refactor latitude

- S5: malformed lines counted and skipped, never fatal. The per-line
  `json.JSONDecodeError` guard and the `errors="replace"` open stay.
- S6: the end-of-file `pending` drain (transcript.py:215-233) emitting
  `no_result=True` calls with `output_chars=0`. A seam that drops the drain
  silently loses unmatched calls -- reintroducing TB-12's bug class inside the
  fix for it.
- S1/S2 join and payload-resolution precedence for the Claude schema
  (`tool_use_id` over `toolUseID`; block-local `content` over `toolUseResult`).
- `result_len` and `ToolCall` are already schema-neutral. They do not move and
  they do not change. Reuse, do not fork.
- The `path_looks_binary` NUL sniff stays and runs BEFORE schema detection --
  a SQLite dump has no first JSON line to detect.

OUT OF SCOPE

- Writing the codex parser. That is TB-12. This ticket lands the seam and
  migrates the two parsers that exist today (claude, hermes). Sequencing is
  either order, but whichever lands second rebases onto the first -- do not
  develop them in parallel against a shared file.
- The cursor parser. TB-12 already records why it needs its own repro first.
- Any change to `passive.py` reducers, report rendering, or the CLI surface.
- Performance work. `detect_schema` reads one line; if it ever needs more,
  that is a separate conversation.

ACCEPTANCE

- `uv run python -m toolbench.passive --agent all --all` produces byte-identical
  agent-breakdown and tool-leaderboard rows to the pre-refactor 2026-07-09 run
  for claude, cowork, and hermes. Regression-pin those three rows in a test.
- A fixture session in an invented schema raises `UnknownSchema` and appears in
  `skipped_roots`; it does NOT appear as a 0-call agent row.
- Existing 145 tests green. ruff clean. mypy --strict clean.
