## Own-reviewer review — TB-2

**Verdict: PASS** (one Major finding raised during review, fixed and re-committed before this review was filed)

### Scope checked
Diff: origin/main..HEAD (2 commits, cf82ac6 + dd96d91). 10 files changed:
pyproject.toml, uv.lock, toolbench/{__init__,transcript,passive,probe}.py,
tests/{__init__,test_transcript}.py.

### Findings

- **Major (fixed) — toolbench/records.py:1 (pre-fixup).** SPEC.md's own
  section header ("Parser & records — `toolbench/transcript.py`", covering
  S1-S6) and BUILDPLAN's architecture diagram both name `transcript.py` as
  the home for `ToolCall`/`result_len`. The initial commit (cf82ac6) instead
  created a separate `toolbench/records.py`, which would have left T2
  (parse_session/ParseResult, depends on T1) with an ambiguous import
  surface instead of extending one file as SPEC implies.
  Recommendation: rename to `toolbench/transcript.py`. **Applied in dd96d91.**

- **Minor — toolbench/transcript.py `result_len` dict branch.** For a bare
  dict without a `content` key, length is `len(json.dumps(payload))`. SPEC
  S2 (block-local `content` wins over top-level `toolUseResult`) is T2's
  job, not this ticket's — `result_len` here only needs to accept an
  already-resolved payload, which it does for all four shapes named in S3.
  No action needed for T1; flagging so T2's delegator knows `result_len`
  is generic and payload-shape resolution happens before it's called.

- **NIT — toolbench/passive.py, toolbench/probe.py.** Stub `main()` bodies
  are print-only; acceptable per ticket scope (S21 stub requirement), no
  action needed.

### SPEC coverage confirmed
- S3 (`result_len`, 4 shapes): tests/test_transcript.py — all 4 covered.
- S4 (`ToolCall` fields + derived `tokens`/`input_tokens` via `//4`):
  covered, including a non-multiple-of-4 case to catch round/ceil bugs.
- S20 (stdlib-only, uv-managed, empty runtime deps, dev group): pyproject.toml
  has `dependencies = []` and `dev = [mypy, pytest, ruff]`; `toolbench/`
  imports only `json`, `dataclasses`, `__future__` — no third-party imports.
- S21 (entry points): `uv run python -m toolbench.passive` and `.probe`
  both print a stub line and exit 0 (verified interactively).