# TB-29 — `--exclude-subagents` is a silent no-op

Spec: S13 · Closed by PR #47 (branch `fix/tb-28-29-run-attribution-gaps`)

## The bug

`sources.py::_project_and_subagent` tested `rel.parts[1] == "subagents"`. The real
on-disk layout is:

```
<project>/<session-uuid>/subagents/agent-*.jsonl
   [0]          [1]          [2]          [3]
```

`parts[1]` is the **session UUID**, so `is_subagent` was **never** `True` on a raw
scan. `filter_subagents` therefore filtered nothing, and the report printed
`Subagents included: no` while including every subagent: **the report stated the
opposite of what it did.**

## Why it survived a 381-test suite

The suite did not *miss* this — it **ratified** it.

Every subagent fixture in `tests/test_sources.py` built a flat
`<project>/subagents/<file>.jsonl` — a **three**-part path that exists nowhere on
disk. Against that invented layout, `parts[1] == "subagents"` is *correct*. Test and
code were written from the same wrong mental model, so the suite stayed green and
the feature was dead.

This is the lesson worth keeping: **a test written from the same misunderstanding as
the code does not protect the code, it protects the bug.** Fixing `sources.py` alone
would have meant making it pass a layout reality never produces, so the **fixtures**
had to move first. The flag is now asserted on the **filtered refs**, never on the
flag's own echo in the report — an assertion the no-op could not have faked.

## What shipped

`is_subagent = "subagents" in rel.parts[1:-1]` — matching any segment **between** the
project and the filename, so the check does not re-break if the nesting depth changes
again. The slice excludes `parts[0]` and `parts[-1]`, so a project or session
directory literally named `subagents` cannot false-positive.

## Measurement (live corpus)

| | pre-fix | post-fix |
|---|---|---|
| default | 201 sessions | 201 |
| `--exclude-subagents` | **201** (identical — a no-op) | **174** (27 subagents dropped) |

Confirmed by running the *same command* on pre-fix `main` — evidence, not inference.

An incidental live proof: mid-session the subagent delta rose 27 → 29, exactly
matching the two review subagents dispatched while the corpus was being written.

## What the review round added

**TB-29 survived on freeze replay.** `freeze.py::_is_subagent_from_manifest` let an
explicit flag beat path re-derivation — the path fallback fired only when the key was
*absent*. But manifests frozen **before** this fix persist an explicit
`"is_subagent": false` for real subagent sessions, written by the very code that could
never set it `True`. So `--freeze old.manifest --exclude-subagents` would have kept
silently including subagents **forever**: the exact TB-29 symptom, relocated into
replay, immortalised in every frozen corpus.

The path is ground truth; a stale flag is not. The two are now OR-ed, which also
self-heals legacy manifests that omit the key. A genuine non-subagent (no
`/subagents/` segment) still reads `False` — pinned by
`test_explicit_false_is_honoured_for_a_genuine_non_subagent`.

## Docs

`SPEC.md`, `README.md` and `AGENTS.md` all still documented the flat
`<project>/subagents/*.jsonl` layout this ticket disproved — the contract
contradicted its own fix. All corrected to the real nested layout.

## Note

Pre-existing (CQ 3.2); predates TB-27 and does not affect S40's run totals —
subagent cost **belongs** in the run, which is what already happened. The defect was
never a wrong run number; it was a flag that lied about what it did.
