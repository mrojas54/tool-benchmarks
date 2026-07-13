"""Drive one trial: worktree -> headless claude -> oracle -> scored result.

`launch` and `oracle` are injected so the suite never shells out to a real
`claude` binary (the project's fake-runner pattern, S24).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from toolbench.complex import (
    BANNED_TOOLS,
    DEFAULT_FIXTURE_ROOT,
    ArmSpec,
    DefectSpec,
    TrialResult,
    score_trial,
)

Launch = Callable[[list[str], Path], Path]
Oracle = Callable[[Path], bool]


class UnprovisionedWorktree(RuntimeError):
    """Raised when `run_trial` finds no `PROMPT.md` in the worktree.

    A missing `PROMPT.md` means `provision_worktree` never ran (or failed)
    against this worktree -- the trial is broken, not merely under-prompted.
    The old behavior fell back to `defect.rationale`, but that field is the
    predicted-winner justification (e.g. "serena should win here because...
    "), not a bug report: handing it to the agent under test leaks the
    answer straight into its input, the same defect class already fixed
    four times over in fixture prompts that leaked the answer to `rg`.
    There is no correct default prompt, so there is no fallback -- a broken
    trial must fail loudly rather than quietly produce a plausible-looking,
    compromised result.
    """


def build_claude_argv(prompt: str, arm: ArmSpec, cwd: Path) -> list[str]:
    """Headless invocation for one arm.

    `--disallowedTools` is belt-and-braces beside the allowlist: it states the
    Agent ban explicitly, so a future permissive default cannot quietly reopen the
    subagent escape hatch. The ban is still verified post-hoc from the transcript
    (`arm_violations`) -- a flag is a claim, not evidence.
    """
    return [
        "claude",
        "-p",
        prompt,
        "--allowedTools",
        ",".join(arm.allowed_tools),
        "--disallowedTools",
        ",".join(BANNED_TOOLS),
        "--add-dir",
        str(cwd),
    ]


def run_trial(
    defect: DefectSpec,
    arm: ArmSpec,
    trial: int,
    workdir: Path,
    launch: Launch,
    oracle: Oracle,
) -> TrialResult:
    """Run one cell and score it. The prompt is the defect's bug report.

    Raises `UnprovisionedWorktree` if `PROMPT.md` is absent -- see that class
    for why there is no fallback prompt to substitute.
    """
    prompt_path = workdir / "PROMPT.md"
    if not prompt_path.exists():
        raise UnprovisionedWorktree(
            f"{workdir} has no PROMPT.md: the worktree was not provisioned "
            "(see provision_worktree). There is no default prompt to fall "
            "back to."
        )
    prompt = prompt_path.read_text(encoding="utf-8")
    session_path = launch(build_claude_argv(prompt, arm, workdir), workdir)
    return score_trial(session_path, defect, arm, trial, oracle(workdir))


def shell_oracle(test_cmd: list[str], test_cwd: str) -> Oracle:
    """Real oracle: the corpus repo's own test suite must exit 0."""

    def _oracle(workdir: Path) -> bool:
        proc = subprocess.run(
            test_cmd, cwd=workdir / test_cwd, capture_output=True, check=False
        )
        return proc.returncode == 0

    return _oracle


def branch_name(defect: DefectSpec, arm: ArmSpec, trial: int) -> str:
    """`probe/<repo>/<defect_id>/<arm>/t<N>` -- one session per trial means the
    session IS the cell; this name just makes the cell legible in `git branch`."""
    return f"probe/{defect.repo}/{defect.id}/{arm.name}/t{trial}"


def _find_fixture_dir(fixture_root: Path, defect: DefectSpec) -> Path:
    """Locate `<repo>-<id>-<slug>` under `fixture_root` without re-deriving the
    slug: ids repeat across repos (wids and maltese both have a D3), so the
    match is on (repo, id) as a glob prefix, never on id alone."""
    matches = sorted(fixture_root.glob(f"{defect.repo}-{defect.id}-*"))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one fixture dir for {defect.repo}/{defect.id} under "
            f"{fixture_root}, found {len(matches)}"
        )
    return matches[0]


def provision_worktree(
    defect: DefectSpec,
    arm: ArmSpec,
    trial: int,
    corpus_root: Path,
    dest: Path,
    fixture_root: Path = DEFAULT_FIXTURE_ROOT,
    apply_defect: bool = True,
) -> Path:
    """A hermetic standalone trial repo of `defect`'s corpus repo at its pinned SHA,
    defect applied and committed, prompted for one trial.

    DEVIATION FROM SPEC §5 ("a fresh git worktree per trial"), and why. A worktree
    shares the corpus clone's object store, so the pre-defect blobs stay reachable
    -- `git diff <sha> HEAD`, `git log --all`, or a bare `git diff` against the
    unstaged patch all hand the bash/control arms the exact file, symbol and line
    of the defect for free, voiding the arm while still producing a plausible
    number (C1). §5 itself calls the branch name "a bonus, not the mechanism" and
    the *isolation* the mechanism: a standalone repo delivers that isolation and
    the branch name both. So the tree is built by exporting the pinned SHA's tree
    (`git archive | tar -x`), applying the defect, then `git init` + one commit
    naming only the repo and SHA. Net effect: `git status` is clean, `git log` has
    exactly one commit, there is no parent to diff, and the defect is
    indistinguishable inside a whole-tree initial add.

    Pinned to the SHA in `corpus_root/manifest.json`, never to the corpus repo's
    current HEAD -- the corpus repo is a shared clone that other trials (and a
    future `vendor.sh` re-run) may advance, and a tree that silently followed HEAD
    would make every trial after the first measure a different repo state than its
    pre-registered defect. `corpus_root` is a parameter rather than the real
    `corpus/` so this is testable against a throwaway repo: the fast suite must not
    depend on the vendored corpus existing.

    `apply_defect=False` provisions the clean pinned tree (C7 asserts the oracle is
    GREEN there before proving the defect turns it RED).
    """
    manifest = json.loads((corpus_root / "manifest.json").read_text(encoding="utf-8"))
    sha = manifest[defect.repo]["sha"]
    repo_path = corpus_root / defect.repo
    branch = branch_name(defect, arm, trial)

    dest.mkdir(parents=True, exist_ok=True)
    # Export the pinned tree into `dest` as plain files: the archive carries no
    # history and no object store, so nothing pre-defect is reachable afterward.
    archive = subprocess.run(
        ["git", "-C", str(repo_path), "archive", sha],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout,
        check=True,
        capture_output=True,
    )

    fixture_dir = _find_fixture_dir(fixture_root, defect)
    if apply_defect:
        patch_path = (fixture_dir / "defect.patch").resolve()
        subprocess.run(
            ["git", "apply", str(patch_path)],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
        )

    prompt_text = (fixture_dir / "prompt.md").read_text(encoding="utf-8")
    (dest / "PROMPT.md").write_text(prompt_text, encoding="utf-8")

    # A fresh repo whose single commit IS the defect state. Identity is set on the
    # commit invocation so provisioning needs no global git config. PROMPT.md is
    # deliberately committed too, so `git status` stays clean rather than showing
    # an untracked file that would itself invite `git status` (C1 in miniature).
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=dest, check=True, capture_output=True, text=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=probe@toolbench.local",
            "-c",
            "user.name=toolbench-probe",
            "commit",
            "-q",
            "-m",
            f"{defect.repo} @ {sha}",
        ],
        cwd=dest,
        check=True,
        capture_output=True,
        text=True,
    )
    return dest
