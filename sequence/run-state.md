# run-state — tool-benchmarks (Tone arc)

The resume anchor for the whole Tone arc. Every stage reads this first on invoke
and resumes from what is recorded here.

## Mode

**Brownfield initiation, in place.** The repo already exists and already ships
its architect-stage contract (`SPEC.md`, `EVALUATION.md`, `BUILDPLAN.md`, 613
green tests). This run **backfills the missing initiation artifacts** —
`PHILOSOPHY.md`, `sequence/USER_STORIES.md`, this run-state, and a repo-derived
research dossier — reverse-engineered *from the existing code and docs*. No
existing code is touched; only new artifacts are written.

## Current position

- **Stage:** tone-initiation (Stage 1 of 4).
- **Phase:** 3 → 4 (Philosophy & stories written; awaiting the stories-review touchpoint).
- **Next:** client reviews `USER_STORIES.md`; then hand off to `tone-prototype`.

## Keystone decisions

| Decision | Resolution | One-line why |
|---|---|---|
| Subject of the arc | **`tool-benchmarks` itself**, not a new project | Client: "get the tone of tool-benchmarks down from existing repo." |
| "My tools" | The scripts/tools whose efficiency the harness *measures* | Client picked "tools/scripts I author"; the repo's purpose is measuring exactly that. |
| "Efficient" dimensions | **Context/token cost** (primary) + **success & retries** | Client selection; matches the repo's own metric roles (S19). |
| Core deliverable | **Measure → optimize → re-measure loop** | Client selection; measure exists today, trend + optimize are named follow-ups. |
| Home | **In place, this repo** | Reuses the shipped measurement substrate; a new repo would rebuild parsing. |
| Research depth | **Repo-derived, no external market fan-out** | Source of truth is the existing repo; a market survey would be disproportionate. |
| Economics | **N/A — no `ECONOMICS.md`** | Internal harness, no P&L; economics are not load-bearing here. |

## Decisions log

- Interpretation of the commission was confirmed by the client's second message
  and locked; validation happens at the Phase-4 stories touchpoint.
- Philosophy and stories are *derived* from `docs/2026-07-07-tool-benchmarks-design.md`,
  `SPEC.md` (S1–S41), `BUILDPLAN.md`, and `README.md` — cited, not invented.
- Forward-looking stories (the optimize + trend halves the client wants) are
  minted from the design doc's own "Open follow-ups (out of scope for v1)" plus
  the repo's named open defects (TB-17, TB-28, cursor parser), and tagged
  `forward` so they never read as as-built.
- Consistency audit passed pre-handoff: AC-1..AC-38 unique + gapless, every
  story carries ≥1 criterion, every `S`-reference in the stories resolves to a
  real SPEC criterion. c11 surfaces raised for the stories-review touchpoint
  (USER_STORIES flashing; PHILOSOPHY / run-state / dossier tabbed beside it).
- **Philosophy amendment (client, touchpoint):** true north reframed from
  "honest measurement / never a healthy zero" to an **SRE discipline** — one
  honest metric, held to a budget, driven along a trajectory. Honesty survives
  as the *trust-your-instrument* integrity tenet, no longer the headline. An
  explicit boundary states we borrow SRE discipline, not its live-monitoring
  machinery (offline, read-only analyzer). `PHILOSOPHY.md` rewritten; the
  `SLI discipline` cross-reference in USER_STORIES AC-36 updated. Proposed —
  awaiting client confirmation of the flavor.
- **Brand-voice steer (client, touchpoint):** `PHILOSOPHY.md` recast in a
  **cyberpunk / AI-age register** — "reliability engineering for the AI age,"
  agents as the automated night shift, context as the scarce currency, the
  harness as the gauge bolted to the machine. Voice only: the SRE spine, every
  `S`-citation, and the offline-analyzer boundary are unchanged; register goes
  on the framing, never the meaning. This becomes the seed brand voice
  `tone-prototype` inherits (that stage formally pins voice). Proposed —
  awaiting client confirmation.
