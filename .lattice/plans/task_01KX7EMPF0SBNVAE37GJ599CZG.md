# TB-27: per-run cache-token grouping: --run-manifest <agents.md> flag folds a run's session set into one reducer, emitting read+creation per run

Add per-run grouping to the passive analyzer: a --run-manifest <agents.md> flag that folds a lattice run's session set (actors/branches/worktrees from .lattice/orchestration/agents.md) into one reducer, emitting cache read+creation per run, normalized per ticket. New grouping dimension above session + CLI + corpus filtering. Depends on TB-26 (session-grain cache sums).
