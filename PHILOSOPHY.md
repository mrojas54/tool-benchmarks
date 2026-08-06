# PHILOSOPHY — tool-benchmarks

## The one thing

**I'm obsessed with optimizing tool usage.** In the AI age the operators are
agents, the currency is context, and every tool call spends it — so optimizing
that spend is the whole game: drive the cost down, keep it down, prove it stayed.
But you can't optimize what you can't trust, and that's where most tooling dies.
So one rule holds everything up:

> **No silent zeros: never let a missing signal read as a healthy one.**

Same observability-and-reliability-geek energy, aimed at a new machine: one
honest gauge, a hard budget, a line I drive down. Trust the machine to run — then
verify every number before you spend a decision on it. A number you can't trust
is worse than no number: it lies with confidence, and it gets someone paged for
the wrong thing.

And a run is never one session — it's a tree of them, subagents and all. I trace
the whole fan-out into one world, because the cost you're optimizing is smeared
across the tree, not sitting in the one session you happened to open.

## The boundary — the discipline, not the machinery

Let me come clean about what this isn't. It's not a production-monitoring rig,
and I won't dress it up as one — no live service, no uptime SLA, no alerting, no
on-call, no 3 a.m. pager. The crisis it's built for is the slow one: the context
tax you can't see until you audit the tape. So it reads the black boxes after the
fact — 8,103 sessions, 101,919 messages of transcript on disk, never touched. It
carries SRE's *discipline* — SLI selection, error budgets, regression detection,
blameless evidence, toil reduction — and skips the real-time hardware. Read-only
is the ethos: watch the machine, verify the record, leave no fingerprints.

## Principles

1. **SLI discipline — one signal in the noise.** Context cost (joined
   tool-result payload tokens, `chars/4`) is the only thing that ranks. Cache
   flags are caveat-only; failure / slow / retry-churn feed the inefficiency
   callouts, never the leaderboard (S19). Reliability — success and retries —
   rides shotgun beside cost and never gets traded for it in silence. A column
   that fuses signals is a column that lies.

2. **Trust, but verify — hunt the ghost in the transcript.** A gauge you take on
   faith lies with confidence. Every silent zero is a ghost in the transcript — a
   dead sensor reading clean — so the rig names each one instead of swallowing
   it: unjoinable records counted and shown (S38); skips stamped with a typed
   reason (S34); discovery reconciled as `discovered = scanned + skipped` (S35);
   unattributable per-run cost booked to a named bucket, never folded, never
   dropped (S40). A "healthy zero" that's really a haunted sensor is the outage
   this whole thing exists to prevent — the project's named signature failure.

3. **Type the absence at the source.** Uncertainty is first-class, decided where
   the evidence lives, not reverse-engineered from prose downstream.
   `UsageProvenance` (`PRESENT` / `ABSENT_BY_SCHEMA` / `ABSENT_BY_EXPORT` /
   `ABSENT_UNEXPECTED`, S29); `SkipReason` as a typed enum (S34); "unmeasured" as
   SQL `NULL`, a different fact from a measured `0` (S32/S39). Wire the gaps,
   don't guess them.

4. **Regression detection — the diff is the unit of trust.** A number you can't
   re-derive is a rumor. The corpus is fingerprinted and `--freeze` pins it, so a
   report's delta is chargeable to a code change, not to a corpus that slid out
   from under you mid-scan (S36/S37). Before/after comparability is the whole job;
   a snapshot alone proves nothing.

5. **Error budgets and trajectory — you drive the line, you don't admire it.**
   Efficiency is a number I push down on purpose. A tool gets a budget — a
   context-cost / failure-rate ceiling; crossing it is a regression a gate catches
   before it ships; the trajectory across runs is the verdict on whether the
   discipline is working (US-10/US-12/US-13/US-15). "50% cheaper" is a claim only
   when the rig can name every way the comparison could be conning you: drift,
   uneven sampling, unattributable cost.

6. **Comparisons are earned, not assumed.** Two numbers compare only across a
   shared basis. Cross-agent ratios hold *only when no uneven-sampling line
   prints* (S41); a known-incomparable pair — probe usage inflated by bash
   instrumentation, TB-17 — is flagged as an open defect, never dressed up as a
   result.

