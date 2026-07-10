# TB-19 Plan — replace `unittest discover` with `pytest -q` as the documented gate

## Verified locally (2026-07-10, on this worktree @ origin/main 28b6187)

- `uv run python -m unittest discover tests`: **176 tests**, 1 error. Module-level
  `test_*` functions are invisible to `unittest.TestLoader.discover`; only
  `unittest.TestCase` methods run.
- `uv run pytest -q`: **213 tests collected**, 1 failed. `213 - 176 = 37` — the
  exact module-level count the ticket cites, confirmed file-by-file via an AST
  walk (`test_adapters.py` 14, `test_sources.py` 7, `test_parsers.py` 6,
  `test_registry.py` 6, `test_passive.py` 4).
- Both harnesses fail the **same** pre-existing test:
  `tests/test_hermes.py::LiveArchive::test_live_archive_schema_envelope`. This
  machine has a real, live `~/.hermes` archive whose `state.db` /
  `profiles/*/state.db` are open by a running hermes process; `toolbench/hermes.py`'s
  `_connect` does `sqlite3.connect(f"file:{db}?mode=ro", uri=True)` with no
  `immutable=1` fallback for a sidecar-less WAL file, which raises
  `sqlite3.OperationalError: unable to open database file`. This is the exact
  defect session-memory records as **"TB-18 Task 0: Fix `_connect` to read
  sidecar-less WAL hermes profiles"** — out of scope here, pre-existing on
  `origin/main` before any TB-19 change, and identical under both harnesses.
  **Deviation flag:** the orchestrator's gate line ("all green pre-PR") cannot
  be met literally by TB-19 alone; I will report the gate as green modulo this
  one pre-existing, unrelated, already-tracked failure and call it out
  explicitly in the completion comment rather than silently declare a false
  green or silently expand scope into TB-18's territory.

## Decision: Option (a) from the ticket

The orchestrator's run-2 intake comment already settles this: "`uv run pytest
-q` is the working baseline." Going with (a) — change the documented gate
command — not (b) (converting 37 functions to `TestCase` methods), which the
ticket itself frames as the alternative, not the chosen path.

## Scope classification (TDD carve-out)

No `toolbench/*.py` runtime behavior changes. This ticket is a **docs +
tooling-config** fix: which command is documented, and formalizing pytest's
own discovery config so it doesn't silently drift. Per CLAUDE.md, "docs-only
edits are exempt" from the strict RED → GREEN commit pairing. I'm treating the
`pyproject.toml` pytest config addition + its regression test as one unit
(still two commits: test first, then the docs+config, since the test is a
new-file-add and independently reviewable) rather than forcing an artificial
RED that doesn't correspond to any real pre-fix failure — there is no code
defect in `unittest`/`pytest` themselves to turn red first; the defect is
*which command was documented*.

## Steps

1. **`pyproject.toml`** — add `[tool.pytest.ini_options]` with
   `testpaths = ["tests"]`. Small, uncontroversial hardening: pins the
   discovery root explicitly instead of relying on implicit rootdir/cwd
   behavior, so the gate command is invocation-order-independent.
2. **New regression test** `tests/test_gate_completeness.py` — one
   hermetic test (module-level function, deliberately — it dogfoods its own
   fix) that builds a tiny synthetic fixture package in `tmp_path` containing
   one module-level `test_*` function and one `unittest.TestCase` method,
   then shells out to both `python -m unittest discover` and
   `python -m pytest --collect-only -q` against it and asserts unittest
   reports exactly 1 test while pytest reports 2. This pins the exact defect
   TB-19 exists to fix, independent of whatever the real `tests/` directory
   looks like in the future.
3. **`README.md`** —
   - Usage section: `uv run python -m unittest discover tests` →
     `uv run pytest -q`.
   - Quality gate section: same substitution; wording update to say the full
     **pytest** suite must be green.
4. **`EVALUATION.md`** — "Harness commands" section: redefine `test` as
   `uv run pytest -q`; note it collects both `TestCase` methods and
   module-level `test_*` functions (this is *why* it replaces `unittest
   discover`, per S31). Add **S31** row to the criteria table.
5. **`BUILDPLAN.md`** — "Test split" section: `test` command becomes
   `uv run pytest -q`. Add a **TB-19** row to the Tickets table
   (Scope / SPEC IDs `S31` / Depends on `—`), mirroring the TB-18 precedent
   of appending a ticket row for gate/harness work.
6. **`AGENTS.md`** — it quotes the quality-gate command verbatim
   ("`uv run python -m unittest discover tests`"); update to
   `uv run pytest -q` so the Cursor Cloud doc doesn't silently re-diverge from
   README.
7. **`SPEC.md`** — add **S31** under the existing `## Testing` section
   (after S26, before the `## Schema dispatch` header): the documented/gate
   command is `uv run pytest -q`; it collects `unittest.TestCase` methods and
   module-level `test_*` functions uniformly, so a test added either way
   cannot silently escape the gate.

## Validation

Run all three gate commands from the worktree root:
`uv run ruff check .`, `uv run mypy --strict toolbench tests` (diff against
the 38-error baseline), `uv run pytest -q`. Report exact counts, including the
one pre-existing unrelated failure called out above.

## PR

Base `main`. Body must flag the shared-file overlap with TB-18's PR #20
(README.md, EVALUATION.md — both add tail content, expect a union merge).