- **Voice consolidated (client, touchpoint):** the SRE ∩ cyberpunk fusion is
  welded by three threaded motifs — **ever-present imminent crisis** (one tool
  call from the edge), **trust, but verify** (the ethos: trust the machine to
  run, verify every number), and **the toolbelt** (the harness is what you reach
  for when the crisis lands). "Ghost in the transcript" is load-bearing —
  silent zeros are dead sensors reading clean (principle 2). Still voice only;
  substance + `S`-cites intact.
- **US-15 folded in, US-16 proposed (informed default while client iterated
  voice):** the co-authored philosophy now headlines *a hard budget / a gate*,
  so the budget-and-regression-gate story (US-15, AC-39..41) is required for
  artifact coherence, not optional — folded in as `forward`. US-16
  (cost-attributed-to-task, AC-42..43) has weaker signal; added tagged
  `proposed`, droppable/promotable on the client's word. Story set is now
  US-1..US-16 / AC-1..AC-43. Client's final **lock** on voice + story set is the
  only thing still pending; the touchpoint stays open for it.
- **Voice re-based to Michelle's own profile (client, touchpoint):**
  `PHILOSOPHY.md` rewritten in the voice at
  `~/Claude/Projects/Interviewing/writing-voice-profile.md` — warm-first-person +
  engineer's-ledger, provocative-thesis opener (client-set: "I'm obsessed with
  optimizing tool usage"), her actual principle-spine ("no silent zeros: never
  let a missing signal read as a healthy one" — kept as the spine under the
  opener),
  self-label ("observability-and-
  reliability-geek"), agency-forward close ("set that budget, evolve it,
  maintain it"). Per the profile's marker 3a (*accurate mechanism beats vivid
  color*), the free-floating cyberpunk atmosphere was dialed down and real
  mechanism up; vivid metaphor kept only where it carries truth (the ghost = a
  literally missing record). No named war-story fabricated — the repo's real
  mechanisms are the ledger evidence. **This profile is the seed brand voice
  `tone-prototype` inherits** (supersedes the invented cyberpunk cast).
  Substance + all 16 `S`-cites intact.
- **Sub-session tracing / unified-world vision added (client, touchpoint):** a
  run is a tree of sessions + subagents, not one session. Added to the one-thing
  framing and as PHILOSOPHY principle 11 ("Trace the whole tree — a run is a
  world, not a session"): distributed tracing aimed at agents, stitching the
  fan-out into one accounting — subagent transcripts as real tool use (S13),
  per-*entry* run attribution by `gitBranch` so straddling sessions (29/158)
  aren't double-counted, detached-`HEAD` to its own bucket (S40). New story
  **US-17** (AC-44..47) bridges: as-built tracing substrate (S13/S40/S33) + one
  `forward` AC for the loop's unified per-tool fan-out view. Set now
  US-1..US-17 / AC-1..AC-47, 11 principles; audited clean, all `S`-cites resolve.

## Touchpoints

| # | Phase | Purpose | Status |
|---|---|---|---|
| Commission | 0 | Confirm subject + keystone forks | **done** (2 rounds) |
| Stories review | 4 | Client validates `USER_STORIES.md` | **PAUSED — awaiting client lock. Everything I own is done (artifacts written, 3 voice rounds applied, audited clean, US-15 folded / US-16 proposed). Handoff to `tone-prototype` is ARMED: fires on "locked" / "locked, drop US-16".** |

## Per-phase stats (proportion, not spend)

| Phase | Agents spawned | Human touchpoints | Notes |
|---|---|---|---|
| 0 Commission | 0 | 2 | Keystone forks resolved via `ask` + dialogue. |
| 1 Research | 0 | 0 | Repo-derived dossier; external fan-out shrunk (recorded above). |
| 2 Refine | 0 | 0 | Positioning folded into dossier; no economics. |
| 3 Philosophy & stories | 0 | 0 | `PHILOSOPHY.md` + `USER_STORIES.md` written. |
| 4 Review | 0 | 1 (pending) | — |
