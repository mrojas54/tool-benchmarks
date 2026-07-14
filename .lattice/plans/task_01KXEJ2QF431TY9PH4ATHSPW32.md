# TB-34: calls_joined == 0 early-return discards the census: 'no sessions matched' throws away a denominator it already computed

Pre-existing early-return, but TB-33 now gives it a disclosure to throw on the floor.

WHERE: toolbench/passive.py:418-428.

WHAT: when reducer.calls_joined == 0, main prints "toolbench.passive: no sessions matched the given selection." (plus a skip tally when skips is non-empty) and returns 0 BEFORE render_report is ever called.

By that point the run has already built a full AgentCensus: census.totals (per-agent archive sizes), census.archive_total and census.residual are all in hand -- on a representative run, 8 agents and ~11,976 archive sessions. All of it is discarded unprinted.

WHY IT MATTERS: a user who scopes a window too narrowly is told "nothing matched" and cannot distinguish (a) an empty archive from (b) an archive holding 11,976 sessions that this window simply failed to reach. That is exactly the silence TB-33 exists to break -- and the zero-result case is where the denominator matters MOST, yet it is the one case that never prints one.

FIX SKETCH: before the early return, emit the census the run already computed -- archive_total, per-agent totals, and the unreached-agents line that _sampling_notes (toolbench/report.py:91-101) already knows how to render. Either render a census-only report, or thread the totals into the no-match message.

NOTE: several tests pin the current message (tests/test_passive_cli.py:274,283,304,352; tests/test_hermes.py:425; tests/test_sources.py:607) -- they assert substring "no sessions matched", so an ADDITIVE census suffix should keep them green.
