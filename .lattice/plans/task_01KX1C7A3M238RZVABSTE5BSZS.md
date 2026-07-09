# TB-4: sources.py multi-agent discovery

SessionRef, iter_session_files, iter_agentsview_sessions (cursor pagination), open_session_jsonl, index-source policy (auto/agentsview/raw). Fake-runner tests, no live daemon.

SPEC: S7, S8, S9, S10, S24
BUILDPLAN anchor: T3
Depends on: T1

## Plan (delegator, filled in)

Branch `tb-4-sources` is currently at `origin/tb-2-scaffold` (verified via
`git log`: HEAD = `dd96d91 Rename records.py -> transcript.py`). PR base is
`main`, dependent on PR #1 (tb-2-scaffold).

### New file: `toolbench/sources.py`

- `@dataclass SessionRef`: `agent: str, source: str, project: str,
  session_id: str, path: str | None`. `path` is `None` for AgentsView-only
  refs (no filesystem path known until export); populated for raw refs.
- `iter_session_files(root="~/.claude/projects", project=None, since=None)`
  (S7): `Path(root).expanduser()`; raise `FileNotFoundError` if missing.
  `rglob("*.jsonl")`; filter by `project` as a substring of the immediate
  parent dir name; filter by `since` (parsed via
  `datetime.fromisoformat`) against `Path.stat().st_mtime`. Yields `Path`.
  Claude-Code-only per BUILDPLAN G3 (no Codex/Hermes raw adapters).
- `iter_agentsview_sessions(agent="all", project=None, since=None,
  limit=500, runner=_run_agentsview)` (S8): builds
  `["agentsview", "session", "list", "--json", "--limit", str(limit)]`,
  plus `--agent <agent>` when not `"all"`, `--project <project>` when set,
  `--date-from <since>` when set (matches real CLI flags confirmed via
  `agentsview session list --help`). Loops: run → `json.loads(stdout)` →
  yield a `SessionRef` per `sessions[]` entry (`source="agentsview"`,
  `path=None`) → if `next_cursor` is a non-empty string, append
  `--cursor <next_cursor>` and repeat; stop when cursor is empty/absent.
  `runner` is the injected subprocess-runner seam (S24): a callable
  `(argv: list[str]) -> subprocess.CompletedProcess`, defaulting to a thin
  wrapper over `subprocess.run(argv, capture_output=True, text=True,
  check=False)`.
- `open_session_jsonl(ref: SessionRef, runner=_run_agentsview) ->
  Iterator[str]` (S9): if `ref.path` is set, open and yield lines from the
  filesystem file; else shell `agentsview session export <ref.session_id>`
  via `runner` and yield stdout lines (real CLI confirmed to stream raw
  JSONL directly — no `--json` wrapping needed for export).
- `IndexSource = Literal["auto", "agentsview", "raw"]`;
  `iter_sessions(index_source="auto", ...) -> Iterator[SessionRef]` (S10):
  - `"raw"`: delegates to `iter_session_files`, wraps each `Path` in a
    `SessionRef(source="raw", ...)`.
  - `"agentsview"`: delegates straight to `iter_agentsview_sessions`; CLI
    missing/nonzero exit is a hard error (`FileNotFoundError` /
    `RuntimeError`) — strict, no fallback.
  - `"auto"`: try `iter_agentsview_sessions` first (probing via one
    `session list --limit 1` call through the runner); on
    `FileNotFoundError` (binary missing) or nonzero exit, fall back to
    `iter_session_files` and **record the reason** — return value carries
    `(Iterator[SessionRef], fallback_reason: str | None)` so callers
    (passive.py, T4) can surface it in report provenance (S15 depends on
    this).

### Fake-runner test seam (S24)

`test_sources.py` builds a `FakeAgentsViewRunner` closure/class that takes
a scripted mapping of `argv tuple -> CompletedProcess`-like result (or a
list of paged responses keyed by presence/absence of `--cursor`), asserts
the exact argv constructed (agent/project/date-from/limit/cursor flags),
and returns canned JSON matching the real shape captured via
`agentsview session list --json --limit 2` (fields: `sessions`,
`next_cursor`, `total`). Tests cover: single page (no cursor), two-page
pagination (cursor forwarded correctly, loop terminates on empty
next_cursor), missing binary (`FileNotFoundError` from the runner) driving
auto-fallback with reason recorded, nonzero exit under `"agentsview"`
strict mode raising, and `open_session_jsonl` reading a raw `path` vs.
shelling to `export` for an AgentsView-sourced ref. No `~/.claude` access,
no real daemon — fully hermetic.

### Self-review notes (before implementation)

- Cursor logic: must stop on `next_cursor in (None, "")`, not just `None`
  — confirmed real CLI returns `""`-worthy semantics are unclear from one
  sample (only saw a populated cursor); treat both `None` and falsy string
  as "no more pages" to be safe, tested explicitly.
- Fallback-reason: policy says "RECORDING THE REASON" — model this as a
  return tuple rather than a global/side-channel, since S11 (passive.py)
  needs incremental streaming without shared mutable state.
- Fake-runner seam: inject at the `iter_agentsview_sessions`/
  `open_session_jsonl` call boundary (a `runner` parameter), not by
  monkeypatching `subprocess` globally — keeps tests hermetic and
  parallel-safe.

### Shared-file risk

`toolbench/__init__.py` is currently empty (0 bytes) — TB-3 may add
re-exports there too. This ticket makes no edits to `__init__.py` unless
strictly needed for test imports; if an edit becomes necessary, it will be
additive-only (a re-export line), not a rewrite.

### Out of scope

`transcript.py` (parse_session/id-join — TB-3), `passive.py`, `probe.py`
— untouched.
