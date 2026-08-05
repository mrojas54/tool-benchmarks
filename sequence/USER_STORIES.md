# USER STORIES — tool-benchmarks

Stories with stable acceptance-criterion IDs (AC-N), minted here and carried
unchanged downstream (prototype → evaluation → spec → tickets → validation), so a
dropped criterion is mechanically visible.

**Lineage note.** The *as-built* stories (US-1 … US-9) capture behavior already
shipped; each AC cross-references the existing `S`-criterion in `SPEC.md` that
already verifies it, so the two ID spaces are traceable both ways. The *forward*
stories (US-10 … US-16) capture the measure→optimize→re-measure loop the client
wants; their ACs have **no `S`-criterion yet** and are the work the
prototype/architect stages will design and spec. Most are tagged `forward`;
US-16 is tagged `proposed` — offered but not yet confirmed by the client, drop
or promote on their word. **US-17 bridges** — the sub-session tracing substrate
is as-built (AC-44…46, S13/S40/S33), the unified per-tool fan-out view is
`forward` (AC-47).

`felt` = a criterion whose truth is experiential (a human must look), a candidate
for a human-use checkpoint the architect stage will schedule.

---

## As-built: the honest snapshot

### US-1 — See where tooling wastes context, across agents
*As an operator running agents across Claude Code, Codex, and Hermes, I want a
single markdown report of which tools/agents/projects dump the most context back
into sessions, so I can target the worst offenders.*

- **AC-1** A passive scan emits a five-section markdown report — agent breakdown,
  tool leaderboard, model breakdown, inefficiency callouts, summary — in that
  order. (S14)
- **AC-2** Context cost = joined tool-result payload tokens (`chars/4`) is the
  sole ranking metric; cache flags never rank, failure/slow/churn feed callouts
  only. (S19)
- **AC-3** Every aggregate is keyed by agent as well as tool; agents are never
  collapsed into one bucket. (S4, S14)
- **AC-4** Aggregation streams per session; no whole-corpus `list[ToolCall]` is
  built in memory. (S11)
- **AC-5** `felt` — a reader can tell from the report alone which tools to fix
  first, and trusts the ranking without re-deriving it. (report legibility)

### US-2 — Trust the number: no silent zeros
*As an operator, I want every undercount, absence, and uncertainty named in the
output, so I never act on a healthy-looking zero that is really a missing
measurement.*

- **AC-6** Skipped sessions carry a typed `SkipReason`, and the summary renders a
  reason histogram; no prose-parsed skip line. (S34, S35)
- **AC-7** The summary reconciles discovery as `discovered = scanned + skipped`,
  never a third drifting count. (S35)
- **AC-8** Tool records a parser recognizes but cannot join are counted and named
  (`Unjoinable tool records (seen, not joined)`), never dropped or faked as
  orphans. (S38)
- **AC-9** Usage absence is typed at the source (`UsageProvenance`), and the
  cache column renders `n/a` / `n/a*` / `no` distinctly. (S29)
- **AC-10** "Unmeasured" is distinct from measured zero (SQL `NULL` vs `0`) for
  every session-grain cache stat. (S32, S39)

### US-3 — Diff two reports and trust the delta
*As an operator, I want a report's change between runs attributable to a code
change and not to corpus drift, so I can prove a change did something.*

- **AC-11** The summary carries a corpus fingerprint (`sha256` over per-session
  signatures folding identity + call/malformed/unjoinable counts) and the scanned
  count. (S36)
- **AC-12** `--freeze <manifest>` pins the corpus: first run writes the ref list;
  later runs replay it, bypassing live discovery, and name refs vanished since
  freeze. (S37)
- **AC-13** Over an unchanged corpus a `--freeze` replay is byte-identical,
  fingerprint line included. (S37)
- **AC-14** Manifest v2 persists the freeze-time census; replay discloses
  historical fractions with an explicit "not current" caveat, or marks them
  unavailable rather than inventing a denominator. (S37/TB-37, S41)

### US-4 — Read each agent's schema honestly
*As an operator, I want each runtime's transcript parsed by its own schema, and
unrecognized ones skipped loudly, so no agent is silently reported as a healthy
zero.*

- **AC-15** Schema is content-sniffed (Hermes SQLite excepted, keyed on source);
  no parser is the default; two matches raise `AmbiguousSchema`, zero raise
  `UnknownSchema` and skip. (S27, S28)
- **AC-16** Codex is parsed (`CodexParser` joins three paired shapes on
  `payload.call_id`); `cursor` skips pending its own parser and is named as such.
  (S33, S28)
- **AC-17** Hermes is read read-only from its archive; cache is surfaced at
  session grain only, never invented as a per-call rate; its under-sampling is
  recorded as an upstream defect, not routed around. (S9a, S9b, S32)

