# TB-37: Freeze replay has no denominator: the manifest persists no census, so every Agent Breakdown row on --freeze replay is unqualified

WHERE: toolbench/passive.py:327-342 (replay branch), toolbench/freeze.py:76-84 (write_manifest), MANIFEST_VERSION at freeze.py:22.

WHAT: a freeze pins the REF LIST, not the archive it was drawn from. write_manifest persists version, fingerprint, count and refs -- and no census. So on replay there is no denominator to disclose: passive.py constructs an empty AgentCensus (totals={}, archive_total=0) with unavailable_reason set to "frozen corpus replay (<path>): no denominator was recorded at freeze time".

CURRENT STATE IS CORRECT, NOT BROKEN. TB-33 chose to STATE the absence rather than let it read as zero -- an unstated unknown is exactly the silence TB-33 exists to break. _sampling_notes (report.py:84-88) honours unavailable_reason and tells the reader each row may rest on a different fraction of its agent archive and this run cannot say. That is the honest floor. This ticket is about raising it.

WHY TB-33 DID NOT DO IT: persisting a census into the manifest is a MANIFEST FORMAT CHANGE, which that ticket did not own. It needs a MANIFEST_VERSION bump plus a read path that tolerates v1 manifests with no census block (the same backward-compat shape freeze.py:46-54 already uses for the TB-29 stale-is_subagent self-heal).

FIX SKETCH:
1. Bump MANIFEST_VERSION to toolbench-freeze-2.
2. write_manifest persists the AgentCensus captured at freeze time (totals, archive_total, residual).
3. read_manifest returns census=None for v1 manifests; passive.py keeps todays unavailable_reason for that case, now worded to name the manifest version rather than freezing in general.
4. Replay with a v2 manifest renders real fractions -- the freeze-time archive size, which is the correct denominator for a pinned corpus.

CAVEAT TO SPEC: a v2 denominator is a HISTORICAL one (archive size as of freeze time), and the archive has since moved. The report must say so, or it trades one silent lie for another.
