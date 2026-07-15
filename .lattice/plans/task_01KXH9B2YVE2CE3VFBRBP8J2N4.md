# TB-40: Docs bookkeeping gap: S41 criterion, T18/T19 build rows, and TB-35 attribution never landed on main

Draft PR #59 (cursor/engineering-documentation-updates-af18) was closed as superseded by the Run 3 closeout: it was 15 commits stale, and the substance it documented (--agentsview-timeout / TB-39, AgentCensus + sampling disclosure, the sampled column, the export_timeout skip reason) had already landed on main independently.

Three pieces of bookkeeping it carried, however, are genuinely ABSENT from main. Verified by grep across all *.md on main at 95c13f7 -- zero hits for each:

  1. EVALUATION.md -- no S41 criterion row. The sampling-disclosure behaviour is implemented and tested, but has no row in the criteria table, so the one table meant to enumerate every SPEC criterion silently under-reports. Every other S-number has a row.

  2. BUILDPLAN.md -- no T18 / T19 rows. T17 is the last row on main, so the two build tickets that produced the AgentsView hang bound (TB-32/TB-39) and the per-agent sampling disclosure (TB-33/TB-35) are missing from the build ledger.

  3. TB-35 is referenced NOWHERE in any doc on main, despite being closed. The uneven-sampling line apportions truncation vs attrition from observed signals -- that is TB-35's contribution -- but the text credits only TB-33.

DO NOT rebase or cherry-pick PR #59's branch. Its EVALUATION.md hunks rewrite the S34 / S35 / S37 rows from a merge-base at which those rows were much shorter; main has since expanded them with the TB-34 and TB-37 census text. Resolving that merge risks regressing those rows. Author the three additions fresh against current main instead.

Scope is docs-only: no code change, no test change. Confirm the gate still passes rather than assuming it.
