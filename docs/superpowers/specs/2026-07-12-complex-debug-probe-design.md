# Complex debug probe — design

**Status:** library implemented (`src/toolbench/complex.py`,
`src/toolbench/shell_safety.py`, `src/toolbench/complex_runner.py`); no CLI
yet. Fixtures under `src/toolbench/probes/complex/`; pinned corpora under
`corpus/` (packaged manifest at `src/toolbench/corpus/manifest.json`, copied
by `corpus/vendor.sh`). Pre-registered predictions committed; no live trial
matrix run yet.
**Date:** 2026-07-12 (status refreshed 2026-08-06)

## Implementation map (as shipped)

| Module / path | Responsibility |
|---|---|
| `src/toolbench/complex.py` | Defect loading, `LOCATED:` scoring, profile render; re-exports shell-safety symbols |
| `src/toolbench/shell_safety.py` | Bash tokenization, path-containment, gate-escape audits (`arm_violations`, `read_escapes`, `BANNED_TOOLS`) |
| `src/toolbench/complex_runner.py` | Hermetic worktree provision, deps cache, injectable `run_trial` |
| `src/toolbench/probes/complex/` | Per-cell fixtures (`defect.patch`, `truth.json`, `prediction.md`, `oracle.json`, `prompt.md`) |
| `src/toolbench/corpus/manifest.json` | Pinned SHAs + dep/warmup/provision recipes (`wids`, `maltese`, `rich`); `corpus/vendor.sh` copies this into `corpus/` |
| `benchmarks/harbor/toolbench-complex/` | Harbor packaging for selected defects (WIDS D2 build canary; grading not verified yet) |

`ensure_deps` / `provision_worktree` default to the packaged manifest
(`MANIFEST_PATH`). Custom corpora must pass `manifest_path` explicitly; a
stale generated `corpus/manifest.json` must never change a trial SHA.

