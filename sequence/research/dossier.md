# Research dossier — tool-benchmarks

**Type:** brownfield capture. The "landscape" being surveyed is the existing
repo, its design snapshot, and the provenance it cites. External market research
was deliberately not fanned out (see `run-state.md` → Keystone decisions):
`tool-benchmarks` is an internal methodology harness, not a market-facing
product, and its differentiation is methodological, not competitive.

Sources read for this dossier:
`docs/2026-07-07-tool-benchmarks-design.md`, `SPEC.md` (S1–S41), `BUILDPLAN.md`,
`README.md`, `AGENTS.md`.

## 1. The problem and the people

**The problem.** Agent work silently wastes context, time, retries, and tool
calls, and there is no re-runnable, evidence-grounded way to see *where*. The
cost is real but invisible: a tool that dumps a large result payload back into a
session burns the context window on every call; a flaky tool that retries burns
turns; a deferred-tool-loading step (`ToolSearch`) taxes discovery. Across a
corpus of thousands of sessions these costs compound, but they hide inside
prose-shaped transcripts that no one audits.

**The people.**
- **Primary:** the operator/author (the client) who runs agents across Claude
  Code, Codex, Hermes, and other AgentsView-supported runtimes, authors the
  scripts/tools those agents invoke, and wants to know which of their tools are
  expensive or unreliable — and whether changes actually help.
- **Secondary:** anyone reasoning about agent-tooling efficiency who needs
  evidence rather than intuition (the methodology descends from claude-mem
  observation **#8376**, the native-tool-vs-Bash benchmark).
- **Acuteness:** chronic, not acute — a steady tax rather than a fire. Observed
  scale (AgentsView, 2026-07-07): **8,103 sessions / 101,919 messages / 86
  projects**; heavy sessions exceed 90 tool calls and 86 turns. At that scale
  the tax is worth instrumenting.

**How it's solved today (the alternatives / competitive landscape).**
- **Nothing systematic** — intuition and eyeballing transcripts.
- **`session-report` skill** — rich HTML session views, but not cross-agent
  cost aggregation; deliberately out of scope here (this repo is markdown-only,
  so it does not compete with it).
- **AgentsView CLI** — a cross-agent *index* (session list, stats, export), not
  a metric engine. This harness uses it as a discovery/index layer and keeps the
  source parsers authoritative for cost.
- **Live token APIs** — could price usage but require network and mutate nothing
  offline; explicitly a non-goal (all numbers derive from on-disk transcripts).

## 2. The external system the user also lives in

The harness reads *other systems' transcripts*, so the integration seam is the
on-disk/exported schema of each agent runtime — and each schema is a black box
that lies differently:
- **Claude Code** — `~/.claude/projects/**/*.jsonl`; tool_use/tool_result joined
  by id; usage per response (pooled across entries sharing a `requestId`).
- **Codex** — multi-root (incl. archived); three paired `response_item` shapes
  joined on `payload.call_id`; bills tokens per turn (no per-call usage);
  `web_search_call` is unjoinable (~4% undercount, surfaced not dropped).
- **Hermes** — SQLite archive read directly (AgentsView `export` returns the
  whole profile DB); cache at session grain only; under-sampled by `session
  list` vs `stats` (upstream defect, not routed around).
- **Cursor** — recognized but unclaimed; lands in `skipped_roots` pending a parser.
- **AgentsView** — the cross-agent index; can hang, exit nonzero mid-listing, or
  return schema-invalid payloads, all of which must degrade to raw scanning
  without splicing partial results.

The seam lesson the repo already learned: **schema is a property of the payload,
not the producer** — content-sniff, never assume; no default parser; unknown
schemas skip loudly rather than report a healthy zero.

## 3. Economic viability

**Not load-bearing.** Internal harness, no P&L, no external users to price. No
`ECONOMICS.md`. The only "cost" that matters is the one it measures — context
tokens — and the only "budget" is the strict quality gate. Recorded here so the
absence is deliberate, not an omission.

## Refine (Phase 2) — positioning, scope, differentiation

**Positioning.** A re-runnable, offline, standard-library-only harness that
turns agent session transcripts into a single markdown report of *where tooling
wastes context* — trustworthy because every way a number could mislead is named.

**What it is.** Passive analyzer (cross-agent cost + inefficiency callouts) +
active tool-vs-Bash probes + a locate-then-fix complex probe (library).

**Differentiation (the moat is honesty, not features).**
- **No silent zeros.** Undercounts, absences, and uncertainty are typed and
  surfaced, never laundered into a healthy-looking number. This is the repo's
  explicit signature failure to avoid.
- **Reproducible/diffable.** Corpus fingerprint + `--freeze` mean two reports'
  delta is attributable to a code change, not corpus drift.
- **Offline + stdlib-only.** Runs anywhere `python3 ≥3.13` exists; no network,
  no third-party runtime deps, read-only over all sources.
- **One primary metric.** Context cost ranks; everything else is caveat/callout.

**Smallest version that delivers value (already shipped):** the passive analyzer
emitting the five-section report over Claude Code transcripts. Everything else
(Codex/Hermes parsers, probes, freeze, run-manifest) is accreted value.

**The gap the client wants closed (forward work).** The design doc's own "Open
follow-ups": **trend tracking across dated report runs** ("know how efficient
they're getting") and an **optimize** step (recommend/prove changes). These are
the measure→optimize→re-measure loop — the measure half exists; the trend and
optimize halves are forward stories.

**Defensible "don't build more" positions to hold at architect stage:** no HTML,
no live API, no web-chat, no third-party deps, no transcript mutation — these
scope guards are load-bearing to the "runs anywhere, trust the number" promise.
