# Run Closeout — tool-benchmarks (2026-07-08)

## Outcome
- **6/6 tickets built and landed at `review`** (TB-2…TB-7), 6 open PRs (#1–#6).
- Assembled `integration/full` strict gate GREEN: ruff clean, mypy --strict clean
  (10 files), 93 unittest OK.
- Phase-2 audit: **24/24 pre-merge-static rows PASS** (see validation-report.md),
  run in degraded mode (Orchestrator-as-validator; see caveat in that report).
- Merged (PRs #1–#7). **Operator smoke checklist run 2026-07-08: 4 PASS,
  1 bug found (TB-8, fixed).** Row 4 opened as TB-9 and closed the same day.
  Verifying TB-9 against the live corpus then exposed TB-10 (crash on a
  non-transcript session export), also fixed the same day.
  See "Operator smoke results" below.

## Operator smoke results (2026-07-08)

Gate re-verified on merged `main` first: ruff clean, mypy --strict clean (10 files),
99 unittest OK.

| Row | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Join-key on real data (S1/S2) | **PASS** | 301 calls joined, 470k output tokens, 0 malformed. Real transcripts hold 302 block-local `tool_use_id` results and **zero** top-level `toolUseID` — the block-local branch is the only path that ever fires. Reversed precedence would have zeroed every join. |
| 2 | AgentsView live path (S10/S25) | **PASS** (incomplete) | Healthy daemon → `auto` uses AgentsView (`fallback reason: none`). Binary hidden from `PATH` → falls back to raw, names the reason. `--index-source agentsview` → fatal, exit 1. **All three exercise an *early* failure inside `_discover_refs`, where `FileNotFoundError` is caught. A failure during *per-session export* was never exercised — that gap is TB-10.** |
| 3 | Scale / flat memory (S11) | **PASS** | Pushed past the 200-session bar to the full corpus: 3,705 sessions / 417 MB / 5.2 s, peak RSS **44.8 MB** vs 35.9 MB at 20 sessions (+25% for 185× sessions). Joined 6,683 of 6,684 ground-truth `tool_use` blocks; the 1 gap is a call appended to the live transcript mid-read. |
| 4 | Report reads well (`felt`) | **PASS** (was PARTIAL) | Four sections in spec order, scannable. Callouts were bare counts with no denominators or attribution — `Failures: 865` named no tool. Ticketed as TB-9 and fixed: each callout now reads `N of M calls (P%); top: <tool> (n)`. Live corpus: `Failures: 147 of 997 calls (14.7%); top: Bash (109)`. |

### Follow-up: TB-9 — inefficiency callouts were signal, not action

Smoke row 4 was left open on operator judgment, then ticketed and fixed rather than
accepted. `render_report` printed each S14 callout as a bare integer, so an operator
could not tell whether `Failures: 865` was alarming (no denominator) or where to look
(no attribution).

`InefficiencyCounters` now carries a `*_by_tool` breakdown beside each scalar, and
`_callout()` renders `N of M calls (P%)` naming the worst tool. Ties break
alphabetically so the report stays deterministic; a zero count omits the top-offender
clause instead of naming an arbitrary tool. ToolSearch keeps its token figure and
gains a denominator.

S14 fixes *which* callouts appear, not their formatting, so no spec change was needed.
Verified by driving the real CLI (`--index-source raw --limit 80`), not fixtures alone
— per the retrospective finding below. RED → GREEN → DOCS, 109 tests.

### Bug found: TB-10 — one non-transcript session aborted the whole corpus scan

Found while verifying TB-9 against the live corpus. `--index-source auto --limit 60`
died with an unhandled `UnicodeDecodeError` (`0xa0`); `--limit 3` succeeded, so the
failure was data-dependent, not daemon-dependent.

Three decode boundaries read strict UTF-8: `subprocess.run(..., text=True)` in
`_run_agentsview` (the crash site — decoding happens inside `communicate()`, before
any parsing code runs), `open()` in `open_session_jsonl`, and `Path.open()` in
`parse_session`. That third one is the boundary the **raw** CLI path actually hits:
`_parse_ref` calls `parse_session(ref.path)` directly and never routes through
`open_session_jsonl`, so the ticket's stated `sources.py:98` was not the raw crash
site. All three now decode with `errors="replace"`.

`main()` wraps `_parse_ref` in `except (OSError, RuntimeError)` precisely so a bad
session degrades to `skipped_roots`. `UnicodeDecodeError` subclasses `ValueError`,
so it slipped that guard and took the whole run down. The guard now names
`UnicodeDecodeError` explicitly — deliberately narrower than the ticket's proposed
bare `ValueError`, which would have converted genuine programming errors inside
`parse_session` into silently-skipped sessions.

**The stray byte had a bigger cause than the ticket assumed.** `agentsview session
export` returns returncode 0 and a **37 MB SQLite database** for hermes cron sessions
(`SQLite format 3\0…`), not JSONL. Lenient decode alone would have *absorbed* that as
351,110 "malformed lines" per session — 702,220 across a 60-session run, drowning a
provenance counter that reads 0 on a clean corpus. Sessions are now sniffed for a NUL
byte (impossible in JSONL, present in SQLite's header at offset 15) and rejected as
`NonTranscriptExport`, which subclasses `RuntimeError` so the existing per-session
guard demotes them to `skipped_roots` with no new branch.

The two fixes the ticket proposed in the alternative — lenient decode *or* a widened
guard — turn out to conflict: decode alone absorbs binary as garbage, guard alone
discards good sessions that carry a single stray byte. Both are needed, at different
layers. A session with one bad byte keeps its calls; a session that is not a
transcript is skipped by name.

Fixing this surfaced a latent temp-file leak: `_parse_ref` bound `tmp_path` only
*after* the write loop, so any exception from the line generator stranded a
`delete=False` `NamedTemporaryFile`. It already leaked on a nonzero export returncode;
the new raise merely made the path hot.

Live corpus, the original repro (`--limit 60`): before, `UnicodeDecodeError` and no
report; after, 58 scanned, 4,518 calls, `Malformed lines: 0`, 2 skipped roots named.
`--index-source raw` unaffected. RED → GREEN → DOCS, 121 tests.

Open question for AgentsView, not fixed here: an `export` that exits 0 while emitting
a database file rather than the transcript it contracts is arguably broken upstream.
27 such sessions appear in a default 500-session page (~1 GB of binary read and
discarded per full run).

### Bug found: TB-8 — `--project` silently dropped every subagent session

`sources.py` globbed recursively but filtered on `path.parent.name`. Subagent
transcripts live at `<project>/subagents/*.jsonl`, so `parent.name == "subagents"`
never contained the project substring: **every subagent session was dropped whenever
`--project` was passed**, `--exclude-subagents` was a no-op (nothing left to filter),
and the report still printed `Subagents included: yes` (false provenance).
`--all` was unaffected — `project is None` short-circuits the branch — which is why
the full-corpus run looked correct and only the per-project run exposed it.

Measured on `-Users-…-wids-nyc-reading-group-assistant`: `iter_session_files(project=P)`
→ 197 files / 0 subagent; unfiltered ∩ P → 249 files / 52 subagent. Post-fix the two
sets are identical, joining 2,689 calls — matching an independent `tool_use` block count.

Violated **S13** and **S15**. Fixed in `tb-8-subagent-project-filter` (RED → GREEN,
101 tests).

## Timeless findings (failure mode → why it matters → fix)

1. **Probe Lattice subcommand availability during Phase 0, not just status vocab.**
   The orchestrator playbook's boot templates assume a Stage11 preset
   (`claim`, `plan-review`, `code-review`, `needs-human`, `in_validation`/`pr_open`).
   This install was v0.2.0 **classic** and had none of them. Had a delegator called
   `lattice claim`/`plan-review` it would have errored mid-run. Fix already applied:
   Phase 0 now probes each assumed subcommand (`lattice <cmd> --help`) and bakes
   substitutions (own-reviewer fallback, `status … needs_human`) into boot prompts.
   → Candidate promotion to `references/intake.md` install-facts checklist.

2. **`gh` aliased to `op plugin run -- gh` (1Password) breaks headless agents.**
   Fails with "interactive IO not available" and pops a blocking 1Password overlay
   that can also wedge fresh c11 surface init. Plus the default
   `GITHUB_PERSONAL_ACCESS_TOKEN` lacked PR-create scope. Fix baked into prompts:
   use `/usr/local/bin/gh` + `gh auth switch` to the keyring (repo-scoped) account;
   Escape to dismiss the overlay. → Machine-specific; belongs in personal/global
   env notes, not the skill.

3. **c11 new-surface PTY init can fail late in a long run.** Fresh terminal
   surfaces (background tabs especially) failed to initialize their PTY
   ("Surface not ready" / "Terminal surface not found") while existing surfaces
   read fine — a partial degradation, aggravated by the stuck 1Password overlay.
   Blocked the Phase-2 fresh-validator spawn. Mitigation used: halt-and-surface,
   then Orchestrator-as-validator (degraded mode). → c11/orchestrator footgun;
   recovery = degraded-mode audit or retry after clearing the overlay.

4. **Two-parent ticket → integration branch pattern (positive).** TB-5 needed both
   TB-3 (parser) and TB-4 (sources); no single parent branch had both. Cutting
   `integration/substrate = merge(tb-3-parse, tb-4-sources)`, validating the
   assembled tree green (40 tests), then basing TB-5 on it worked cleanly — same
   for `integration/full` before TB-7. Confirms the skill's integration-branch
   guidance for stacked/multi-parent work.

5. **A criterion "verified" only against hand-built fixtures is not verified.**
   Validation-plan rows 15 (S13) and 17 (S15) both passed the static audit while the
   shipped code violated both (TB-8). The tests constructed `SessionRef` objects
   directly and never called `iter_session_files`, so the discovery layer and the
   subagent filter were each correct in isolation and wrong in composition. A fixture
   proves your test is self-consistent, not that real data has that shape.
   → When a criterion asserts behavior over a real directory layout or payload shape,
   its verification row must exercise the real seam end-to-end, or be explicitly
   tagged `post-merge-smoke` and **not counted as a pass**. Two rows here were counted
   as passes. Corollary: "leave-at-review" merged one row too early — the smoke rows
   were the only ones that could have caught this, and they ran after the merge.

   **Confirmed twice.** TB-10 is the same failure at the byte layer: every source test
   built `CompletedProcess(stdout=<str>)` by hand, so no test ever let
   `subprocess.run(text=True)` *decode* anything — which is exactly where the crash
   lived. A fixture cannot reproduce a defect in the step that produces fixtures.
   Both TB-8 and TB-10 were found by running the real CLI over the real corpus, and
   both were invisible to a green suite. TB-10's tests therefore drive a live
   subprocess emitting real `0xa0` bytes and write real bytes to disk.
   → Generalization: **mock at the seam you own, never at the seam you're testing.**
   For any boundary that decodes, parses, or globs, at least one test must cross it
   for real.

6. **A bug report's root cause is a hypothesis, not a finding.** TB-10's ticket named
   `sources.py:98` as the raw-path crash site; the raw path never executes that line
   (`_parse_ref` → `parse_session`). It proposed catching bare `ValueError`, which
   would have masked real errors, and its two proposed fixes silently conflicted —
   one absorbs binary as garbage, the other discards salvageable sessions. It also
   assumed "a stray non-UTF-8 byte" where the truth was a 37 MB SQLite file. Every one
   of those was written by an agent that had reproduced the crash but not the *cause*.
   → Reproduce before you fix, and re-derive the root cause from the failing run
   rather than inheriting it from the ticket — even a ticket you wrote yourself.

7. **A spike's job is to falsify the plan's premises, not to confirm them.** TB-11 was
   approved as "enumerate hermes from its `sessions` table, bounded by the corpus time
   window." Both premises died on contact with the data. The window reaches back to
   2026-05-23 while hermes' archive begins 2026-06-07, so the bound filters *nothing*.
   And AgentsView's 89-of-814 session selection is derivable from no column in the
   archive (`archived` is 0 on every row; `parent_session_id` is set only on *indexed*
   rows; `session_key` is NULL on all 728 cron rows). Implementing as approved would
   have shipped a `WHERE started_at >= ?` clause that filters zero rows and a hermes
   corpus 9× larger than every other agent's, with no principled account of its
   contents.
   → Before building on a premise, **run the query that would disprove it.** Report the
   result even when — especially when — it invalidates an approved plan.

8. **Distinguish the write-ahead record from the curated view.** `state.db` holds
   everything hermes ever did, including 699 automated cron runs. AgentsView's index
   holds what counts as a session. "Recover unreachable data" (an access question) and
   "recover data that belongs in this corpus" (a sampling question) look identical
   until you notice hermes would jump from 0% to 29% of corpus tool calls purely
   because we handed one agent a private data source.
   → When a benchmark compares populations, changing *where* one population's data
   comes from changes *what* is being compared.

9. **A key that is always present is not a signal; its value is.** Hermes tool results
   carry an `error` key on nearly every row, `null` on success. `content LIKE '%"error"%'`
   matches 1,527 of 2,368 rows and would have reported nearly every successful call as
   a failure, roughly doubling the reported failure rate with a green test suite.
   → Read the distribution of a field's *values* before treating its presence as
   meaning anything.

10. **Changing a code path can silently retire the test that guarded it.** Two TB-10
    tests used hermes as their binary-export example. Once hermes stopped shelling out,
    one failed for an unrelated reason (its scripted runner response fell through to the
    *next* session) and the other would have passed **vacuously** — hermes never reaches
    the temp-file path it asserts about. A vacuous pass is worse than a failure: it is a
    guard that reports success while guarding nothing.
    → When you divert a code path, audit every test that used it as an example, and ask
    whether each still *can* fail.

## Bug closed: TB-11 — hermes contributed zero tool calls

`agentsview session export hermes:<id>` returns `rc=0` and streams
`~/.hermes/state.db` verbatim (37,175,296 bytes, identical sha256 for every id).
TB-10's NUL sniff correctly demoted these to `skipped_roots`, so runs completed — and
hermes silently contributed **0 calls to every report**, never appearing in the agent
breakdown.

Sharper than the ticket's diagnosis: the export streams the **default profile's**
database. Two of the 29 in-corpus sessions live in `profiles/aphrodite-mood/state.db`,
so for those, AgentsView returns success plus a database containing **zero rows for the
requested session**. A *fixed* export would still not reach them. Reading the archive
directly recovers strictly more than repairing the tool upstream would.

Fix: `passive._parse_ref` routes `agent == "hermes"` to `toolbench/hermes.py`, which
resolves the session across every profile database (read-only, `mode=ro`) and joins
`tool_calls[].id → messages.tool_call_id`. Discovery stays with AgentsView (finding 8).

Verified on the live archive: **29 sessions, 176 tool calls, 0 dangling, 0 malformed** —
independently corroborated by hermes' own `sessions.tool_call_count` column, which
agrees on all 29. Recovers the corpus's only MCP-tool data (16 `mcp_dash0_*` calls).
145 tests, ruff clean, mypy --strict clean.

## Config decisions (see run-state.md decision log)
- Delegators Sonnet; Result Validator downgraded Opus→Sonnet at 76% weekly usage.
- Leave-at-review (no auto-merge). One delegate pane (well under surface cap).
