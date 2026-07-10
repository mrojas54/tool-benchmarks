# Validation Plan — Run 2 (TB-19, TB-18 remainder, TB-20)

Source spec: [SPEC.md](../../SPEC.md) · Source evaluation: [EVALUATION.md](../../EVALUATION.md) · Date: 2026-07-10

Run 1's plan and report live in [`run-1/`](run-1/). This run delivers S29/S30
(TB-18 Tasks 3–6), plus two criteria the tickets author themselves during
their DOCS phases: **S31** (TB-19) and **S32** (TB-20). Rows 10–13 therefore
audit *both* the authored contract row and the behavior it pins — a ticket
that ships behavior without its criterion row FAILS its contract-row row.

| # | Criterion (ID) | Verification method | Artifact to inspect | Pass condition | runnable_at |
|---|----------------|---------------------|---------------------|----------------|-------------|
| 1 | S29 — producer split | Read `toolbench/parsers.py` + `tests/test_parsers.py` in the TB-18 PR tree: `HermesTraceParser.claims_line` keys on `version == "hermes-agent"`; `ClaudeParser.claims_line` excludes it; a partition test asserts no line is claimed by both | TB-18 PR (#20) diff + source | Both predicates present; partition test exists and passes in CI-equivalent local run | pre-merge-static |
| 2 | S29 — fixture routes | `tests/fixtures/schema_hermes_trace.jsonl` exists; a test drives `detect_parser` over it and asserts the returned parser is `HermesTraceParser` (by `type(...) is`, not isinstance) | TB-18 PR diff | Fixture committed; routing test asserts exact type; no `AmbiguousSchema`/`UnknownSchema` | pre-merge-static |
| 3 | S29 — provenance stamped | `ToolCall.usage_provenance` is a required (no-default) field; every `ToolCall(` construction site in `toolbench/` passes it; `HermesTraceParser._provenance` returns `ABSENT_BY_EXPORT` unconditionally | source at PR head | `rg -n 'ToolCall\(' toolbench/` sites all pass `usage_provenance`; mypy --strict shows no new errors vs baseline (38) | pre-merge-static |
| 4 | S29 — four-case cache render | `tests/test_passive.py` covers all four renders: `yes`, `no` (usage present, zero hits), `n/a` (no call measurable), `n/a*` (mixed); `ToolStats.usage_missing` is a counter, not a scalar | TB-18 PR diff (Task 3) | Four distinct render assertions pass; `no` is only emitted when `usage_missing == 0` for the bucket | pre-merge-static |
| 5 | S29/S19 — cache stays caveat-only | Diff inspection: no ranking/sort in `render_report` consults cache fields | TB-18 PR diff | Ranking unchanged; cache column render-only | pre-merge-static |
| 6 | S30 — probe refuses trace at dispatch | Test drives probe entry over a hermes-trace fixture and asserts refusal (exception naming hermes-trace/NonIsolableTurns), not a scored table | TB-18 PR diff (Task 5) | Refusal test passes; no partial-corpus mode flag exists | pre-merge-static |
| 7 | S30 — `_turn_key` raises | `_turn_key` on an entry lacking `requestId` raises `NonIsolableTurns`; `rg -n 'ts:' toolbench/probe.py` finds no timestamp-fallback key construction | source at PR head | Raise test passes; zero `f"ts:{...}"` occurrences remain | pre-merge-static |
| 8 | S30 — probe fixtures migrated first | Commit order in the TB-18 PR: fixture `requestId` migration (Task 4) lands before the fallback deletion (Task 5); `probe_session_response_pooled.jsonl` byte-identical to pre-PR state | PR commit list + `git diff` on that fixture | Task-4 commit precedes Task-5 commit; pooled fixture untouched | pre-merge-static |
| 9 | S22 — strict gate per PR | For each of the three PRs at head: `uv run ruff check .`, `uv run mypy --strict toolbench tests` (≤38 pre-existing errors), `uv run pytest -q` all green | each PR checked out locally | All three commands exit 0 (mypy: no NEW errors); pytest reports 0 failures | pre-merge-static |
| 10 | S31 — criterion authored (TB-19) | SPEC.md gains an S31 row; EVALUATION.md gains a matching table row + updated Harness commands; BUILDPLAN gains a T-row carrying both IDs | TB-19 PR diff | All three documents updated in the same PR; S31 text pins one fast-suite command that collects the full suite | pre-merge-static |
| 11 | S31 — full collection proven (TB-19) | Compare collected test counts: the documented command's count equals `uv run pytest -q`'s count; a regression test or CI-visible check prevents silent skips recurring | TB-19 PR tree | Documented command collects 100% of tests; the 37-test gap is closed or `unittest discover` is no longer documented anywhere (`rg -n 'unittest discover'` over README/EVALUATION/BUILDPLAN/SPEC returns nothing) | pre-merge-static |
| 12 | S32 — criterion authored (TB-20) | SPEC.md gains an S32 row; EVALUATION.md row; BUILDPLAN T-row | TB-20 PR diff | Same three-document standard as row 10 | pre-merge-static |
| 13 | S32 — session-grain cache consulted (TB-20) | Test with a hermes fixture whose session row carries `cache_read_tokens > 0` asserts the report no longer renders a universal miss for hermes buckets; DB opens remain read-only (`mode=ro` / guarded `immutable=1` only) | TB-20 PR diff + source | Test passes; `rg -n 'sqlite3.connect' toolbench/` shows no writable open | pre-merge-static |
| 14 | S29/S30 live trace export (external-oracle) | EVALUATION smoke #6: `hermes sessions export --format trace <dir>` on a real session; `passive` renders `n/a` (not `no`) for its calls; `probe` over the same file refuses with `NonIsolableTurns` | operator terminal, merged tree | Both behaviors observed against a fresh export from the installed hermes CLI | post-merge-smoke |
| 15 | S32 live archive (operator-assisted) | `TOOLBENCH_LIVE=1 uv run pytest -q` against the real `~/.hermes` archives; hermes cache figures materially non-zero in a real report run | operator terminal, merged tree | Live suite green; a real hermes report shows session-grain cache signal | post-merge-smoke |
| 16 | Report reads well (felt) | Operator reads one full passive report post-merge | merged tree report output | Four-case cache column is scannable and the `n/a*` footnote explains itself | post-merge-smoke |

## Notes for the Result Validator

- Rows 10–13 depend on contract rows that do not exist at plan-writing time
  by design (the tickets author them). If a ticket's PR ships behavior without
  its SPEC/EVALUATION/BUILDPLAN row, mark the contract-row row FAIL — do not
  audit the behavior against the ticket description as a substitute.
- The mypy --strict baseline is 38 pre-existing errors; "green" for row 9
  means no new errors, not zero.
- TB-18's PR is #20 and already contains Tasks 0–2; audit the whole PR, not
  only the run-2 commits.
