# TB-32 — Bound every AgentsView subprocess call, and route a hang into the paths that already exist

## Problem
`_run_agentsview` (`toolbench/sources.py:181`) calls `subprocess.run(...)` with no
`timeout=`. S10 defines the auto-fallback as firing when the AgentsView CLI "is
missing or exits nonzero"; a daemon that accepts the connection and then never
responds produces *neither* signal, so `subprocess.run` blocks forever. Consequence:
`passive --index-source auto` hangs instead of falling back to raw — the exact
inversion of S10's intent, which is that an unhealthy AgentsView must never block a
scan.

No hermetic test can see this: every test injects a fake `Runner` that returns
immediately, so the hang lives in the real `_run_agentsview` default that no test
exercises. Found by operator smoke checkpoint CP2 (EVALUATION.md), 2026-07-13.

## Correction to the ticket's stated FIX
The ticket proposes: add `timeout=`, catch `subprocess.TimeoutExpired` in
`_probe_agentsview`. That is necessary but **not sufficient, and shipping only it
introduces a crash.**

`_run_agentsview` is the runner for *four* call sites, not one:

| line | call | when it runs |
|------|------|--------------|
| 250 | paginated `session list` | ref collection |
| 312 | census `session list --limit 1` | ref collection |
| 541 | `session export <id>` | **once per session, mid-scan** |
| 558 | probe `session list --limit 1` | `auto` health check only |

`subprocess.TimeoutExpired` subclasses `SubprocessError` → `Exception`. It is **not**
an `OSError` and **not** a `RuntimeError`. The two guards that absorb source failures
are `passive.py:406` `(FileNotFoundError, RuntimeError)` and `passive.py:447`
`(OSError, RuntimeError, UnicodeDecodeError)`. A bare `TimeoutExpired` escapes both.

So a daemon that is healthy at probe time and hangs later — on export #4000 of 8591 —
would raise straight through the per-session guard and abort the whole scan. That
trades "hangs forever" for "crashes late and loses every session already scanned."
Both are S10 violations.

## Chosen shape: bound the call, re-type the exception, reuse the existing guards
Convert the timeout into an exception the codebase's existing error taxonomy already
routes correctly, rather than plumbing a new signal through four call sites.

### S10a — bound the subprocess
- `AGENTSVIEW_TIMEOUT_S: float = 60.0`, module constant in `sources.py`.
- `_run_agentsview` passes `timeout=AGENTSVIEW_TIMEOUT_S` to `subprocess.run`.
- One constant for all four call sites, not per-site values: the `Runner` seam is
  `Callable[[list[str]], CompletedProcess[str]]` (single-arg), so a per-site timeout
  would have to widen the signature and break every injected fake in the suite. 60s is
  chosen to be generous for the largest legitimate single call (one session export, or
  one list page — never the whole archive at once) while still bounded.

### S10b — re-type it
- `class AgentsViewTimeout(RuntimeError)` in `sources.py`, alongside the existing
  `NonTranscriptExport` / `MissingSourceExport` typed-failure siblings.
- `_run_agentsview` catches `subprocess.TimeoutExpired` and raises `AgentsViewTimeout`
  naming the timeout and the argv.
- Subclassing `RuntimeError` is the load-bearing choice: it makes the new failure flow
  into both existing guards with no changes at the call sites.

### S10c — route it
- **Probe (`auto`)**: `_probe_agentsview` catches `AgentsViewTimeout` and returns it as
  a fallback reason (`agentsview timed out after 60.0s: ...`), joining the
  missing-binary and nonzero-exit branches. → falls back to raw, reason named in the
  report. This is S10's intent.
- **Mid-scan export**: `RuntimeError` → already caught at `passive.py:447` → becomes a
  `SkipRecord`. Gets its own `SkipReason.EXPORT_TIMEOUT` rather than being lumped into
  the generic `EXPORT_FAILED` bucket, per TB-23's rule ("type the absence rather than
  stringify it"). The scan survives; the skipped sessions are named and counted.
- **Ref collection under explicit `--index-source agentsview`**: `RuntimeError` →
  already caught at `passive.py:406` → "fatal source error", exit 1, reason named. No
  fallback, which is correct: the operator explicitly demanded agentsview.

## Tests (TDD — all must fail first)
1. `test_run_agentsview_passes_a_bounded_timeout` — monkeypatch `subprocess.run`,
   assert `_run_agentsview` passes a non-None `timeout`. **This is the one that guards
   the real default**, the line no existing test touches.
2. `test_run_agentsview_retypes_timeout_as_agentsview_timeout` — patched
   `subprocess.run` raises `TimeoutExpired`; assert `AgentsViewTimeout` (an
   `isinstance` of `RuntimeError`) comes out.
3. `test_auto_falls_back_to_raw_when_agentsview_times_out` — `FakeRunner` raising
   `TimeoutExpired`; assert `resolve_refs(index_source="auto")` returns raw refs and a
   `fallback_reason` naming the timeout.
4. `test_export_timeout_is_skipped_not_fatal` — export runner raises
   `AgentsViewTimeout` mid-scan; assert the run completes and the session lands as a
   `SkipRecord` with `SkipReason.EXPORT_TIMEOUT`.
5. `test_explicit_agentsview_timeout_is_fatal_and_named` — `--index-source agentsview`
   + timeout → exit 1, reason on stderr, no silent raw fallback.

## Acceptance
An AgentsView daemon that hangs never blocks a scan and never silently loses one:
under `auto` it falls back to raw with the reason named; mid-scan it degrades to
counted, typed skips; under explicit `agentsview` it fails fast and says why.

## Out of scope
A `--agentsview-timeout` CLI flag (the constant is sufficient for the acceptance
criteria; a flag can be added if 60s ever proves wrong in practice).
