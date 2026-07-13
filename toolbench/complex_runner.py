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
    """Run one cell and score it. The prompt is the defect's bug report."""
    prompt_path = workdir / "PROMPT.md"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else defect.rationale
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
) -> Path:
    """Fresh worktree of `defect`'s corpus repo, patched and prompted for one trial.

    Pinned to the SHA in `corpus_root/manifest.json`, never to the corpus repo's
    current HEAD -- the corpus repo is a shared clone that other trials (and a
    future `vendor.sh` re-run) may advance, and a worktree that silently followed
    HEAD would make every trial after the first one measure a different repo state
    than its pre-registered defect. `corpus_root` is a parameter rather than the
    real `corpus/` so this is testable against a throwaway repo: the fast suite
    must not depend on the vendored corpus existing.
    """
    manifest = json.loads((corpus_root / "manifest.json").read_text(encoding="utf-8"))
    sha = manifest[defect.repo]["sha"]
    repo_path = corpus_root / defect.repo
    branch = branch_name(defect, arm, trial)
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(dest), sha],
        check=True,
        capture_output=True,
        text=True,
    )

    fixture_dir = _find_fixture_dir(fixture_root, defect)
    patch_path = (fixture_dir / "defect.patch").resolve()
    subprocess.run(
        ["git", "-C", str(dest), "apply", str(patch_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    prompt_text = (fixture_dir / "prompt.md").read_text(encoding="utf-8")
    (dest / "PROMPT.md").write_text(prompt_text, encoding="utf-8")
    return dest
