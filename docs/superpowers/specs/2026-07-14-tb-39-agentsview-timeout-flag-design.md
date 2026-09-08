# TB-39 — `--agentsview-timeout`: make the TB-32 bound an operator's choice

**Status:** shipped (TB-39). Body below is the design-time snapshot — flat-layout
paths and contemporaneous wiring notes are historical. Live contract:
[`README.md`](../../../README.md) (`--agentsview-timeout`), SPEC/EVALUATION S10/S12,
and `src/toolbench/passive.py` / `src/toolbench/sources.py` /
`src/toolbench/report.py`. Default remains `AGENTSVIEW_TIMEOUT_S` (60s); `0` is
unbounded; Summary discloses the ceiling only on `export_timeout` truncation or
an unbounded run.

## Problem

TB-32 bounded every `agentsview` subprocess call at `AGENTSVIEW_TIMEOUT_S = 60.0`. The
value is a module constant with no override.

60s is a compromise, and a compromise is wrong for somebody at both ends:

- a large or slow archive can exceed 60s on a **legitimate** call, so a healthy daemon is
  killed and its sessions land under the `export_timeout` skip reason — a truncated corpus
  caused by our own default;
- an operator who knows their daemon is merely slow cannot say *"wait longer"*, and one
  debugging a hang cannot say *"give up sooner."*

## Design

### The flag

`--agentsview-timeout SECONDS` on `toolbench.passive`, `type=float`, default `60.0`.

| value | meaning |
|---|---|
| `> 0` | bound at that many seconds. **Default 60.0 — behaviour identical to TB-32 today.** |
| `0` | unbounded: `timeout=None` to `subprocess.run`, restoring pre-TB-32 blocking. Deliberate escape hatch. |
| `< 0` | rejected by argparse. Not a policy choice; a negative ceiling is nonsense. |

`0` is allowed on purpose. An operator who knows their daemon is just slow should be able
to say so, and refusing to express "wait forever" would be paternalistic. The cost is that
TB-32's hang becomes re-armable — which is precisely why it is disclosed (below) rather
than merely permitted.

### The wiring — one seam, no new plumbing

`main()` already accepts an injectable `runner: Runner | None = None`, and **both**
consumers fall back to `_run_agentsview` independently when it is `None`:

- `iter_sessions` → `run = runner if runner is not None else _run_agentsview`
- `AgentsViewLoader.__init__` → `runner: Runner = _run_agentsview` (reached via
  `pick_adapter`)

So `main()` binds the timeout once, at the point where the default runner is chosen:

```python
runner = runner or functools.partial(_run_agentsview, timeout=args.agentsview_timeout)
```

`functools.partial(_run_agentsview, timeout=…)` is still
`Callable[[list[str]], CompletedProcess[str]]`, so it satisfies `Runner` unchanged. Every
injected test fake keeps working, and the flag reaches all four call sites (probe,
paginated listing, census, per-session export) for free.

`_run_agentsview`'s `timeout` widens from `float` to `float | None`; `None` is passed
straight through to `subprocess.run`, whose native semantics for `timeout=None` are
already "block forever." No branch needed in the runner itself.

**Injected-runner precedence.** An explicitly-injected `runner` (tests, callers) wins over
the flag and is never wrapped. The flag configures *the default runner*, not the seam.

### The disclosure — the part that is not merely a knob

The Summary names the timeout when, and **only** when, it changed what the reader is
looking at:

1. **The run produced ≥1 `export_timeout` skip.** A short timeout that truncated the corpus
   is exactly the "report claims a population it did not scan" failure TB-21/TB-33 exist to
   prevent. The reader must not attribute that gap to the archive when it was caused by our
   own ceiling.
2. **The run was unbounded (`0`).** Otherwise a reader cannot distinguish *"AgentsView was
   healthy"* from *"we got lucky"* — the run could have blocked indefinitely, and a clean
   report is not evidence that it didn't.

A clean bounded run says **nothing**. No stderr warning in any case: this is a Summary
fact, not a nag.

Line shapes:

```
- AgentsView timeout: 5.0s — 12 session(s) skipped as `export_timeout`; the corpus is
  truncated by this ceiling, not by the archive.
- AgentsView timeout: unbounded (--agentsview-timeout 0) — a hung daemon would have
  blocked this run indefinitely rather than degrading.
```

## Testing

| test | pins |
|---|---|
| default is 60.0 when the flag is absent | the flag cannot silently change TB-32's behaviour |
| flag value reaches `subprocess.run` | assert the bound value actually arrives, not merely that the flag parses |
| `0` → `timeout=None` | the escape hatch is real |
| negative → `SystemExit` (argparse error) | rejected, not silently coerced |
| an injected runner is **not** wrapped | the flag configures the default, never overrides an explicit seam |
| Summary line present iff `export_timeout` skip occurred | disclosure fires on truncation |
| Summary line present iff unbounded | disclosure fires on the risky config |
| Summary silent on a clean bounded run | no noise in the common case |

## Out of scope

- A `--agentsview-timeout` on `toolbench.probe`. `probe.py` does not use the `Runner`
  seam at all.
- Per-call-site timeouts (a short probe, a long export). `Runner` is single-arg by
  design; widening it would break every fake in the suite, and TB-32 already argues no
  single call is unbounded work.
- Widening `auto`'s fallback to survive a failure *mid-listing* — that is **TB-38**, and
  it belongs to all three failure modes at once, not to the timeout alone.
