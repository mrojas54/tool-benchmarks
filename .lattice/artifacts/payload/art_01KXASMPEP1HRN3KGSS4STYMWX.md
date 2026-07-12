Closed via PR #43, merged as dc52ca9.

Finding: TB-26's implementation had already landed on main in 15879ac (whole-repo
CQ refactor, #37) as CQ 1.2, BEFORE this ticket was minted. The S39 spec block and
TB-26/TB-27 came later (c7e5523), formalizing capability the refactor had already
shipped. Every S39 eval clause was verified satisfied except one.

The gap: the TB-25 date-range survival invariant was never extended to the new
session_cache_creation_tokens column. It held by construction (_apply_date_range
rebuilds via replace()) but nothing pinned it.

PR #43 closed it with two mutation-verified regression tests:
- hand-listed reconstruction (TB-25's bug on a new column): creation silently
  resets to None while the existing read-survival test stays green.
- falsy coalesce (`x or None`): passed EVERY pre-existing test and still corrupted
  measured-zero into unmeasured. Only the counter-trap test fired. Under S32, `0`
  (measured, cache unused) and `None` (unmeasured) are different facts.

Test-only; toolbench/ zero diff. Gate green on merged main: ruff clean,
mypy --strict clean (29 files), 356 passed (was 354).

Unblocks TB-27, whose design landed as spec S40
(docs/superpowers/specs/2026-07-12-tb-27-per-run-cache-grouping-design.md).