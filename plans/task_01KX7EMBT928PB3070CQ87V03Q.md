# TB-26 — session-grain cache-token sums for Claude (read + creation)

Spec: S39 · Buildplan: T16 · Closed by PR #43 (merged `dc52ca9`)

## Plan (revised after audit)

The plan changed on contact with the code. What follows is what was actually
done, and why it is not what the ticket asked for.

### 1. Audit before building

Auditing the ticket against the S39 eval row found the implementation **already
on main** — landed in `15879ac` (the whole-repo CQ refactor, #37) as *CQ 1.2*,
before TB-26 was minted. The S39 spec block and the TB-26/TB-27 tickets came
later (`c7e5523`), formalizing capability the refactor had already shipped. That
is why the ticket sat in `backlog` with working code behind it.

Building it would have duplicated working code.

| S39 eval clause | State at audit |
|---|---|
| Claude sums read/creation from per-message `usage` | `parsers.py:271-272` ✅ |
| `None` unmeasured / `0` measured-zero | `parsers.py:356` guard ✅ |
| hermes path unchanged, no double-count | `hermes.py:166` ✅ |
| Summary caveat prints read + creation, never ranks | `report.py:248` ✅ |
| `cache_tokens` façade over `ClaudeParser` | ✅ |
| TB-25 date-range survival extends to the new field | ❌ **untested** |

### 2. Close the one real gap

The survival invariant held **by construction** — TB-25 fixed `_apply_date_range`
to rebuild via `dataclasses.replace(result, calls=kept)` rather than hand-listing
fields, so `session_cache_creation_tokens` passed through date filtering for free
the moment it was added to `ParseResult`.

Held, but unpinned. Two regression tests added to
`tests/test_passive_cli.py::DateRangeFilterTests`, each **verified to fail under
mutation** rather than merely passing on green main:

1. **Hand-listed reconstruction** — TB-25's original bug reintroduced on a new
   column. Creation silently resets to `None`; the pre-existing read-survival test
   stays green, so nothing else catches it.
2. **Falsy coalesce** (`x or None`) — passes *every* pre-existing test, including
   both `=42` survival assertions, and still corrupts measured-zero into
   unmeasured. Only the counter-trap test fires.

The second earns its keep. Under S32, `0` (measured; cache genuinely unused) and
`None` (unmeasured, SQL NULL) are **different facts**. Collapsing one into the
other does not crash or print a wrong number — it makes a measured session look
unmeasured, quietly undercounting the cache caveat. A benchmark that lies rather
than fails.

## Outcome

Test-only change; `toolbench/` zero diff. Gate green on merged main: `ruff` clean,
`mypy --strict` clean (29 files), **356 passed** (up from 354).

Unblocks TB-27, whose design landed as spec **S40**
(`docs/superpowers/specs/2026-07-12-tb-27-per-run-cache-grouping-design.md`).
