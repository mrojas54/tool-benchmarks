# TB-24: codex web_search_call has no call_id and no output record, so CodexParser cannot join it

Found during TB-12 review. The live codex archive (114 rollouts) contains 138 `web_search_call` response_item records.

Unlike the three shapes CodexParser joins, web_search_call:
  - carries NO `call_id` (verified: has_call_id={False} across all 138)
  - has NO matching `web_search_output` record at all

So it cannot be joined on payload.call_id, which is CodexParser's only key. Claiming it would either fabricate a join key or emit 138 permanent no_result orphans; both are worse than reporting none. S33 documents the omission explicitly.

IMPACT: codex's reported call count understates its true tool usage by 138 calls (~4%). Web search is invisible in the corpus for codex.

## Decision — (b), confirmed by operator 2026-07-10

Leave `web_search_call` **unclaimed as a joined call** (corpus call counts and every
downstream inefficiency ratio unchanged), but **count it and surface the gap** in the
Summary reconciliation block so codex's undercount is named, not silently absent.
Matches the project's no-silent-zeros stance and reuses TB-21's reconciliation home.
Option (a) — emitting them as `no_result` orphans — was rejected: it shifts corpus
numbers and recreates the exact orphans the parser docstring warns against.

## Shape

A parser can now report tool records it *recognized as real calls* but *structurally
cannot join* (no join key, no output record). This is a third bucket alongside
"joined calls" and "malformed lines": a record that is neither noise nor a countable
call. Keyed by record kind so the Summary attributes the gap (TB-23's typed-bucket
ethos) rather than reporting an anonymous total.

## Changes

1. **`toolbench/transcript.py`** — `ParseResult` gains
   `unjoinable: dict[str, int] = field(default_factory=dict)`: record kind → count of
   recognized-but-unjoinable tool records. Empty for every parser that has none. (S38)

2. **`toolbench/parsers.py`** — `CodexParser.parse` recognizes `web_search_call`
   (before the `call_id` guard, since it never carries one), tallies it into a local
   `unjoinable` dict, and threads it into the returned `ParseResult`. Docstring updated
   from "tracked separately" to name the field. Other parsers unchanged (default `{}`).

3. **`toolbench/passive.py`**
   - `Reducer` gains `unjoinable: dict[tuple[str, str], int]` ((agent, kind) → count);
     `absorb` folds `result.unjoinable`.
   - `_apply_date_range` preserves `unjoinable` (a count of seen records, not
     date-filterable calls — treated exactly like `malformed`, which it also keeps).
   - `session_signature` folds the per-session unjoinable total, and the sig call site
     passes it: the Summary now renders this number, so per TB-22's invariant an append
     of a `web_search_call` must move the fingerprint (else the number moves while the
     fingerprint falsely matches — the one outcome S36 forbids). (S36 extended)
   - `render_report` adds, after "Malformed lines", when the total is non-zero:
     ```
     - Unjoinable tool records (seen, not joined): <total>
       - <agent>/<kind>: <count>
     ```
     Absent entirely when zero, mirroring the "Skipped by reason" nested style. (S38)

4. **Docs** — SPEC S38 (+ update S33's closing paragraph to point at the new
   surfacing), EVALUATION row, README note, BUILDPLAN T14 row.

## Tests (RED first)

- `test_parsers`: a codex transcript with N `web_search_call` records →
  `ParseResult.unjoinable == {"web_search_call": N}`, and none of them appears in
  `.calls` (join counts unchanged).
- `test_passive`: `Reducer.absorb` folds unjoinable into `(agent, "web_search_call")`.
- `test_passive`: `session_signature` differs when a `web_search_call` is appended
  (fingerprint moves with the rendered number).
- `test_passive`: `_apply_date_range` preserves `unjoinable`.
- `test_passive`: `render_report` shows the reconciliation line with `codex/web_search_call: N`;
  the line is absent when there are no unjoinable records.

## Gates

RED → GREEN → DOCS as separate commits. `ruff`, `mypy --strict`, full `pytest` green
before each commit; report counts. Branch + PR (remote `origin` confirmed); Lattice
review → merge → done ceremony.

RELATED: TB-21 (Summary reconciliation — the home this reuses), TB-22 (fingerprint
invariant this extends), TB-12 (where the gap was found).
