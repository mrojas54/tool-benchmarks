# TB-30: agentsview index path silently excludes 70% of the archive: --include-{children,automated,one-shot} never passed

iter_agentsview_sessions (toolbench/sources.py:153) builds:

    argv = ["agentsview", "session", "list", "--json", "--limit", str(limit)]

The design doc (docs/2026-07-07-tool-benchmarks-design.md:57) mandates:

    agentsview session list ... --include-children --include-automated --include-one-shot --limit 500 --json

None of the three --include-* flags are passed. agentsview excludes one-shot, automated, and child sessions BY DEFAULT and announces it on stderr:

    Excluded 7494 sessions by default: 7433 one-shot, 61 automated.

sources.py checks only returncode and parses stdout, so the banner is discarded.

MEASURED (full cursor pagination, live archive, 2026-07-13):
  toolbench actual call (no flags):  3534 sessions
  design-mandated (all 3 includes): 11950 sessions
  SILENTLY EXCLUDED:                 8416  (70.4% of the archive)

WHY S35 CANNOT SEE IT: the loss is UPSTREAM of discovery. The Summary prints a
perfectly balanced 'Sessions discovered: 200 / scanned: 200 / skipped: 0' because
D is already truncated. This is the TB-21 family (unreconciled corpus loss) one
level further up: not skipped, never discovered.

CONSEQUENCE FOR S10 (the real damage): auto->raw fallback is NOT corpus-preserving.
Same agent, same --limit 50, only --index-source differs:
  raw:        50 scanned, 693 calls,  fingerprint d67c5c93fc1e67e7
  agentsview: 50 scanned, 4016 calls, fingerprint e432354b624ff485
A 5.8x call-density difference. The report names the fallback reason, which implies
a PROVENANCE change; it is actually a POPULATION change. Numbers measured across a
fallback boundary are not comparable, and S37 freeze pins a different population
depending on which index source was live.

FIX: pass --include-children --include-automated --include-one-shot in
iter_agentsview_sessions; surface agentsview stderr exclusion banner rather than
discarding it. Add a test asserting the argv carries all three flags, and an
index-source equivalence test (raw vs agentsview over the same project must agree
on the discovered session-id set).

Found by: operator smoke checkpoint CP2/CP3 (EVALUATION.md), 2026-07-13.

---

## Plan (as built) — PR #53

Shipped together with TB-31; TB-30 alone is not safely shippable (admitting child
sessions while `--exclude-subagents` still cannot filter them turns a harmless no-op
into an active false claim).

1. `toolbench/sources.py` — pass `--include-children --include-automated
   --include-one-shot` on the session listing (`_ALL_INCLUDES`).
2. `toolbench/sources.py` — stop discarding agentsview's stderr. An exclusion banner on
   the full listing now raises `AgentsViewExclusionWarning`: we opt into every exclusion
   agentsview documents, so a banner means it dropped sessions we did not ask it to drop.
3. Tests — assert the argv carries all three flags.

### Verified before fixing (live archive, full cursor pagination)
    no flags (what toolbench did):  3,536 sessions
    all three includes (mandated): 11,955 sessions   -> 70.4% silently excluded

### Per-agent sampling fractions — the real severity
    claude 1934/8585 = 22.5%   hermes   99/977 = 10.1%
    codex    90/173 = 52.0%    cursor   55/73  = 75.3%
A 7.4x spread, in a project whose purpose is comparing agents.

### Deviation from the filed FIX
The ticket asked for a raw-vs-agentsview *index-source equivalence test*. Not added: the
two sources are not equivalent by construction — raw scans `~/.claude/projects` (claude
only, keyed by filesystem UUID) while agentsview indexes every agent under its own id
scheme and project naming. An equality assertion between them would encode a false
invariant. The corpus claim is instead pinned where it is actually decidable: the argv
carries all three flags, and the fingerprint A/B moves on both sources (TB-31).

### Not addressed (candidate follow-up)
With `--agent all` and a `--limit`, the N most-recent sessions can still be dominated by
one agent. That is a uniform, visible truncation rather than a hidden default exclusion.
The CP7 ask to surface per-agent discovered/total is moot for the *default-exclusion*
cause — every agent is now discovered at 100% of its archive — but would still disclose
this milder recency skew.
