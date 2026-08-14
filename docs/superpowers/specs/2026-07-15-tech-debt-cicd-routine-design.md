# Tech-debt routine — a repo CI gate, and a shared local assessment tool

## Status

**CI gate shipped** (`.github/workflows/ci.yml`, merged with the design; extended
by PR #95): every PR and every push to `main` runs
`uv sync --frozen --python 3.13`, then the documented gate
(`ruff check .`, `python -m toolbench.complexity_gate --base <sha>`,
`mypy --strict src/toolbench tests`, `pytest -q`). Checkout uses
`fetch-depth: 0` so the complexity base commit is available. `[tool.mypy]` in
`pyproject.toml` pins the same type-check scope so a bare local `mypy` mirrors
CI (and does not type-check `tools/`). The assessment tool remains local-only
under `~/tech-debt-work/` (not in this repo).

## Problem

The repo had a well-defined quality gate — `uv run ruff check .`, `uv run mypy
--strict toolbench tests`, `uv run pytest -q` (documented in `AGENTS.md` and
`README.md`) — but nothing ran it except a human at a keyboard. Before this
change there was no `.github/` workflow for the gate: the only automated check
was the `.githooks/pre-commit` hook, and it guards Lattice event-log integrity,
not the gate. So the gate held only as long as every contributor remembered to
run it locally. That is exactly the condition under which tech debt accretes
silently.

Separately, there is an established local practice: `~/tech-debt-work/<REPO>/` is
where per-repo tech-debt work and reports live (today it holds a
`wids-nyc-reading-group-assistant/` checkout). The assessment reports in that area
should follow one convention — `~/tech-debt-work/<REPO_NAME>/YYYYMMDD_tech_debt.md`
— so a repo's debt history is a sorted, dated series in one predictable place.

These are two different jobs answered in two different places:

- **The gate** answers *"did this change introduce debt?"* — it blocks, runs on
  the change, and belongs in the cloud (GitHub Actions), committed to the repo.
- **The assessment** answers *"how much debt have we accrued that a green gate
  can't see?"* — it is a dated, human-read report filed on local disk under
  `~/tech-debt-work/`, and it is inherently cross-repo.

The two cannot share an executor: a GitHub Actions runner is an ephemeral cloud
VM with no access to `~/tech-debt-work` on this machine. So the gate is CI; the
assessment is a **shared local tool**, run against any repo.

## Design

### Two deliverables, two locations

| deliverable | location | committed? | trigger |
|---|---|---|---|
| CI gate | `tool-benchmarks/.github/workflows/ci.yml` (+ `src/toolbench/complexity_gate.py` from PR #95) | yes — the gate enforcement surface | `pull_request`, `push` to `main` |
| Assessment tool | `~/tech-debt-work/tech_debt_report.py` | no — shared cross-repo personal tooling | run on demand (or a local schedule the user enables) |
| Assessment output | `~/tech-debt-work/<REPO_NAME>/YYYYMMDD_tech_debt.md` | no — local report series | written by the tool |

The assessment tool lives outside every repo because its outputs are centralized under
`~/tech-debt-work/` and it is meant to run against many repos, not just this one.
The complexity gate module lives in-package so `[tool.mypy] files` covers it and
tests can pin the policy hermetically.

### 1. `.github/workflows/ci.yml` — the gate (blocking)

Mirrors `wids-nyc-reading-group-assistant`'s `ci.yml` *structure*; runs *this*
repo's documented gate verbatim.

- **Triggers:** `pull_request: {}` and `push: { branches: [main] }`
- **`permissions: contents: read`** — the gate only reads the tree
- **Two jobs**, both `runs-on: ubuntu-latest`, `timeout-minutes: 15`:
  - `gate` — default install (no optional extras): ruff + complexity + mypy +
    pytest
  - `tracing` (#112) — `uv sync --extra tracing`, asserts `lmnr` is
    importable, then pytest again so real-SDK tests cannot skip silently on
    the default lane
- **Checkout:** `actions/checkout@v4` with `fetch-depth: 0` (full history for
  the complexity base SHA)

| step | command | why |
|---|---|---|
| checkout | `actions/checkout@v4` (`fetch-depth: 0`) | full history so `--base` resolves |
| install uv | `astral-sh/setup-uv@v5` with `enable-cache: true`, `cache-dependency-glob: uv.lock` | cache keyed on the lockfile |
| sync deps (`gate`) | `uv sync --frozen --python 3.13` | `--frozen` fails CI if `uv.lock` drifted from `pyproject.toml`; `--python 3.13` matches `requires-python >=3.13` (uv provisions the interpreter if the runner lacks it); keeps the default install free of optional extras |
| lint | `uv run ruff check .` | the documented gate |
| complexity | `uv run python -m toolbench.complexity_gate --base $COMPLEXITY_BASE_SHA` | PR base SHA, or push `github.event.before`; fail only on new / crossed / worsened debt vs that baseline (`DEFAULT_THRESHOLD` 10; warn on ≥2 rise still ≤10). Budget lives only in `complexity_gate.py` — no `[tool.ruff.lint.mccabe]` decoration (#112) |
| type-check | `uv run mypy --strict src/toolbench tests` | the documented gate (path updated with the src-layout move; originally `toolbench tests`) |
| test (`gate`) | `uv run pytest -q` | the documented gate on the default install |
| sync deps (`tracing`) | `uv sync --frozen --python 3.13 --extra tracing` | install the optional Laminar SDK without contaminating `gate` |
| assert lmnr | `python -c "… find_spec('lmnr') …"` | fail loudly if the extra stops delivering the SDK |
| test (`tracing`) | `uv run pytest -q` | exercises lmnr-guarded tests that skip on `gate` |

`--frozen` is not incidental — a lockfile silently drifted from `pyproject.toml`
is itself tech debt, and this is the cheapest place to catch it. The dev group
(ruff, mypy, pytest) installs by default under `uv sync`, so no extra flag is
needed to reach the gate tools. The `gate` job runs `ruff check .`, **not**
`ruff format --check`, and mypy over `src/toolbench tests` and no wider — it
reproduces the gate the repo documents, it does not invent a stricter one
beyond the intentional complexity-regression step and the separate `tracing`
lane. (Before the src-layout move the mypy path was `toolbench tests`; CI and
live operator docs now use `src/toolbench`.) Locally, `[tool.mypy]` in
`pyproject.toml` pins the same `files` + `strict` so a bare `uv run mypy`
mirrors CI and does not descend into the `tools/` probe corpus; CI still
passes explicit paths + `--strict` on the command line (those take
precedence).

The complexity base is placed in an environment variable and quoted by a static
`run:` command — event text is not interpolated into shell source.

### 2. `~/tech-debt-work/tech_debt_report.py` — the shared assessment tool

Pure standard library, targeting `python3 >= 3.9` so it runs under any
interpreter — a shared tool must not depend on any single repo's `uv` env. No
third-party imports, no network, no execution of the target repo's code.
Deterministic given the tree and the date.

**CLI**

| flag | default | meaning |
|---|---|---|
| `--repo PATH` | `.` | the repo to assess |
| `--tech-debt-root PATH` | `~/tech-debt-work` | base output directory |
| `--out PATH` | *(computed)* | explicit output file; overrides the computed path |
| `--date YYYYMMDD` | today (local) | date stamp; override for backfill / deterministic tests |
| `--stdout` | off | also echo the Markdown to stdout |

**Output path.** `<tech-debt-root>/<REPO_NAME>/<YYYYMMDD>_tech_debt.md`, where
`REPO_NAME` is the basename of `git -C <repo> rev-parse --show-toplevel` and
`YYYYMMDD` is the local date (e.g. `tool-benchmarks` today →
`~/tech-debt-work/tool-benchmarks/20260715_tech_debt.md`). The per-repo directory
is created if absent. A same-day file is overwritten — the report is an
idempotent daily snapshot, so a re-run refreshes today's rather than appending.

> **Date format:** the request wrote `YYYMMDD`; read here as the standard 8-digit
> `YYYYMMDD` with an underscore before `tech_debt.md`. If `YYYY-MM-DD` (hyphens)
> is preferred, it is a one-line change to the format string.

**Inputs.** File list from `git -C <repo> ls-files '*.py'` — exactly the tracked
Python surface, no `.venv`/caches/untracked scratch. Every target repo is a git
repo, so `git ls-files` is the right, portable source. A non-git `--repo` is a
clean error and a non-zero exit.

**Report.** A Markdown document headed with repo name, date, and `git rev-parse
--short HEAD` + branch, then the five sections below. It deliberately does **not**
re-run ruff or mypy: on a repo whose gate keeps them at zero, counting them weekly
measures nothing. It measures the debt that *survives* a green gate.

| section | signal | why it is debt the gate can't see |
|---|---|---|
| Marker census | counts of `TODO` / `FIXME` / `XXX` / `HACK` / `BUG`, capped `file:line` list | deferred work, invisible to lint/type/test |
| Suppression census | counts of `# type: ignore` and `# noqa`, capped `file:line` list | debt that passes *because* it is suppressed — the sharpest signal in a `--strict`/ruff-clean repo |
| Module size | top-10 tracked `.py` by LOC; flag any `> 500` | a file grown large is often doing too much — a refactor-pressure gauge |
| Test surface | test-file count (`test_*.py`/`*_test.py`), `def test_` count, skip/`xfail`/`unittest.skip` marker count | rising skips = coverage quietly eroding |
| Totals | tracked `.py` count, total LOC | the denominator for every number above |

`file:line` lists are capped (default 20 per section) so the report stays legible
on a large tree. Marker matching keys on the token so a design note discussing
`# type: ignore` is not miscounted as a suppression.

**Exit code.** `0` on success regardless of what it finds; non-zero only on tool
error (not a git repo, unreadable tree). The assessment is advisory — it is never
a second, clock-triggered gate.

### The routine (how it runs on a cadence)

- **On demand:** `python3 ~/tech-debt-work/tech_debt_report.py --repo
  ~/tool-benchmarks` — produces today's dated file and prints its path.
- **Weekly automation:** a `launchd` plist (macOS) or `cron` line, provided in the
  PR/handoff for the user to enable. It is **not** auto-installed: a launchd agent
  is persistent system configuration, which is the user's to opt into (or to
  authorize explicitly in a later step).

The gate (`ci.yml`) is already the automatic, on-change half. The assessment's
cadence is a deliberate, low-frequency choice the user owns.

### Security & permissions

- `ci.yml`: `permissions: contents: read`; the complexity base SHA is copied
  into an env var and quoted by a static `run:` (event text is not interpolated
  into shell source); action majors pinned (`checkout@v4`, `setup-uv@v5`).
- The tool: read-only against the target repo (`git ls-files` + file reads);
  writes only under `~/tech-debt-work`; no network; never executes repo code.

## Testing / validation

The tool is, by the chosen design, **not** committed into `tool-benchmarks`, so it
is not covered by this repo's `pytest`/`mypy`. It is validated by running it and
checking its output, not by a harness:

| check | pins |
|---|---|
| run against `~/tool-benchmarks` | writes `~/tech-debt-work/tool-benchmarks/20260715_tech_debt.md`; sections render |
| marker/suppression counts cross-checked against a manual `rg` on the same tree | the census is accurate, not just present |
| run against a second repo (`~/wids-nyc-reading-group-assistant`) | `REPO_NAME` derivation + per-repo foldering are correct across repos |
| `--date 20260101` | filename is deterministic and honors the override |
| `--repo` at a non-git path | clean error message, non-zero exit |
| the gate, dry-run locally before pushing: `uv run ruff check .`, `uv run python -m toolbench.complexity_gate --base origin/main`, `uv run mypy --strict src/toolbench tests`, `uv run pytest -q` | `ci.yml` will pass; the first PR run is the live confirmation |
| complexity policy hermetic tests | `tests/test_complexity_gate.py` pins threshold / noqa / CI wiring; forbids an inert mccabe budget unless C901 is selected |
| tracing-lane CI pin | `tests/test_observability.py` asserts `ci.yml` installs `--extra tracing` and that lmnr is importable in that job |

## Out of scope

- **A GitHub-Actions assessment workflow / artifact.** The assessment is local by
  decision — its home is `~/tech-debt-work/<REPO>/`, which no cloud runner can
  reach.
- **Committing, gating, or unit-testing the tool inside `tool-benchmarks`.** It is
  shared cross-repo tooling; validation is by execution (above).
- **Auto-installing a scheduler** (`launchd`/`cron`). Persistent system config; a
  ready-to-enable snippet is provided, enabling it is the user's.
- **Version-controlling `~/tech-debt-work`.** It is a plain directory today; making
  it a git repo is a separate, later choice.
- **Broadening the gate further** — `ruff format --check`, mypy over `tools/` or
  the repo root, coverage thresholds. Each is its own decision. The complexity
  regression step (PR #95) is an intentional, documented addition to S22; it is
  not a license to pile on unrelated checks.
- **A Lattice ticket (TB-XX).** The board is minted via `lattice create`, never by
  hand (the pre-commit hook enforces this). Board tracking, if wanted, is a
  separate deliberate step.
- **Recording the `~/tech-debt-work/<REPO_NAME>/YYYYMMDD_tech_debt.md` convention
  in global `CLAUDE.md`/rules.** Offered separately; writing persistent config is
  a change to confirm, not to assume.
- **Mirroring wids's other CI jobs** (`web`, `edge-functions`). `tool-benchmarks`
  is a single Python package; there is no web or Deno surface.
