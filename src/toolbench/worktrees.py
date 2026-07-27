"""Linked git worktree inventory with a reclaim verdict per tree.

`toolbench worktrees` answers one question a human otherwise answers on a hunch:
which of the trees hanging off this clone could be reclaimed, and which are
somebody's live checkout. It prints; it never removes, never prompts, and never
touches a ref -- reclamation stays the hand-run `AGENTS.md` procedure.

Three decisions are load-bearing.

**Select with plumbing, never by grepping porcelain.** The vendored
`commit-commands:clean_gone` skill is a silent no-op in this repository because
it greps `git branch -v` for the literal `[gone]`, while real output is
`[origin/<name>: gone]`. Every fact here comes from `git worktree list
--porcelain` or `git for-each-ref`, whose fields are contracts rather than
prose.

**`%(upstream:track)` is never read, and its absence is the point.** That field
is empty BOTH for a branch in sync with a live upstream and for a branch with no
upstream at all -- eleven branches and `feat/s41` respectively, measured here.
Collapsing the two is the same class of error as the `[gone]` bug. This module
asks for `%(upstream)` (existence) and for the refs that actually contain a
head, so the ambiguity has nowhere to enter.

**Everything comes through the `Runner` seam** (`sources.Runner`), including the
two non-git calls -- `du` for size and `stat` for the admin `gitdir` mtime, which
git documents as updated "every time the linked repository is accessed" and is
therefore the idle-age signal rather than a commit date. `now` is injected for
the same reason `sources` injects `runner`: idle-age assertions must be
deterministic, and no test in this suite may read live worktree state.

A verdict is a claim, so a failing verdict-bearing call raises
`WorktreeProbeFailed` rather than guessing (the `UnprovisionedWorktree`
standard: fail loudly rather than emit a plausible-looking result). Idle age and
size are decoration, so a failing `du`/`stat` degrades to `?` and the row still
prints.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolbench.sources import Runner

# CLAIMED is declared but not yet produced: the ownership predicate ("a live
# remote-tracking ref still backs this branch") lands in Phase 2. Declaring it
# here keeps the verdict vocabulary in one place instead of widening the type
# later at every call site.
Verdict = Literal["SAFE", "DIRTY", "LOCKED", "UNIQUE-WORK", "CLAIMED"]

# Idle age / size we could not measure. Never 0 and never a plausible number: a
# tree of unknown age must not read as "touched today", and the threshold that
# Phase 2 applies to idle age must not fire on a measurement that never happened.
UNKNOWN = -1

SECONDS_PER_DAY = 86400.0

# One bound for every call, matching `sources.AGENTSVIEW_TIMEOUT_S`'s reasoning:
# no single call here is unbounded work (one listing, one ref scan, one status,
# one `du` per tree), so a single generous value is finite against a hung git
# over a stale network mount without being tight enough to fail a healthy one.
GIT_TIMEOUT_S = 60.0

# The trunk a tree's work must be reachable from before it can be called SAFE.
# `refs/remotes/` covers the other half of the design predicate -- "or from any
# live remote-tracking ref" -- and a `[gone]` upstream contributes nothing to it
# precisely because its remote ref no longer exists.
TRUNK = "refs/heads/main"
REMOTES = "refs/remotes/"

# Tab-separated because a ref name cannot contain an ASCII control character, so
# the separator can never appear inside a field. `%(upstream:track)` is
# deliberately absent -- see the module docstring.
_REF_FORMAT = "%(refname)%09%(objectname)%09%(upstream)"


class WorktreeProbeFailed(RuntimeError):
    """A command that decides a verdict failed, so no verdict is issued.

    Raised rather than degraded because the alternative is printing SAFE (or
    DIRTY) about a tree we could not inspect -- the failure mode
    `UnprovisionedWorktree` exists to prevent, applied to a report instead of a
    trial: a plausible-looking verdict is worse than a loud stop.
    """


@dataclass(frozen=True)
class Tree:
    """One linked worktree, classified.

    `branch` is None for a detached HEAD, which is judged on reachability alone.
    `reason` is the evidence for `verdict`, in the printed table, so a reader who
    has never seen this module can tell why a tree was or was not a candidate.
    """

    path: Path
    branch: str | None
    head: str
    verdict: Verdict
    idle_days: int
    megabytes: int
    reason: str


@dataclass(frozen=True)
class _Stanza:
    """One record of `git worktree list --porcelain`, read as fields, not prose.

    `locked` and `prunable` are attributes of the porcelain record and are read
    only here; re-deriving them from the human format is the mistake this module
    exists to avoid. `bare` is why `head` can be empty.
    """

    path: Path
    head: str
    branch: str | None
    bare: bool
    locked: bool
    lock_reason: str
    prunable: bool
    prunable_reason: str


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """The single default runner. Every other call site takes an injected seam."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=GIT_TIMEOUT_S,
    )


