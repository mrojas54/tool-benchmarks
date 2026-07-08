# Delegator — TB-2 (Scaffold + ToolCall + result_len)

You are the delegator for Lattice ticket **TB-2** in the tool-benchmarks build.
You OWN this ticket end-to-end: plan → implement (TDD) → own-review → validate →
PR, landing it at status **`review`**. You do not merge (operator merges).

## 0. Guards & identity (run FIRST, in order)

```bash
# Standard Clause 1 — worktree assertion (line 1; HALT on mismatch, do not cd)
test "$(pwd)" = "/Users/michellerojas/tool-benchmarks-worktrees/tb-2-scaffold" || { echo "FATAL: wrong cwd"; exit 99; }

# Clause 2 — environment
export LATTICE_SPAWN_BACKEND=headless
export LATTICE_ROOT=/Users/michellerojas/tool-benchmarks

# Identity block — resolve own surface, never rely on $C11_SURFACE_ID
MY_SURF=$(c11 identify --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["caller"]["surface_ref"])')
test -n "$MY_SURF" || { echo "FATAL: no surface ref"; exit 99; }
c11 rename-tab --surface "$MY_SURF" "TB-2 Delegator"
c11 set-title  --surface "$MY_SURF" "TB-2 Delegator"
c11 set-agent  --surface "$MY_SURF" --type claude-code --model sonnet 2>/dev/null || true
c11 set-description --surface "$MY_SURF" "TB-2: uv scaffold + ToolCall record + result_len normalizer"
```

## INSTALL FACTS (this is Lattice v0.2.0 `classic` — read carefully)

- **Status ladder:** `backlog → in_planning → planned → in_progress → review → done`.
  There is **NO** `in_validation` and **NO** `pr_open`. Land at **`review`** and STOP.
- **Missing subcommands:** `lattice claim`, `plan-review`, `code-review`, `needs-human`
  DO NOT EXIST. Do NOT call them.
  - No claim needed — you already own this. Use `--actor agent:tb-2-delegator` on every mutation.
  - Reviews: **own-reviewer fallback** (below), attached as a note. There is no review CLI.
  - Blocked-on-human → `lattice status TB-2 needs_human --actor agent:tb-2-delegator`.
- **Git remote:** `origin`. Base for everything: `origin/main`.
- Every `lattice` mutation REQUIRES `--actor` (or `--name`) or it fails.
- Lattice items live in the root repo; the CLI auto-routes from this worktree.
  **Plan file must be written with the ABSOLUTE path** `$LATTICE_ROOT/.lattice/plans/<uuid>.md`
  (get the uuid from `lattice show TB-2 --json`), not a worktree-relative path.

## The ticket (SPEC S3, S4, S20, S21 · BUILDPLAN T1)

Walking skeleton of the `toolbench` package:
- `uv init` shape: `pyproject.toml` with **empty runtime deps** + a `dev` group
  (`ruff`, `mypy`, `pytest`); `uv.lock` committed. (S20)
- Empty `toolbench/` package (stdlib-only; NO third-party imports in shipped code). (S20)
- **`ToolCall`** record (S4) carrying exactly: `agent, source, project, name,
  input_chars, output_chars, tokens (=output_chars//4), input_tokens (=input_chars//4),
  session_id, ts, usage, duration_ms, error`. Use a `@dataclass`; `tokens`/`input_tokens`
  as derived properties or computed fields — match the spec's `//4` integer division.
- **`result_len`** normalizer (S3): given a dict / string / MCP block-list /
  block-local `content` payload, return a character length. Handle all four shapes.
- **Entry-point shape (S21):** the package must be runnable as
  `uv run python -m toolbench.passive` and `uv run python -m toolbench.probe` — for
  THIS ticket, create minimal module stubs with an `if __name__ == "__main__"` guard
  (a `main()` that prints a "not yet implemented" line and exits 0 is fine; later
  tickets fill them). Tests run via `uv run python -m unittest discover tests`.
- **First tests** under `tests/`: `result_len` over all four shapes; `ToolCall`
  field set + the two derived `//4` props. These are the S3/S4 acceptance tests.

Read `$LATTICE_ROOT/SPEC.md` (S3, S4, S20, S21) and `$LATTICE_ROOT/BUILDPLAN.md`
(architecture + test split) as the source of truth before planning. Do NOT
implement other tickets' modules (parser join logic, sources, reducer) — stubs only.

## Workflow (inline, own-reviewer)

1. **Plan.** `lattice status TB-2 in_planning --actor agent:tb-2-delegator`. Write the
   plan to `$LATTICE_ROOT/.lattice/plans/<uuid>.md` (absolute path). Then critically
   self-review the plan once (spec coverage, test phrasing, `//4` division, keyword-safe
   names). `lattice status TB-2 planned --actor agent:tb-2-delegator`.
2. **Implement (TDD).** `lattice status TB-2 in_progress --actor agent:tb-2-delegator`.
   `git fetch origin` and record "working against origin/main @ <sha>". Write tests
   first (RED), then implement (GREEN). Commit with a clear message. If the plan
   contradicts SPEC/codebase, deviate and flag it in the completion comment.
3. **Own-reviewer review.** Compute the diff yourself:
   `git log origin/main..HEAD --stat` + per-file diffs. Write a review in the standard
   shape — **Verdict** (PASS / PASS-WITH-NITS / FAIL) + findings
   (Critical/Major/Minor/NIT, each `file:line` + recommendation). Attach it:
   `lattice attach TB-2 --role review --inline "<review text>" --actor agent:tb-2-delegator-reviewer`.
   If FAIL/Critical/Major → fix, re-commit, re-review.
4. **Validate — the strict gate (S22-scoped for this ticket):** run and capture:
   - `uv run ruff check .`
   - `uv run mypy --strict toolbench tests`
   - `uv run python -m unittest discover tests`
   - a smoke of the entry points: `uv run python -m toolbench.passive` and `… .probe` exit 0.
   All must be green. Attach evidence:
   `lattice attach TB-2 --role validation --inline "<command outputs / green summary>" --actor agent:tb-2-delegator`.
   (No `in_validation` status exists — attach the note while `in_progress`.)
5. **PR.** Push and VERIFY the push landed (Clause 10):
   ```bash
   git push -u origin tb-2-scaffold
   git fetch origin && test "$(git rev-parse HEAD)" = "$(git rev-parse origin/tb-2-scaffold)" || { echo "PUSH DID NOT LAND — re-push"; }
   ```
   Open the PR with `gh pr create --base main --head tb-2-scaffold --title "TB-2: scaffold + ToolCall + result_len" --body "<summary; SPEC S3/S4/S20/S21; validation green>"`.
   Confirm `gh pr view --json headRefOid,baseRefOid` shows head != base (non-empty PR).
   Attach the PR URL: `lattice attach TB-2 <pr-url> --type reference --actor agent:tb-2-delegator`.
   `lattice status TB-2 review --actor agent:tb-2-delegator`.
6. **Completion comment + STOP.** `lattice comment TB-2 "<what landed, PR #, validation
   green, any deviations>" --actor agent:tb-2-delegator`. Then STOP — do not merge, do
   not touch other tickets. The Orchestrator takes it from `review`.

## Guardrails
- Never commit to `main`; never `cd` to the root repo before a commit/push (Clause: a
  commit inheriting the root cwd lands on root `main`). Stay in this worktree.
- Read-before-Write on any pre-existing file.
- If you hit a genuine blocker needing a human, set `needs_human` and post a comment
  saying what you need; do not spin.
```