### US-5 — Prove tool-vs-Bash cost on a fixed corpus
*As an operator, I want controlled matched-pair probes over a committed corpus,
so tool-vs-shell cost claims are reproducible from a clean checkout.*

- **AC-18** The probe corpus is five files vendored under `tools/` (log-spaced
  ~121 → ~2,242 lines); probe output lands under `reports/`, never mixed with
  inputs. (S16)
- **AC-19** Arms match by distinct evidence (tool arm structurally; bash arm by
  sentinel); a fully-seeded table raises `SeededReportError` unless
  `--allow-seeded`. (S17, S18)
- **AC-20** Usage is attributed only when a turn is isolable (one `tool_use`, no
  prose/reasoning), keyed solely by `requestId`; hermes-trace input is refused.
  (S26, S30)

### US-6 — Measure tokens-to-outcome, not just per-call
*As an operator, I want to know which toolset reaches a verified fix for the
fewest context tokens, so I can compare paths, not just individual calls.*

- **AC-21** The complex probe scores a trial by `LOCATED:` + oracle across
  toolset arms in a hermetic worktree; the prompt is always `PROMPT.md`, never
  the defect rationale (no winner leak). (complex library)
- **AC-22** Arms are enforced by transcript read-scope audit, not filesystem
  walls; any resolved read outside the trial tree voids the trial. (complex library)

### US-7 — Attribute cache cost to a specific run
*As an operator dispatching orchestrated runs, I want per-run cache-token totals,
so I can cost a run without fabricating attribution.*

- **AC-23** `--run-manifest` attributes usage per transcript **entry** by
  `gitBranch` (not per session); detached `"HEAD"` usage is booked to a named
  `detached_*` bucket, never folded into the run total or dropped from the
  report/accounting. (S40)
- **AC-24** A manifest branch matching zero entries is reported, never a silent
  zero; `session total == sum of buckets` holds. (S40)

### US-8 — Know how much of the corpus you actually sampled
*As an operator using `--limit`, I want each agent's sampled fraction disclosed,
so I never mistake a narrow window for the whole archive.*

- **AC-25** The agent breakdown carries a `sampled` cell (numerator / census /
  fraction); agents present but unreached still get a row (`sessions == 0` reads
  as looked-and-found-none). (S41)
- **AC-26** When sampling is uneven, the cause is apportioned between truncation
  and attrition from observed signals only; cross-agent ratios are trustworthy
  only when no uneven-sampling line prints. (S41)

### US-9 — Run it anywhere, gate it strictly
*As a maintainer, I want the default harness to run with no third-party runtime
deps and pass one strict gate, so it is portable and every test is collected.*

- **AC-27** The default `toolbench` installation imports no third-party runtime
  packages and is runnable via `uv run toolbench passive|probe` and
  `python -m`; the optional `tracing` extra adds Laminar observability without
  changing the default install. (S20, S21)
- **AC-28** The gate is `uv run ruff check .`, `uv run mypy --strict src/toolbench
  tests`, and `uv run pytest -q` (not `unittest discover`, which under-collects);
  all green before a PR. (S22, S31)

---

## Forward: from snapshot to loop `forward`

These stories are the client's stated goal — *make my tools more efficient, and
know how efficient they're getting*. They have no `S`-criterion yet; the
prototype and architect stages design and spec them. They must inherit the one
thing (PHILOSOPHY): every comparison names every way it could be lying.

### US-10 — Track efficiency over time `forward`
*As an operator, I want to see a tool's / agent's context-cost and failure-rate
trajectory across dated runs, so I can tell whether things are actually getting
more efficient.*

- **AC-29** `forward` Given ≥2 frozen/fingerprinted runs, the harness emits a
  trend for a chosen tool/agent (context cost + failure rate per run).
- **AC-30** `forward` A trend point is comparable to its neighbours only across a
  shared basis; a drift, sampling, or comparability caveat is attached to any
  point that lacks one (inherits AC-11/AC-14/AC-26). No trend line implies a gain
  it cannot defend.
- **AC-31** `forward` `felt` — a human looking at the trend can tell "getting
  better / worse / flat" at a glance, with the caveats legible, not buried.

### US-11 — Recommend concrete efficiency changes `forward`
*As an operator, I want the harness to point at specific, actionable
inefficiencies in the tools I author (oversized outputs, retry loops, deferral
tax) and recommend a change, so I know what to fix — I approve, it does not
auto-apply.*

- **AC-32** `forward` For a targeted tool, the harness surfaces its top cost/
  failure drivers with evidence (which calls, how many tokens, what failed) and a
  recommended change.