7. **Safe, boring operations.** Never mutate a transcript or a source project. No
   live token-API calls — every number comes off on-disk records. Python standard
   library by default, so it runs wherever `python3 ≥3.13` boots (S20); the
   optional `tracing` extra explicitly adds Laminar observability without changing
   the default install. Aggregate incrementally; never load a whole-corpus
   `list[ToolCall]` into memory (S11). At 8k+ sessions, memory discipline and
   leaving no trace are correctness, not polish.

8. **Schema is a property of the payload, not the producer.** Content-sniff the
   transcript; no default parser; an unrecognized schema skips loudly instead of
   falling through to a parser that matches nothing and reports a healthy zero
   (S27/S28). Don't trust the label on the crate — open it and verify.

9. **Name the toil, don't route around it.** Upstream defects — Hermes
   under-sampling, the missing `cursor` parser — are logged as upstream/open, not
   quietly patched in a way that skews a cross-agent rate. Naming the toil is how
   it dies instead of compounding in the dark.

10. **Scope discipline is a reliability feature.** No HTML (the `session-report`
    skill owns that), no live API, no web-chat, no unrelated third-party deps in
    the default runtime, no unrelated refactors. The optional `tracing` extra is
    an explicit Laminar exception. Each guard defends the "runs anywhere, trust
    the number" promise; loosening one is a philosophy amendment, not a shortcut.

11. **Trace the whole tree — a run is a world, not a session.** Agent work fans
    out: a run spawns subagents (`<project>/<session-uuid>/subagents/*.jsonl`,
    S13) and straddles branches — 29 of 158 sessions cross more than one, and
    delegators don't always land in a clean worktree, so no single session or
    `cwd` owns the cost. I stitch it back into one world: subagent transcripts
    counted as the real tool use they are (S13), per-run cost attributed per
    *entry* by `gitBranch` so a straddling session is never double-counted (S40),
    `--exclude-subagents` a deliberate cut and never a silent drop. This is
    distributed tracing, aimed at agents — you can't optimize a tool by staring
    at one session when its real cost is smeared across the tree.

## Taste

Smells, not bugs — the calls a discerning contributor makes when no rule forces
their hand.

- **Guardrails are enforced, not trusted.** An unenforced invariant is a future
  incident wearing a nice comment. I audit every non-negotiable with its own
  pass/fail check — trust-but-verify turned on my own code.
- **The rationale ships with the rule.** Every SPEC criterion says *why* the gap
  was closed and what would reopen it. A rule stripped of its reasoning rots, so I
  leave the thinking in the record — the next person inherits the reasoning, not
  just the fix.
- **Brownfield reconcile before minting work.** Audit what's already wired
  (DONE / PARTIAL / MISSING with file:line), then plan only the gap. This document
  is that reflex turned on the project's own voice.
- **An honest blank beats a confident fabrication.** I'd rather hand you a blank I
  can defend than a number I can't. A session-grain cache figure never gets
  divided by call count to fake a per-call rate (S32); a non-isolable probe arm
  shows `—`, not a re-seeded guess (S18).
- **Never re-parse your own output to recover a fact.** `detail` is for a human's
  eyes; `reason` reads from the typed enum, never regexed back out of the prose
  that carried it (S34). Structure it once, upstream — reconstructing it
  downstream by string-match is the seam drift crawls in through.
- **A caveat travels with the number it qualifies.** Frozen-census fractions
  carry their "historical, not current" note right beside them (S37); the
  uneven-sampling warning prints with the very ratios it undermines (S41). A
  caveat stranded three sections from its number gets missed in the one moment it
  matters.

## The part I care about most

Make my tools more efficient — and actually *know* they're getting there. That's
what turns this repo from a snapshot into a loop: baseline, change, prove the
change held, watch the line — with a budget that makes the trend a gate, not a
chart I nod at. I want to set that budget, evolve it, and maintain it. Whatever
the loop becomes, it inherits the one thing: the discipline is only as good as the
honesty of its instrument. A budget enforced against a number you can't trust is
theater with better lighting — and the crisis it was supposed to catch walks right
through it.
