# TB-21: Summary reports 'Sessions scanned' as corpus size; 48% of discovered sessions are skipped and unreconciled

`Sessions scanned: 1746` reads as the corpus size. It is not. Measured on the live archive (2026-07-10, `--agent all --all`, `--index-source auto`):

  discovered (agentsview paging)  3386
  scanned                         1746   (52%)
  skipped                         1639   (48%)
  reconciles                      3385 + 1 live session whose ended_at advanced mid-scan

Nothing is lost — every skipped session lands in `skipped_roots`, which is the TB-18 "fail loudly, never a healthy zero" discipline working as designed. The defect is the RENDER, not the counting.

`render_report` joins all 1639 entries into a single `; `-separated line (passive.py:443). That is a string, not data. During the investigation that produced this ticket, the one-line format directly caused a mis-tally: a `^[a-z]+:` scan of it silently dropped both `claude-ai:` (hyphen) and bare-UUID ids — and bare-UUID is exactly how claude sessions are keyed — yielding "170 skipped" when the true figure was 1639. A report whose own author cannot count it is not legible.

Skip reasons, correctly tallied:

  942  agentsview session export failed: source file not found for session claude-ai:...
  559  agentsview session export failed: source file not found: /Users/.../<uuid>.jsonl
  136  no registered parser claimed any of the first N decodable lines
    2  non-transcript payload (binary content) from session export

The `claude-ai` row is the headline: 942 sessions (28% of the indexed corpus) are unexportable on EVERY run, and no line in the report says so. A reader concludes the benchmark saw the corpus. It saw half.

ACCEPTANCE. The Summary section reconciles discovery: `Sessions discovered: N / scanned: M / skipped: K`, plus a per-reason histogram with counts. Individual session ids move behind a `--verbose` flag or a sidecar file. What must not survive: a report that presents `scanned` as though it were `discovered`, or a 1639-entry skip list rendered as one unreadable line.

Depends on TB-23 landing first if the histogram is to be keyed on typed reasons rather than on string prefixes.
