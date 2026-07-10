# TB-23: skipped_roots stringifies typed exceptions: dead index entries and parser gaps land in one bucket

`skipped_roots` is a `list[str]` of formatted messages (passive.py:306, 478). Every skip -- whatever its cause -- is flattened to prose and joined into one line. Two categorically different diagnoses are therefore indistinguishable without regex archaeology on the rendered report:

  1501  agentsview session export failed: source file not found
         -> a DEAD INDEX ENTRY. AgentsView lists a session whose transcript no
            longer exists on disk. Nothing to fix in this repo. Expected, ongoing,
            and driven by external retention (see TB-22).

   136  no registered parser claimed any of the first N decodable lines
         -> a PARSER GAP (`UnknownSchema`, S28). codex/cursor/warp/antigravity.
            This is a TODO, tracked by TB-12 (CodexParser).

     2  non-transcript payload (binary content) from session export
         -> the NUL sniff correctly rejecting an off-contract export (TB-10).

Only the second class is actionable engineering work. Today they are one undifferentiated blob, so the 136 that matter are buried under 1501 that never will. The README's troubleshooting table already treats these as separate symptoms with separate remedies; the data model does not.

Note the failure mode this enables: `NonTranscriptExport` is a typed exception and `UnknownSchema` is a typed exception, but both are caught by the same `except (OSError, RuntimeError, UnicodeDecodeError)` guard in `main` (passive.py:473) and immediately stringified. The type information exists at the raise site and is destroyed one frame later.

CANDIDATE SHAPES (not chosen):
  a. `SkipRecord(session_id, agent, reason: SkipReason)` where `SkipReason` is an
     enum -- MISSING_SOURCE / UNKNOWN_SCHEMA / NON_TRANSCRIPT / DECODE_ERROR.
     Mirrors the `UsageProvenance` enum TB-18 introduced for the same class of
     problem: type the absence rather than stringify it.
  b. Raise a dedicated `MissingSourceExport(NonTranscriptExport)` from
     `AgentsViewLoader.lines` when stderr matches `source file not found`, so the
     distinction is made at the raise site where the evidence lives.

Blocks TB-21: the per-reason histogram it asks for should key on a typed reason, not on a substring match against an error message that upstream is free to reword.

ACCEPTANCE. Skips carry a machine-readable reason from raise site to report. `passive` can answer "how many sessions have no parser?" without parsing its own prose. What must not survive: a dead index entry and a missing parser counted in the same bucket.