**SHA pin for deps and warmups (PR #99):** trial trees were already exported
from the manifest SHA via `git archive`, but npm_ci previously copied
`package.json` / `package-lock.json` from the live corpus checkout and warmup
steps ran at that checkout's cwd — so a shared clone that advanced past the
pin without re-vendoring could silently rebuild caches from `HEAD`.
`ensure_deps` now reads those manifests with `git show <sha>:…` and runs
warmup commands inside a short-lived archived tree at the same SHA.
Idempotency remains existence-based (`target.exists()`): a warm cache leaf is
not rebuilt when the packaged SHA alone changes, so operators must wipe
`vendor-cache-<uid>/<repo>/` after a manifest pin bump (otherwise trials archive
the new SHA while oracles execute older `node_modules`). Open PR #102 tracks
stamp-based invalidation; do not treat that as shipped until it merges.

**Deps-cache invariants** (`UnsafeDepsCache`): the shared cache must diverge from
the corpus at the filesystem root; must be a real private directory owned by this
uid (not a replaceable symlink — the leaf is rejected before `resolve()`,
including a dangling link); contents are symlinked into trials and executed by
oracles. Default base: `tempfile.gettempdir()/vendor-cache-<uid>`.

## Why this exists

The active probe (S16–S18) measures **cost per call**: file fixed, pattern fixed,
call dictated by a run sheet. That is what makes it honest — the only free
variable is the tool. Its result (2026-07-12, session `982cc044`) is that serena
costs 1.26–3.2× Bash in context tokens per call, with the ratio collapsing toward
parity as result size grows: the overhead is a fixed per-call envelope, not a
proportional tax.

That result cannot answer the question an operator actually has, which is not
"what does one call cost" but **"which toolset should I reach for, for this kind
of problem."** A tool that costs 3× per call but needs a third as many calls is
free. Per-call cost is the wrong unit for that question.

The complex probe changes the unit from *tokens per call* to **tokens to reach a
verified outcome**, and it accepts the consequence: the agent now chooses its own
path, so the number of steps — not the cost of any step — becomes the dominant
term. Everything below exists to keep that measurable.

## What it must decide

Not a single ratio. A **routing profile**: for each class of defect, which
toolset reaches a verified fix for the fewest context tokens, and does the agent
already pick that toolset on its own when left unrestricted.

## Design

### 1. Corpus — two repos, crossed

Two of the operator's own public repos, each vendored **read-only at a pinned
SHA** under `corpus/`. The working repos are never touched.

| repo | shape | languages |
|---|---|---|
| `wids-nyc-reading-group-assistant` | `web/` 148 TS/TSX, `migrations/` 20 SQL, `scripts/` 13 Py | TS, SQL, Python |
| `maltese-agent` | 3 Rust crates (`falcon-detective`, `falcon-mcp`, `falcon-agent`) + TS | Rust, TS |

Using the operator's own repos attacks the training-data confound: a vendored
third-party package (flask, rich) is likely memorized, and a model that already
knows where things live compresses navigation for *every* arm and shrinks the gap
under measurement. Recent, personal repos are far less likely to be memorized.
The confound is reduced, not eliminated — public code may still have been seen —
and this belongs in the report, not a footnote.

**Repo is a blocking factor, not a confound — because defects are crossed, not
nested.** Every defect class is instantiated in *both* repos. If D1 lived only in
Rust and D2 only in TS, "serena won D1, lost D2" would be unattributable: tool or
language? Crossing lets the repo effect be measured and subtracted.

### 2. Defects — five classes, pre-registered

Each defect is injected by patch into the pinned corpus. Each has a
**pre-registered predicted winner, committed before any trial runs.**

| id | defect | why it discriminates | predicted winner |
|---|---|---|---|
| D1 | symbol renamed on one type; many unrelated types share the name | `rg` returns a wall of false positives; `find_referencing_symbols` resolves true callers | serena |
| D2 | handler resolved by string (`supabase.rpc('fn')`; MCP tool registry) | the LSP reference graph contains **zero** edges for a string literal; grep finds it instantly | native / bash |
| D3 | wrong argument originates 3 frames up a call chain | transitive caller traversal vs manual grep-chaining | serena |
| D4 | module moved, import path stale | pure file location — one call for `Glob`, `fd`, or `find_file` alike | neutral (control) |
| D5 | cross-language break: SQL function renamed, TS string call stale (TS→Rust in `maltese-agent`) | reference is a string **and** crosses into a language serena may not index at all — blind at both ends | native / bash |

**D5 is the load-bearing defect, and it was not invented for this benchmark — it
is a seam the corpus already had.** That distinction matters: `active-probes.md`
records three separate occasions on which fixtures written to match the author's
mental model supplied *confirmation, not verification*. A defect discovered in
the corpus is pinned to an observed shape; one designed to prove a hypothesis is
the same old bug in a new costume.

A defect class whose prediction comes out **wrong** is the most informative cell
in the table. A run in which every prediction lands has taught us nothing.

### 3. Arms — four toolsets

The existing active probe pits serena against **Bash** (`rg`/`fd`). But that is
not what a default Claude Code user has: the default is the **native `Grep` /
`Glob` / `Read` / `Edit` tools**, which have their own output shapes and token
profiles. "Should I install serena?" is therefore *serena vs native*, and that
comparison has never been run. Bash remains as a third arm so the new numbers
stay comparable to the existing active-probe results.

Shared baseline, every arm:

- `Bash(<test command>:*)` — scoped to the test command only, so the fix
  checkpoint is verifiable without handing `rg` to the serena arm.
- `Read` — held constant across arms, so the measured variable is **search and
  edit**, not file viewing.
- `TodoWrite` — tool-neutral bookkeeping.
- **`Agent` / `Task` is banned in every arm. Non-negotiable.** A subagent
  inherits a full toolset: a serena-restricted arm could spawn a general-purpose
  agent, run `rg` inside it, and return the answer. The restriction would appear
  enforced and be silently void — the same defect class as the TB-29
  `--exclude-subagents` no-op that the suite ratified while it did nothing.

| arm | adds |
|---|---|
| A serena | `mcp__plugin_serena_serena__*` (search + edit) |
| B native | `Grep`, `Glob`, `Edit` |
| C bash | `Bash` (full) |
| D control | everything except `Agent` |

Arm D is the only arm whose result converts directly into action: if D matches
`best(A,B,C)` on every defect class, the agent already routes correctly and there
is nothing to fix. If D is worse, it is mis-routing, and that is a CLAUDE.md rule
worth writing.

### 4. Arm precondition check (the turn-0 analogue)

Serena's symbolic tools are LSP-backed, and an LSP is **per language**. Absent a
working language server, serena degrades — sometimes silently — to text search. A
"serena arm" that is quietly doing text search is an invalid arm that still emits
a plausible number.

So each `(repo, language)` pair gets a **capability precheck before any trial**:
confirm serena returns real symbols, not a text fallback. If it cannot, the arm
is **disqualified for that cell, not scored**.

This is exactly why turn 0 exists in the atomic probe: a failed arm is
unrecoverable, so the precondition must be tested by something that is not an arm.

### 5. Isolation

- Fresh git worktree per trial: pinned corpus + defect patch.
- Fresh headless session per trial (`claude -p`), never resumed.
- **Arms never share a transcript.** Sharper than in the atomic probe: arm 1
  solving the bug puts the answer in context and arm 2 solves it for free.
- Serena's LSP index is **pre-warmed outside the measured window** and held
  constant. A fresh worktree per trial would otherwise charge serena a cold-index
  tax on every trial that a real user pays once — which could single-handedly
  manufacture a serena loss.
- **One session per trial means the session *is* the cell.** Grouping needs only a
  manifest mapping session id → `(repo, defect, arm, trial)`; no new accounting
  layer, and no per-call attribution problem of the kind the atomic probe has.
- Worktree branch is additionally named `probe/<repo>/<defect>/<arm>/t<N>`. This
  is a *bonus*, not the mechanism: S40's `usage_by_branch` buckets **usage/cache**
  tokens by `gitBranch`, which is not the tool-result context-token metric below,
  but it does hand us per-cell cache accounting at no cost.

**Enforcement is by transcript audit, not by filesystem walls (decided
2026-07-12, post-implementation).** Two measurements forced this: (i) an
`--allowedTools Bash(<cmd>:*)` gate does **not** restrict reads — bare `rg`/`cat`
and `;`-chained reads run under a gate scoped to only the oracle command
(measured; see `.superpowers/sdd/progress.md`); (ii) the bash/control arms have a
full shell on a machine where the pristine corpus physically exists, so
`find ~ -name <source>` reaches unpatched source regardless of where trial
artifacts or dep caches live. Making the trial *tree* hermetic (single commit, no
parent, no reachable dep-cache path) removes the *obvious* leaks — `git diff`, a
`..`-walk off a symlink — but cannot remove the pristine clone from the disk.
Therefore:

- The trial tree is made hermetic (standalone repo, one commit; dep cache diverges
  from the corpus checkout at the filesystem root) to close the discoverable
  leaks, AND
- `arm_violations` is extended to a **read-scope audit**: any tool call whose
  resolved read path lies **outside the trial tree** voids the trial. This
  generalizes over symlink-walk, `find`, and `git diff` in one rule, and is the
  primary enforcement — a flag is a claim, the transcript is evidence.
- **Chosen posture: audit now, sandbox later.** The pilot runs tamper-EVIDENT
  (the audit voids offending trials) rather than tamper-PROOF. Whether the arms
  actually reach for outside source is itself measured in the pilot; per-trial
  filesystem sandboxing (a container with only the trial tree mounted) is deferred
  and adopted for the real run only if the pilot shows arms attempting the escape.
  Tamper-evident is sufficient for a benchmark whose failure mode is a *believable
  wrong number*, because a voided trial produces no number at all.

### 6. Metrics

Two checkpoints per trial:

- **N1 — navigation.** Context tokens spent up to first correct localization.
  The moment is made explicit rather than inferred: the prompt instructs the agent
  to emit a single `LOCATED: {"file":…,"symbol":…,"lines":[…]}` line as soon as it
  believes it knows, then continue to the fix. N1 is the sum of `tool_result`
  tokens in the transcript *before* that message; correctness is an exact match
  against the injection patch, which is ground truth. This is the tool comparison.
- **N2 — edit.** Additional context tokens from the `LOCATED:` line to
  oracle-green (the repo's own test suite passes). This is the realism check.

A trial that reaches oracle-green **without** ever emitting a correct `LOCATED:`
line is a solved fix with no navigation measurement — recorded as such, not
back-filled. Guessing its way to green is a real outcome and must be visible.

Reported per `(repo, defect, arm)`: **solve rate, and median cost among solves —
as two separate numbers, never blended.** An arm that never finds the bug is
cheap, and its cheapness is meaningless; cost is uninterpretable without
conditioning on success.

Cost is **context tokens**, never output tokens. TB-17 established that output
tokens are not comparable across arms (the bash sentinel/description tax), and
that finding carries over unchanged.

### 7. Failure handling

Step budget ~40 tool calls. Exceeding it is an unsolved trial: cost recorded,
excluded from cost-among-solves, and **named in the report**. A benchmark that
hides its failures is the same defect class as a fully-seeded table — the project
norm is *visibly incomplete, never quietly wrong*.

## Run size

Matrix is 2 repos × 5 defects × 4 arms.

- **Pilot: 1 trial = 40 sessions.** Purpose is to prove the harness — isolation
  holds, the oracle fires, tokens are attributable, serena's prechecks pass. **It
  is not an answer.** Path variance means N=1 is never an answer.
- **Real run: trial depth set from the pilot's observed variance**, not guessed
  now. At 5 trials this is 200 sessions; that number should be chosen with the
  variance in hand.

## Risks

1. **Serena LSP warm-index tax** — mitigated by pre-warming outside the measured
   window (§5). If unmitigated, it invalidates the serena arm.
2. **Training-data familiarity** — reduced by using personal repos, not
   eliminated. Ceiling on generalizability; goes in the report.
3. **Model nondeterminism** — the reason for repeated trials and for reporting
   medians, not means.
4. **Bash arm's clumsy edits** — writing via `sed`/`patch` is genuinely awkward
   and will depress the bash arm's *fix-phase* solve rate. That is a real cost and
   fair to measure, but it must not be misread as "bash is bad at debugging." N1
   (navigation) is unaffected and is where the tool comparison lives.
5. **Agent-tool escape** — see §3. Ban is load-bearing; verify it is enforced, do
   not trust the flag.

## Precheck results (run 2026-07-12 — resolved)

| question | answer | consequence |
|---|---|---|
| Does serena index Rust? | **Yes, but only when configured.** `maltese-agent` auto-detected as `['typescript']` only and refused to extract any Rust symbol. With `languages: [rust, typescript]` it returned `{"Function": ["caesar_decode"], "Module": ["tests"]}`. `rust-analyzer` is installed. | Two-repo crossed design **survives**. Vendored corpora **must ship an explicit `.serena/project.yml`** — auto-detect is not trustworthy. |
| Does serena index SQL? | **No, and it never can.** `activate_project` with `sql` raises `Invalid language: sql`; the 60+ supported languages do not include it. | D5's premise confirmed a priori. Structural, not a misconfiguration. |
| Test commands? | `wids-nyc`: `npx vitest run` (in `web/`). `maltese-agent`: `cargo test`. | Fix oracle available for both repos. |
| Pinned SHAs | `wids-nyc` `a39cdd0`; `maltese-agent` `7b8fa95` | — |

**The auto-detection finding is the important one, and it is independent of this
benchmark.** An unconfigured serena is invisibly weaker than a configured one. A
benchmark run against the auto-detected config would have measured a crippled
serena and blamed the tool — the same "confirmation, not verification" trap, in a
new costume. Only running the precheck caught it.

**Refinement to D2/D5, now measured rather than hypothesized:** serena is *not*
blind to SQL. Its `search_for_pattern` is a plain regex search and works on any
file. What dies on an unindexed language is serena's *symbolic* advantage — it
degrades to **being a more expensive grep**. D5 therefore measures how much that
forced fallback costs, not whether it happens.

## Still open (for implementation)

- Confirm each defect class has a genuine precondition in **both** repos before
  writing patches. If a class exists in only one, that cell is confounded and must
  be flagged, not quietly reported. **Manufacturing a seam to fill the matrix is
  forbidden** — a defect must be one the corpus already had.

## Not doing (YAGNI)

- No new scoring engine. Branch-keyed worktrees reuse `passive --run-manifest`
  (S40).
- No reuse of the `tools/` corpus: those five files are the active probe's matched
  targets, and a serena/`rg` call against them is structurally an arm. The two
  benchmarks must not share a corpus.
