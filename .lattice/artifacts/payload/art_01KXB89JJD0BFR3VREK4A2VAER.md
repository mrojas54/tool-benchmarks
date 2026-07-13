Closed via PR #46, merged as 95dadce. Gate green on merged main: ruff clean,
mypy --strict clean, 381 passed. Feature verified running from a clean checkout.

PREMISE CORRECTION (the substance of this ticket): both halves of the original
description were false. agents.md cannot be the manifest -- it discards its Branch
column on run completion, exactly when a run becomes measurable. And "the run's
session set" is not well defined -- 29/158 sessions straddle >1 gitBranch, and one
delegator is logged as having "Ran in ROOT checkout". So attribution is PER-ENTRY,
by that entry's gitBranch, against a JSON manifest emitted at dispatch. SPEC S40,
BUILDPLAN T17 and this ticket's description were all corrected.

WHY IT MATTERS, MEASURED: live smoke on run tb-21-23 reports read=67,727,351 with
unattributed=25,079,909 -- 25.1M cache-read tokens spent in the run's own sessions
but on branches outside the run. The naive session-grain fold this ticket originally
specified would have charged all of it to the run: ~92.8M instead of 67.7M, a 37%
over-count on real data. Now unmergeable by accident -- the counter-trap test is
mutation-verified (naive fold => assert 10400 == 400).

BUILD: 7 tasks, subagent-driven, 3 fix waves. Review caught three defects in the
plan itself (silent `run` coercion, uncaught UnicodeDecodeError on non-UTF-8, and a
git rm that would have deleted fixtures shared with test_parsers.py -- the last
caught and refused by an implementer). Final reviewer independently re-implemented
the fold, reproduced all four live figures exactly, and verified the additivity
invariant across 600 real transcripts (582 measured, 0 breaks).

cache_tokens.py retired into the analyzer, as its own docstring scoped it.

FOLLOW-UPS: TB-28 (detached-HEAD blind spot), TB-29 (--exclude-subagents no-op).