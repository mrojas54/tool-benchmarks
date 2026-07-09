# TB-6 plan — `probe.py` + `active-probes.md`

SPEC: S16, S17, S18. BUILDPLAN anchor: T5. Depends on T2 (TB-3, already merged
into this branch's base `origin/tb-3-parse`).

## Design decisions

**Probe corpus (S16).** Five files already vendored under `tools/`
(unchanged, not re-vendored): `regex_check.py`, `mcp.py`, `monitor.py`,
`llm_extraction.py`, `code_analysis.py`. Each gets exactly one probe = a
matched tool-arm-vs-Bash-arm pair, alternating task type by size so both
seeded task types (`search`, `find`) are exercised:

| id | corpus_path | task | tool_name (tool arm) |
|----|-------------|------|------------------------|
| 01 | tools/regex_check.py | find | mcp__serena__find_file |
| 02 | tools/mcp.py | search | mcp__serena__search_for_pattern |
| 03 | tools/monitor.py | find | mcp__serena__find_file |
| 04 | tools/llm_extraction.py | search | mcp__serena__search_for_pattern |
| 05 | tools/code_analysis.py | find | mcp__serena__find_file |

`protocols/active-probes.md` documents this table (five relative paths,
task, tool_name, and both sentinels per probe). Probe *output* (the
comparison table) is written under `reports/` (created at run time, gitignored
— it's generated, not a corpus input); never mixed into `tools/`.

**Sentinels (S17).** Zero-padded numeric probe id + fixed arm suffix:
`TB_PROBE_<id>_TOOL_V2` / `TB_PROBE_<id>_BASH_V2` (10 total). Zero-padding
(`01`..`05`) plus the fixed `_TOOL_V2`/`_BASH_V2` suffix makes a pairwise
substring collision structurally impossible (the padded digit run guarantees
a mismatched character immediately after the shared `TB_PROBE_0` prefix for
any two distinct ids). Verified by an explicit pairwise test, not just
asserted by construction.

`find_probe_calls(path, probes)` cannot use `ToolCall` alone for sentinel
verification — `transcript.py` intentionally normalizes tool input/output to
character counts (`result_len`) and drops raw text, so a `ToolCall` has no
field a sentinel could live in. Design: `find_probe_calls`
1. does its own single-pass raw-JSONL scan (new, local to `probe.py`,
   read-only, no changes to `transcript.py`) collecting
   `(ts, name, serialized_input)` per `tool_use` block, plus a `ts` count
   for isolable-turn detection (see S18 below);
2. also calls `parse_session(path)` (the real TB-3 parser) to get the
   `ParseResult`/`ToolCall`s carrying the actual token/usage numbers;
3. matches a probe arm only when a raw block satisfies BOTH conditions —
   `sentinel in serialized_input` and `name == expected tool name` — then
   joins that arm to its `ToolCall` by `(ts, name)` (unique enough for a
   controlled probe session: one call per assistant turn).

This satisfies "uses the TB-3 parser to find probe calls" (step 2) while
keeping sentinel verification correct (step 1) without touching
`transcript.py`. Deviation flag: ToolCall's lack of an id/raw-input field is
a pre-existing TB-3 design choice, not a bug — noted here rather than filed
against TB-3.

**Comparison table (S18).** One row per probe (5 rows), each carrying both
arms side by side: `tool_tokens`/`bash_tokens` (context tokens =
`ToolCall.tokens`, i.e. `output_chars // 4`) and, when the assistant turn is
*isolable* (exactly one `tool_use` block at that turn's timestamp — usage is
per-turn, not per-call, so it's only attributable when unambiguous),
`tool_usage_tokens`/`bash_usage_tokens` from `ToolCall.usage`. When an arm has
no matching call (`find_probe_calls` returned `None` for it), fall back to
the seeded #8376 baseline for that `(task, arm)`:
`{("search","serena"): 723, ("search","bash"): 794, ("find","serena"): 68,
("find","bash"): 89}`, and mark that arm `seeded=True` in the row.

`toolbench.probe.main()` (S21 entry point) accepts an optional `--session`
path; with none given (the common case — no probe session recorded yet) every
arm is absent, so the table is 100% seeded — this is the "probe scores the
seeded table" checkpoint from BUILDPLAN. Writes a markdown report to
`reports/active-probe-comparison.md` (overridable via `--out` for tests, so
tests never touch the real `reports/` dir).

## Files to add/change

- `toolbench/probe.py` — replace the stub: `ProbeSpec`, `ArmMatch`,
  `ComparisonRow`, `PROBE_SPECS`, `SEED_BASELINES`, `find_probe_calls`,
  `build_comparison_table`, `render_report`, `main`.
- `protocols/active-probes.md` — new, the five-path table (S16).
- `tests/test_probe.py` — new.
- `tests/fixtures/probe_session.jsonl` — new, a small synthetic session
  exercising: one fully-matched arm (sentinel + right tool name), one
  right-tool-wrong-sentinel near-miss, one right-sentinel-wrong-tool
  near-miss, one isolable single-call turn (usage attributable), one
  multi-call turn (usage NOT attributable → usage omitted even though
  present).
- `.gitignore` — add `reports/` (generated probe output, not committed).
- No changes to `transcript.py`, `sources.py`, `passive.py`.

## Self-review checklist (before moving to `planned`)

- [x] Sentinel uniqueness/non-substring: verified by construction (padded
  numeric id) + will add an explicit pairwise test.
- [x] Tool-name verification is a separate AND condition from sentinel
  match, not merged into one check (S17 wording: "carries the sentinel
  **and** used the expected tool").
- [x] Seed values match #8376 exactly: search 723/794, find 68/89.
- [x] `reports/` (output) vs `tools/` (input) kept structurally separate;
  `active-probes.md` never lists a `reports/` path.
- [x] No modification to `transcript.py`/`sources.py`/`passive.py`.
