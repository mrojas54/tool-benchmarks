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