def _parse_worktree_list(stdout: str) -> list[_Stanza]:
    """Parse `git worktree list --porcelain`: records separated by blank lines.

    The first attribute of every record is `worktree <path>`; booleans (`bare`,
    `detached`, `locked`, `prunable`) appear only when true, `locked`/`prunable`
    optionally carrying a reason after the label.
    """
    stanzas: list[_Stanza] = []
    fields: dict[str, str] = {}

    def flush() -> None:
        if "worktree" not in fields:
            return
        branch_ref = fields.get("branch")
        stanzas.append(
            _Stanza(
                path=Path(fields["worktree"]),
                head=fields.get("HEAD", ""),
                # Full ref in, short name out: the porcelain field is
                # `refs/heads/<name>`, and the short form is what a reader
                # recognizes.
                branch=_short_branch(branch_ref) if branch_ref else None,
                bare="bare" in fields,
                locked="locked" in fields,
                lock_reason=fields.get("locked", ""),
                prunable="prunable" in fields,
                prunable_reason=fields.get("prunable", ""),
            )
        )

    for raw in stdout.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            fields = {}
            continue
        key, _, value = line.partition(" ")
        fields[key] = value.strip()
    flush()
    return stanzas


def _short_branch(ref: str) -> str:
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def _parse_upstreams(stdout: str) -> dict[str, str]:
    """Map short branch name -> its recorded upstream ref (`""` when it has none).

    Recorded, not live: whether the remote-tracking ref still exists is a
    separate question, answered by the refs that actually contain a head (and,
    in Phase 2, by an explicit `rev-parse --verify`).
    """
    upstreams: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        upstreams[_short_branch(parts[0])] = parts[2] if len(parts) > 2 else ""
    return upstreams


def _check(proc: subprocess.CompletedProcess[str], argv: list[str]) -> str:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise WorktreeProbeFailed(
            f"{' '.join(argv)} exited {proc.returncode}: {detail[0] if detail else '(no output)'}"
        )
    return proc.stdout


def _containing_refs(runner: Runner, repo: Path, head: str) -> list[str]:
    """Refs that contain `head`, restricted to the trunk and live remote-tracking refs.

    The tree's own local branch is outside both patterns on purpose: a branch
    containing its own tip proves nothing about whether the work survives the
    tree's removal.
    """
    argv = [
        "git",
        "-C",
        str(repo),
        "for-each-ref",
        "--contains",
        head,
        "--format=%(refname)",
        TRUNK,
        REMOTES,
    ]
    out = _check(runner(argv), argv)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _dirty_count(runner: Runner, path: Path) -> int:
    """Modified-or-untracked entries. Untracked counts: `git worktree remove`
    refuses on untracked files alone, so a tree carrying only those is not clean."""
    argv = ["git", "-C", str(path), "status", "--porcelain"]
    out = _check(runner(argv), argv)
    return len([line for line in out.splitlines() if line.strip()])


def _mtime(runner: Runner, path: Path) -> float | None:
    """Epoch mtime via `stat`, BSD spelling first and GNU second.

    Two spellings because `stat` is not POSIX: macOS wants `-f %m`, GNU wants
    `-c %Y`, and each rejects the other's flag. Both are tried rather than
    detecting the platform, so the fallback is exercised by the same seam the
    tests drive.
    """
    for flag, fmt in (("-f", "%m"), ("-c", "%Y")):
        proc = runner(["stat", flag, fmt, str(path)])
        if proc.returncode != 0:
            continue
        try:
            return float(proc.stdout.strip().split()[0])
        except (IndexError, ValueError):
            continue
    return None


