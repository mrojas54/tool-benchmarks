# TB-10: Non-UTF-8 session export crashes the whole run under --index-source auto

Found while verifying TB-9 against the live corpus.

REPRO: `python -m toolbench.passive --index-source auto --limit 60` dies with an unhandled
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa0 in position 35: invalid start byte.
`--limit 3` succeeds. Data-dependent, not daemon-dependent -- it fires on whichever session
export contains a non-UTF-8 byte.

ROOT CAUSE: sources.py:29 `_run_agentsview` calls subprocess.run(..., text=True), which decodes
the child's stdout as strict UTF-8. A single session whose export carries a stray byte raises
UnicodeDecodeError inside subprocess.communicate().

WHY IT ESCAPES: passive.py main() wraps _parse_ref in `except (OSError, RuntimeError)` precisely
so a bad session degrades to skipped_roots instead of killing the run. UnicodeDecodeError
subclasses ValueError, not OSError/RuntimeError, so it slips past that guard. One malformed
session therefore aborts the entire corpus scan and no report is produced.

The raw path has the same latent shape: open(ref.path, encoding='utf-8') at sources.py:98
would raise UnicodeDecodeError on read and escape the same guard.

CONTRAST WITH SMOKE ROW 2: that row passed because it tested the *binary hidden from PATH* case,
which fails early inside _discover_refs where FileNotFoundError IS caught. A late failure during
per-session export was never exercised. Fixture-only tests miss it for the same reason they
missed TB-8.

PROPOSED FIX (to confirm at implementation time):
- Decode leniently at the subprocess boundary (errors='replace') and/or on the raw open(), so a
  stray byte becomes a replacement char rather than an exception -- consistent with how the
  reducer already tolerates malformed lines via malformed_total.
- Widen the _parse_ref guard in main() to catch ValueError, so any decode failure demotes the
  session to skipped_roots and the run still emits a report.
- Consider surfacing the count in report provenance (S15), alongside 'Malformed lines'.

IN SCOPE: toolbench/sources.py (_run_agentsview, open_session_jsonl), toolbench/passive.py (main
guard), tests for both. Should include an integration-shaped test that feeds actual non-UTF-8
bytes, not just hand-built ToolCall fixtures.

OUT OF SCOPE: ruff format drift in 6 pre-existing files.
