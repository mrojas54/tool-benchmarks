# TB-32: passive hangs forever if the AgentsView daemon hangs: _run_agentsview has no subprocess timeout, so S10 fallback never fires

S10 defines the auto-fallback as triggering when the AgentsView CLI 'is missing or
exits nonzero'. A THIRD failure mode is unhandled: the CLI hanging.

toolbench/sources.py:113

    def _run_agentsview(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)

No timeout= argument. _probe_agentsview() catches FileNotFoundError (binary absent)
and inspects returncode (nonzero exit), but a daemon that accepts the connection and
never responds produces neither signal -- subprocess.run blocks indefinitely.
Consequence: 'passive --index-source auto' hangs forever instead of falling back to
raw. There is no timeout, no failure signal, and no fallback: the exact opposite of
S10's intent, which is that an unhealthy AgentsView must never block a scan.

WHY THE TEST SUITE CANNOT SEE THIS: every test injects a fake Runner that returns
immediately. The hang lives in the real _run_agentsview default, which no hermetic
test exercises. This was found by operator checkpoint CP2, and it is precisely the
residual gap a PATH-shim could not close either -- a shim can fake 'exits nonzero',
but proving the real daemon exits rather than hangs requires the live daemon.

REPRO (destructive, needs operator): SIGSTOP the AgentsView daemon (rather than
stopping it cleanly) and run 'passive --index-source auto'. Expect: falls back to
raw and names the reason. Actual (predicted): hangs indefinitely.

FIX: pass a bounded timeout= to subprocess.run; catch subprocess.TimeoutExpired in
_probe_agentsview and return it as a fallback reason ('agentsview timed out after Ns'),
joining the missing-binary and nonzero-exit branches. Add a Runner-injected test whose
fake raises TimeoutExpired and assert the run falls back to raw with the reason named.

Found by: operator smoke checkpoint CP2 (EVALUATION.md), 2026-07-13.
