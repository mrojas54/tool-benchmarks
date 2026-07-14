# TB-38: auto fallback covers only the AgentsView probe: a daemon that dies mid-listing is fatal, so --index-source auto silently forfeits its fallback promise

S10 promises --index-source auto degrades to raw when AgentsView is unhealthy. It only keeps that promise for failures visible AT THE PROBE (_probe_agentsview, a single --limit 1 call). A daemon that answers the probe and then fails during the pagination that follows -- discover_agentsview -> _probe_pass -> _agentsview_pages -- propagates out of iter_sessions uncaught, lands in passive.main's (FileNotFoundError, RuntimeError) guard, and exits 1 as 'fatal source error'. No fallback to raw.

This affects ALL THREE failure modes symmetrically, and is NOT specific to the timeout added by TB-32:

  probe OK, then nonzero exit  -> RuntimeError        -> fatal, exit 1   (pre-existing on main)
  probe OK, then hang          -> AgentsViewTimeout   -> fatal, exit 1   (TB-32, deliberately symmetric)

Verified against main: the nonzero-exit path already behaved this way before TB-32 touched anything. TB-32 pinned the timeout to the same behaviour on purpose (tests/test_sources.py::test_mid_discovery_timeout_is_fatal_like_any_other_source_error) rather than special-casing hangs to fall back while an equally-broken daemon that exits 1 stays fatal.

THE DESIGN QUESTION THIS TICKET OWNS: should auto fall back to raw after a PARTIAL listing? It is not obviously yes. Discovery is lazy (iter_sessions returns an iterator), so a mid-pagination failure can occur after refs have already been yielded downstream. Falling back then means either discarding partial agentsview refs and rescanning raw (double work, and the report's corpus changes identity mid-run), or splicing raw refs onto a truncated agentsview list (a corpus that is neither, and a fingerprint that means nothing -- cf. TB-22). Note _agent_census ALREADY degrades gracefully here (AgentCensus(unavailable_reason=...)) while _probe_pass does not, so the module is already of two minds about it.

Whatever the answer, it must be one answer for all three failure modes, and the report must not be able to claim a corpus it did not scan.

Found by: code review of the TB-32 fix, 2026-07-14.
