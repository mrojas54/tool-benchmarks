# Delegator — TB-3 (parse_session id-join)

You OWN Lattice ticket **TB-3** end-to-end: plan → implement (TDD) → own-review →
validate → PR, landing at status **`review`**. You do NOT merge.

## 0. Guards & identity (run FIRST, in order)

```bash
test "$(pwd)" = "/Users/michellerojas/tool-benchmarks-worktrees/tb-3-parse" || { echo "FATAL: wrong cwd"; exit 99; }
export LATTICE_SPAWN_BACKEND=headless
export LATTICE_ROOT=/Users/michellerojas/tool-benchmarks
MY_SURF=$(c11 identify --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["caller"]["surface_ref"])')
test -n "$MY_SURF" || { echo "FATAL: no surface ref"; exit 99; }
c11 rename-tab --surface "$MY_SURF" "TB-3 Delegator"
c11 set-title  --surface "$MY_SURF" "TB-3 Delegator"
c11 set-agent  --surface "$MY_SURF" --type claude-code --model sonnet 2>/dev/null || true
c11 set-description --surface "$MY_SURF" "TB-3: parse_session id-join + ParseResult + fixtures"
```

## INSTALL FACTS (Lattice v0.2.0 classic — read carefully)
- **Status ladder:** `backlog → in_planning → planned → in_progress → review → done`. NO `in_validation`, NO `pr_open`. Land at **`review`** and STOP.
- **Missing subcommands** — do NOT call: `lattice claim`, `plan-review`, `code-review`, `needs-human`. You already own this; use `--actor agent:tb-3-delegator` on every mutation. Reviews = own-reviewer fallback (below). Blocked-on-human → `lattice status TB-3 needs_human --actor agent:tb-3-delegator`.
- Every `lattice` mutation REQUIRES `--actor`.
- **Plan file:** write to the ABSOLUTE path `$LATTICE_ROOT/.lattice/plans/<uuid>.md` (uuid from `lattice show TB-3 --json`), never worktree-relative.
- **Git:** remote `origin`. This branch `tb-3-parse` is based on **`origin/tb-2-scaffold` (PR #1)**, NOT main — you build on `toolbench/transcript.py` from TB-2. Your PR base is `main`; body must say "based on #1 — merge that first; this rebases."
- **`gh` FOOTGUNS** (from TB-2): the `gh` alias (`op plugin run -- gh`) fails headless. Use the real binary **`/usr/local/bin/gh`**. Before `gh pr create`, the default token lacks PR scope — run `/usr/local/bin/gh auth switch` to the keyring account (repo scope) and confirm `/usr/local/bin/gh auth status`.
- **Shared file:** `toolbench/__init__.py` may also be edited by TB-4 in parallel — keep your edits there minimal (additive re-exports only); union-merge resolves later.

## The ticket (SPEC S1, S2, S5, S6, S24 · BUILDPLAN T2)

Extend `toolbench/transcript.py` (TB-2 created `ToolCall` + `result_len`) with the
parser. Read `$LATTICE_ROOT/SPEC.md` S1/S2/S5/S6/S24 as source of truth.

- **`parse_session(path)` → `ParseResult(calls, malformed)`** (S5) — additive tuple/dataclass so the malformed count reaches the report footer.
- **S1 id-join.** Join each assistant `tool_use` block to its result by id. Join key = assistant `message.content[].id` (blocks with `type=="tool_use"`), matched against EITHER top-level `toolUseID` OR block-local `message.content[].tool_use_id` (user side). Exercise BOTH key locations in fixtures.
- **S2 payload precedence.** Result payload resolves from top-level `toolUseResult` OR block-local `message.content[].content`. When BOTH exist, **block-local `content` WINS**, and record which source was used. Helpers `_result_id` / `_result_payload`.
- **S5 malformed non-fatal.** Malformed/partial JSON lines counted, skipped, never fatal; count exposed on `ParseResult.malformed`.
- **S6 interrupted kept.** A `tool_use` with no matching result → `output_chars=0, no_result=True`; KEPT, not dropped.
- **S24 fixtures** — parser fixtures exercising: a string result, an MCP block-list, a **block-local `content`** payload, an interrupted (no-result) call, and a malformed line. The block-local fixture de-risks the flagged join-key-on-real-data primary risk — make it faithful to real Claude Code JSONL shape.

Do NOT implement sources.py, passive.py, or probe.py (other tickets).

## Workflow (inline, own-reviewer)
1. **Plan.** `lattice status TB-3 in_planning --actor agent:tb-3-delegator`; write plan to `$LATTICE_ROOT/.lattice/plans/<uuid>.md`; self-review once (join-key correctness, block-local precedence, fixture fidelity); `lattice status TB-3 planned --actor agent:tb-3-delegator`.
2. **Implement (TDD).** `lattice status TB-3 in_progress --actor agent:tb-3-delegator`; `git fetch origin`, record "working against origin/tb-2-scaffold @ <sha>"; tests first (RED) then implement (GREEN); commit. Deviate-with-flag if plan contradicts SPEC.
3. **Own-reviewer review.** `git log origin/tb-2-scaffold..HEAD --stat` + per-file diffs; write Verdict (PASS/PASS-WITH-NITS/FAIL) + findings (file:line + recommendation); `lattice attach TB-3 --role review --inline "<review>" --actor agent:tb-3-delegator-reviewer`. Fix Critical/Major and re-review.
4. **Validate.** Run + capture: `uv run ruff check .`; `uv run mypy --strict toolbench tests`; `uv run python -m unittest discover tests` (all green). `lattice attach TB-3 --role validation --inline "<outputs>" --actor agent:tb-3-delegator`.
5. **PR.** `git push -u origin tb-3-parse`; verify: `git fetch origin && test "$(git rev-parse HEAD)" = "$(git rev-parse origin/tb-3-parse)"`. `/usr/local/bin/gh pr create --base main --head tb-3-parse --title "TB-3: parse_session id-join" --body "based on #1 — merge that first; this rebases. SPEC S1/S2/S5/S6/S24. Validation green."`. Confirm head != base. `lattice attach TB-3 <pr-url> --type reference --actor agent:tb-3-delegator`; `lattice status TB-3 review --actor agent:tb-3-delegator`.
6. **Completion comment + STOP.** `lattice comment TB-3 "<landed, PR #, validation green, deviations>" --actor agent:tb-3-delegator`. STOP — do not merge, do not touch other tickets.

## Guardrails
- Never commit to main; never `cd` to the root repo before commit/push. Stay in this worktree.
- Read-before-Write on pre-existing files (`transcript.py` exists from TB-2 — read it first).
- Genuine human blocker → `needs_human` + comment; do not spin.