def _idle_days(runner: Runner, path: Path, now: float) -> int:
    """Days since the worktree's administrative `gitdir` file was last touched.

    git updates that file's mtime "every time the linked repository is accessed"
    (gitrepository-layout(5)) and compares it against `--expire` when pruning, so
    it is the staleness signal git itself uses. A commit date would instead
    report a tree someone worked in yesterday as weeks idle.
    """
    argv = ["git", "-C", str(path), "rev-parse", "--absolute-git-dir"]
    proc = runner(argv)
    if proc.returncode != 0 or not proc.stdout.strip():
        return UNKNOWN
    mtime = _mtime(runner, Path(proc.stdout.strip()) / "gitdir")
    if mtime is None:
        return UNKNOWN
    return max(0, int((now - mtime) // SECONDS_PER_DAY))


def _megabytes(runner: Runner, path: Path) -> int:
    """Disk footprint, rounded UP -- a partly-used megabyte still costs one, and
    ceiling is what `du -sm` itself reports. `-sk` is the portable spelling and
    keeps the rounding here, where a test can see it."""
    proc = runner(["du", "-sk", str(path)])
    if proc.returncode != 0:
        return UNKNOWN
    try:
        kilobytes = int(proc.stdout.split()[0])
    except (IndexError, ValueError):
        return UNKNOWN
    return math.ceil(kilobytes / 1024)


def _verdict(
    runner: Runner, repo: Path, stanza: _Stanza, upstreams: dict[str, str]
) -> tuple[Verdict, str]:
    """LOCKED > DIRTY > UNIQUE-WORK > SAFE, in that precedence.

    The order is the design predicate's: stop at the first condition that makes
    a tree something other than a candidate, and report it rather than force it.
    """
    if stanza.locked:
        why = stanza.lock_reason or "no reason recorded"
        return "LOCKED", f"locked ({why}); removal needs --force twice"
    if stanza.prunable:
        # The working directory is already gone, so there is nothing to stat,
        # size, or `git status`; only the administrative entry remains and
        # `git worktree prune` reclaims it.
        verdict, reason = _reachability(runner, repo, stanza, upstreams)
        why = stanza.prunable_reason or "working tree missing"
        return verdict, f"prunable ({why}); {reason}"
    dirty = _dirty_count(runner, stanza.path)
    if dirty:
        plural = "entry" if dirty == 1 else "entries"
        return "DIRTY", f"{dirty} modified or untracked {plural}; git worktree remove refuses"
    verdict, reason = _reachability(runner, repo, stanza, upstreams)
    return verdict, f"clean, unlocked; {reason}"


def _reachability(
    runner: Runner, repo: Path, stanza: _Stanza, upstreams: dict[str, str]
) -> tuple[Verdict, str]:
    """Does the work survive removing this tree? Cleanliness is the caller's
    claim to make -- a prunable entry has no working directory to be clean."""
    containing = _containing_refs(runner, repo, stanza.head)
    if containing:
        first = containing[0]
        more = f" (+{len(containing) - 1} more)" if len(containing) > 1 else ""
        return "SAFE", f"head is in {first}{more}"
    upstream = upstreams.get(stanza.branch or "", "")
    recorded = (
        f"upstream {upstream} does not contain it"
        if upstream
        else "no upstream recorded"
    )
    return (
        "UNIQUE-WORK",
        f"head {stanza.head[:7]} is in no other ref; {recorded}",
    )


def _collect(
    runner: Runner, *, repo: Path, now: float
) -> tuple[_Stanza | None, list[Tree]]:
    """The main checkout and every linked tree, from one listing call.

    git lists the main worktree first, and it is never a candidate: it cannot be
    removed at any force level, and treating it as one is how a sweep script eats
    the repository it was cleaning.
    """
    listing_argv = ["git", "-C", str(repo), "worktree", "list", "--porcelain"]
    stanzas = _parse_worktree_list(_check(runner(listing_argv), listing_argv))

    refs_argv = [
        "git",
        "-C",
        str(repo),
        "for-each-ref",
        f"--format={_REF_FORMAT}",
        "refs/heads/",
    ]
    upstreams = _parse_upstreams(_check(runner(refs_argv), refs_argv))

    if not stanzas:
        return None, []
    main_checkout, linked = stanzas[0], stanzas[1:]

    trees: list[Tree] = []
    for stanza in linked:
        verdict, reason = _verdict(runner, repo, stanza, upstreams)
        if stanza.prunable:
            idle_days, megabytes = UNKNOWN, UNKNOWN
        else:
            idle_days = _idle_days(runner, stanza.path, now)
            megabytes = _megabytes(runner, stanza.path)
        trees.append(
            Tree(
                path=stanza.path,
                branch=stanza.branch,
                head=stanza.head,
                verdict=verdict,
                idle_days=idle_days,
                megabytes=megabytes,
                reason=reason,
            )
        )
    return main_checkout, trees


def classify(runner: Runner, *, repo: Path, now: float) -> list[Tree]:
    """Classify every LINKED worktree of `repo`. The main checkout is excluded.

    Phase 1 issues SAFE, DIRTY, LOCKED and UNIQUE-WORK. CLAIMED -- "a live
    remote-tracking ref still backs this branch" -- is Phase 2; until then a tree
    whose work survives only on its live upstream is reported SAFE, which is true
    of the work and says nothing yet about who owns the tree.
    """
    return _collect(runner, repo=repo, now=now)[1]


def _display_path(path: Path) -> str:
    """`~`-abbreviate the home prefix. Cosmetic, and only in the printed table --
    every command is issued with the absolute path."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if home and text.startswith(home) else text


def _cell(value: int, suffix: str) -> str:
    return "?" if value == UNKNOWN else f"{value}{suffix}"


def _branch_cell(branch: str | None, head: str) -> str:
    """A detached HEAD has no branch, so the table names the commit instead --
    that is all a reader has to identify `tech-debt-assessment-routine-fcf1e6`
    with."""
    return branch if branch else f"(detached {head[:7]})"


def _render(main_checkout: _Stanza | None, trees: Sequence[Tree]) -> str:
    """One row per `worktree` stanza, main checkout included and labelled as
    never a candidate -- what the command looked at is part of what it reports."""
    header = ("PATH", "BRANCH", "VERDICT", "IDLE", "MB", "REASON")
    rows: list[tuple[str, str, str, str, str, str]] = []
    if main_checkout is not None:
        rows.append(
            (
                _display_path(main_checkout.path),
                "(bare)"
                if main_checkout.bare
                else _branch_cell(main_checkout.branch, main_checkout.head),
                "-",
                "-",
                "-",
                "main checkout; never a candidate",
            )
        )
    for tree in trees:
        rows.append(
            (
                _display_path(tree.path),
                _branch_cell(tree.branch, tree.head),
                tree.verdict,
                _cell(tree.idle_days, " d"),
                _cell(tree.megabytes, ""),
                tree.reason,
            )
        )
    if not rows:
        return "no worktrees registered\n"

    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(5)]
    lines = []
    for row in (header, *rows):
        cells = [
            row[0].ljust(widths[0]),
            row[1].ljust(widths[1]),
            row[2].ljust(widths[2]),
            row[3].rjust(widths[3]),
            row[4].rjust(widths[4]),
            row[5],
        ]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="toolbench worktrees",
        description=(
            "Report every linked git worktree with a reclaim verdict, its idle "
            "age and its size. Prints only: it never removes a tree, deletes a "
            "branch, or touches a ref."
        ),
    )
    # No flags yet by design: `--reclaimable-only` and `--hook` arrive with the
    # predicate and the hook that need them. argparse is here for `--help` and
    # so an unknown flag fails loudly instead of being ignored.
    parser.parse_args(argv)

    main_checkout, trees = _collect(runner or _run, repo=Path.cwd(), now=time.time())
    print(_render(main_checkout, trees), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