- **AC-33** `forward` Recommendations are advisory only; nothing mutates a tool
  or a transcript without the operator applying it (inherits the read-only guard).

### US-12 — Close the loop: baseline → change → prove `forward`
*As an operator, I want to baseline a tool, make a change, and have the harness
prove whether the change reduced context cost / failures, so improvement is
evidence, not vibes.*

- **AC-34** `forward` Given a before-corpus and an after-corpus, the harness
  reports the signed delta in context cost and failure rate for the changed tool,
  with the shared-basis caveats required by AC-30.
- **AC-35** `forward` A change with no significant effect, or a regression, is
  reported as such — never rounded up to a win.

### US-13 — Treat success/retries as a first-class efficiency axis `forward`
*As an operator, I want failure and retry cost tracked with the same honesty as
token cost (the client's second chosen dimension), so a "cheaper" tool that fails
more is not mistaken for an improvement.*

- **AC-36** `forward` The loop reports token cost and failure/retry cost together
  for any comparison; a token win paid for by a reliability regression is flagged,
  not hidden (inherits PHILOSOPHY's *SLI discipline* principle: one signal ranks,
  reliability rides beside cost — surfaced together, never traded silently).

### US-14 — Remove known comparability blind spots `forward`
*As a maintainer, I want the repo's named open defects that block honest
comparison closed before the loop leans on them, so the loop does not inherit a
known lie.*

- **AC-37** `forward` TB-17 (probe usage cells inflated by bash instrumentation)
  is either corrected with a stated adjustment or the usage pair is dropped, so
  usage columns become comparable.
- **AC-38** `forward` TB-28 (detached-`HEAD` attribution blind spot) and the
  `cursor` parser gap are resolved or explicitly excluded from any loop claim that
  would otherwise silently depend on them.

### US-15 — Budget a tool and gate on regressions `forward`
*As an operator, I want to set a context-cost / failure-rate ceiling on a tool
and have a run fail when the tool crosses it, so the trajectory becomes an
enforced error budget, not just a chart I look at.*

- **AC-39** `forward` A tool can carry a budget (a context-cost and/or
  failure-rate ceiling); a run that measures the tool over it reports a budget
  breach, with the breaching evidence (which calls, how far over).
- **AC-40** `forward` The breach can gate a run (non-zero exit / explicit fail
  state) so CI can enforce it; the gate fires only on a comparison with a shared
  basis (inherits AC-30) — never on a delta the harness cannot trust.
- **AC-41** `forward` A budget defined against an unmeasured or non-comparable
  signal refuses to gate and says so, rather than passing or failing silently
  (inherits PHILOSOPHY's *trust, but verify*).

### US-16 — Attribute a tool's cost to the task it was doing `proposed`
*As an operator, I want a tool's context cost separable by the task/workflow it
was serving, so "expensive" is distinguishable from "expensive-for-what."*

- **AC-42** `proposed` A tool's cost can be grouped by an available task/workflow
  signal in the transcript (e.g. project, run branch per S40, or session
  intent), so a costly-but-rare path is not conflated with a cheap-but-constant
  one.
- **AC-43** `proposed` When no reliable task signal exists for a call, its cost
  is booked to a named `unattributed-task` bucket, never silently spread across
  tasks (inherits the no-silent-zeros stance, S40).

### US-17 — Trace a run through its sub-sessions into one world
*As an operator whose runs fan out into subagents and across branches, I want a
run's tool cost traced through every sub-session and stitched into one
accounting, so I optimize the real total and not the slice I happened to open.*
This story **bridges**: the tracing substrate is as-built (AC-44…46, each with an
`S`-criterion); the unified per-tool view for the loop is `forward` (AC-47).

- **AC-44** Subagent transcripts (`<project>/<session-uuid>/subagents/*.jsonl`)
  are counted as real tool use and attributed to the owning project, with
  `is_subagent` set at discovery; `--exclude-subagents` drops them by that flag,
  never by a path-substring afterthought. (S13)
- **AC-45** A run's cost is attributed per transcript **entry** by `gitBranch`,
  so a session straddling branches (29/158) is split correctly and never
  double-counted; detached-`HEAD` work is booked to its own named bucket, never
  folded into the run total or dropped from the report/accounting. (S40)
- **AC-46** Subagent fan-out is surfaced as an inefficiency callout naming its
  top-offending spawn primitive (codex `spawn_agent`, not `wait_agent`). (S14/S33)
- **AC-47** `forward` For a targeted tool, the loop presents its cost unified
  across a whole fan-out — parent plus subagents plus straddling branches — as
  one number, so optimization targets the true total; any sub-session that could
  not be traced is named, never silently omitted (inherits no-silent-zeros).
