# TB-22 — Corpus reproducibility: fingerprint (default) + freeze (opt-in)

## Problem
Two identical `passive --agent all --all` runs 18 min apart disagreed
(sessions 1752→1746, claude scanned 1289→1283). Root cause: claude-mem observer
transcripts age out of a ~30-day sliding window *mid-scan*, so the corpus tail
deletes itself between runs. AgentsView still indexes the vanished sessions; the
export then fails on `source file not found` (already typed `MISSING_SOURCE`,
TB-23). Consequence: two reports are not diffable — a delta cannot be attributed
to a code change because the corpus underneath moved.

## Acceptance (from ticket)
Either the harness produces two byte-identical reports over an unchanged corpus,
**or** the report states plainly that its input set is non-reproducible and names
the mechanism. What must not survive: a reader diffing two reports and
attributing the delta to code.

## Chosen shape: (b) fingerprint + (c) freeze
Detection by default; reproducibility on demand.

### Design decision — fingerprint the SCANNED set, not discovered
The report's numbers come only from sessions that scanned successfully. A
discovered-set fingerprint could match while transcripts slid scanned→skipped
(the incident), falsely reassuring the reader. Fingerprinting the scanned ids
means a matching hash guarantees the same sessions produced the numbers. The
scanned/skipped split and reason histogram (TB-21/S35) already surface *why* a
fingerprint moved.

## S36 — Corpus fingerprint (always-on)
- `CorpusFingerprint(hash: str, count: int)` + `corpus_fingerprint(ids) ->
  CorpusFingerprint`: order-independent (sort then hash), count = number of ids.
- `main()` collects the session_id of every successfully-scanned ref; passes the
  fingerprint into `render_report`.
- Summary gains one line: `- Corpus fingerprint: <hash> (<N> sessions scanned)`.
- Two runs over an unchanged scanned set → identical line. Any change → different
  hash, and the existing reconciliation/histogram explains it.

## S37 — `--freeze <manifest>` (opt-in reproducibility)
- New module `toolbench/freeze.py`: `CorpusManifest(version, fingerprint, count,
  refs: list[SessionRef])` with JSON (de)serialization of `SessionRef`.
- CLI flag `--freeze <path>`.
- `main()`:
  - manifest absent → normal discovery, write manifest (refs + discovered
    fingerprint), then scan as usual. (write-once)
  - manifest present → **replay**: use `manifest.refs` as the discovered set
    (bypass live discovery), scan them. Refs that now fail to load
    (`MISSING_SOURCE`) are **vanished since freeze** and named in the Summary
    (count) + `--verbose` (ids).
- When nothing has vanished, the scanned set is unchanged ⇒ byte-identical report
  (fingerprint line included) — the "byte-identical over unchanged corpus" branch.

## Build order (TDD, RED→GREEN→DOCS commits)
1. **S36 fingerprint** — helper + reducer/main wiring + Summary render.
   Tests: determinism, order-independence, count, render line, same/changed set.
2. **S37 freeze** — `freeze.py` manifest I/O; `--freeze` flag; write-once +
   replay + vanished reporting. Tests: manifest round-trip, write-on-first-run,
   replay-uses-frozen-refs, vanished-refs-reported, byte-identical-when-unchanged.
3. **DOCS** — SPEC S36/S37, EVALUATION rows, README Summary example, BUILDPLAN.

## Out of scope
- Content-hashing sessions (identity + count only, per candidate b). The live
  session's own +10 calls remain a separately-known, documented caveat.
- Changing retention behavior of claude-mem (external; not this repo).

## Gates
ruff clean, mypy --strict clean, full suite green (301 baseline + new), lattice
doctor clean. PR to main; review→done ceremony.
