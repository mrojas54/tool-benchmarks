TB-19 SELF-REVIEW (agent:tb-19-delegator-reviewer)

Verdict: PASS

Scope: replace the documented fast-suite gate command (`uv run python -m
unittest discover tests`, which silently misses module-level `test_*`
functions — 37 of ~213 in this tree) with `uv run pytest -q`, which collects
`unittest.TestCase` methods and module-level functions uniformly. Option (a)
from the ticket, per the orchestrator's run-2 intake settling it.

Commits (2, both mine, only intentional changes vs origin/main@28b6187):
1. c48d0b7 — add tests/test_gate_completeness.py (synthetic-fixture
   regression test proving unittest.TestLoader.discover misses a
   module-level test_* function while pytest --collect-only finds it) +
   pyproject.toml `[tool.pytest.ini_options] testpaths = ["tests"]`.
2. 46007bb — docs: README.md (Usage/Quality gate/Status), EVALUATION.md
   (harness commands + new S31 row), BUILDPLAN.md (Test split + TB-19 ticket
   row + checkpoint 1), AGENTS.md (quoted gate command + intro sentence),
   SPEC.md (S21/S22 wording + new S31 criterion).

Findings: none Critical/Major. Two Minor/NIT:
- NIT: SPEC.md/EVALUATION.md/BUILDPLAN.md now have S31 immediately after S28
  with no S29/S30 — those numbers are reserved by TB-18 (in-flight on a
  different branch, not yet on main). Expected; both PRs will need a routine
  merge-order resolution at these files' tails. Flagged in the PR body per
  the orchestrator's shared-file-overlap instruction.
- NIT: README.md's "Status" paragraph and Quality-gate blurb previously
  quoted a stale count (213); updated to 215 (my +2 tests). This is a
  point-in-time count and will drift again with TB-18's work landing in
  parallel — not a regression, just noting the number is a snapshot.

TDD classification: no toolbench/*.py runtime behavior changes. This is a
docs + pytest-tooling-config fix; per CLAUDE.md's "docs-only edits are
exempt" carve-out, no artificial RED phase was forced (there is no code
defect in unittest/pytest themselves to fail first — the defect was which
command was documented). The regression test added in commit 1 is additive
value (pins the exact collection defect against a synthetic fixture,
independent of the real tests/ directory's future shape), not a RED/GREEN
pair, and is called out as such in its own commit message.

Deviation flag: one pre-existing, unrelated test failure —
tests/test_hermes.py::LiveArchive::test_live_archive_schema_envelope — fails
identically under both unittest discover and pytest, before and after every
commit in this branch, because this machine's live ~/.hermes archive has
sidecar-less WAL profile DBs that toolbench/hermes.py::_connect (mode=ro, no
immutable=1 fallback) cannot open. This is the exact defect session-memory
records as "TB-18 Task 0: Fix _connect to read sidecar-less WAL hermes
profiles" — out of scope for TB-19, not introduced by this branch, and
identical on origin/main before any TB-19 commit. Reporting the gate as
green modulo this one pre-existing, already-tracked, unrelated failure
rather than silently declaring full green or expanding scope into TB-18's
territory.