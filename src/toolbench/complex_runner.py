"""Drive one trial: worktree -> headless claude -> oracle -> scored result.

`launch` and `oracle` are injected so the suite never shells out to a real
`claude` binary (the project's fake-runner pattern, S24).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from toolbench.complex import (
    BANNED_TOOLS,
    DEFAULT_FIXTURE_ROOT,
    MANIFEST_PATH,
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


class UnsafeDepsCache(RuntimeError):
    """Raised when the dependency cache cannot be trusted for this corpus.

    Two ways it goes wrong, both re-opening the C1/F1 leak or worse:

    1. The cache and `corpus_root` share a walkable ancestor other than `/`.
       `..` from a trial's `web/node_modules` symlink target then climbs to
       that ancestor and descends into pristine source. The module note below
       argues this invariant; this exception enforces it against the REAL
       corpus at runtime, because a comment cannot fail a run.
    2. The cache is not private to this user. Everything under it is symlinked
       into every trial tree and then EXECUTED by the oracles, so a cache dir
       a second uid can write is arbitrary code execution, not merely a leak.

    Refusing is always correct here: proceeding yields a scored, plausible
    result produced by a compromised trial, which is worse than no result.
    Pass an explicit `deps_base` that diverges from the corpus to recover.
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
    # workdir is the trial tree's root (dest): the read-scope audit voids any read
    # resolved outside it. Passed explicitly so enforcement does not depend on
    # parser internals.
    return score_trial(session_path, defect, arm, trial, oracle(workdir), trial_root=workdir)


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


# A tree exported from a pinned SHA carries only tracked files -- no node_modules,
# no venv -- so 6 of 8 oracles (`npx vitest run …`) and rich's pytest cannot run
# in it. Dependencies are provisioned ONCE into a shared cache and symlinked into
# each trial tree. The cache MUST live outside the tool-benchmarks tree entirely:
# an agent that follows `web/node_modules` and walks up with `..` reaches only the
# cache's own ancestors, and none of them may hold a pristine corpus clone. A
# cache nested under `corpus_root` (`corpus/.deps/<repo>`) failed this -- `..` from
# it reaches `corpus/` and its sibling clone `corpus/<repo>` = unpatched source, the
# identical leak as C1 in a new costume. Even a repo-root sibling like `.trial-deps/`
# fails, because the repo root has `corpus/` as a child.
#
# But *location outside the tree* is not enough, and `~/.cache/toolbench/deps` (the
# prior fix) still leaked: any two paths on one filesystem share a common ancestor,
# and a cache under `$HOME` shares `$HOME` with a corpus checked out under `$HOME`
# (tool-benchmarks lives under `~`). `$HOME` is walkable, so `..` from the cache
# reaches `~` and back down into `corpus/`. The only safe placement is one where the
# cache and the corpus checkout DIVERGE AT THE FILESYSTEM ROOT -- their sole common
# ancestor is `/`. `tempfile.gettempdir()` (honoring `$TMPDIR`; macOS
# `/var/folders/...`, else `/tmp`) is such a location relative to a `$HOME` checkout,
# and the leaf name is deliberately non-suggestive -- it embeds neither "toolbench"
# nor "corpus", so a committed symlink target reveals no breadcrumb toward the
# corpus. The base stays a parameter so a caller (or a test) can override it.
#
# Two things the argument above does NOT establish, both enforced by
# `_assert_deps_base_safe` on every run that links a dep (see UnsafeDepsCache):
#   - Divergence is a property of the cache AND the corpus, so it cannot be
#     settled by choosing a good default. `$TMPDIR` under `$HOME`, or a corpus
#     checked out under the temp root (Linux CI), puts them back under one
#     walkable ancestor. Only a check against the real `corpus_root` sees this.
#   - On Linux `gettempdir()` is the shared, world-writable `/tmp`, so the leaf
#     is a path any uid can pre-create and own. The cache is symlinked into
#     every trial tree and executed by the oracles, so a foreign cache is code
#     execution. The leaf is therefore per-uid and must be private.
_DEPS_CACHE_DIRNAME = "vendor-cache"
_MANIFEST_SHA_STAMP = ".manifest-sha"


def _default_deps_base() -> Path:
    return Path(tempfile.gettempdir()) / f"{_DEPS_CACHE_DIRNAME}-{os.getuid()}"


def _mkdir_private(path: Path) -> None:
    """Create `path` and any missing ancestors private to this user, atomically.

    `mkdir()`'s default mode is `0o777 & ~umask`, so creating first and chmodding to
    `0o700` after leaves a window in which a permissive umask makes the cache
    world-writable -- and `parents=True` leaves the INTERMEDIATES permissive for
    good, since the chmod only ever reaches the leaf. A world-writable parent alone
    is fatal: another uid can swap our private leaf out from under us, defeating the
    ownership check rather than tripping it.

    `mode=` closes the window because umask can only CLEAR permission bits, never
    set them: `0o700` can never widen, whatever the umask. Every directory is
    private from the instant it exists.
    """
    for ancestor in reversed(path.parents):
        if not ancestor.exists():
            ancestor.mkdir(mode=0o700)
    path.mkdir(mode=0o700, exist_ok=True)


def _assert_deps_base_safe(deps_base: Path, corpus_root: Path) -> None:
    """Refuse a cache that leaks corpus source or that this user does not own.

    Called before any dep is built or symlinked. Rejects the literal cache leaf
    before resolving either path, including dangling symlinks. Resolution still
    catches a symlinked ancestor pointing back under the corpus as the real path
    it is rather than the path it advertises.
    """
    if deps_base.is_symlink():
        raise UnsafeDepsCache(
            f"dependency cache {deps_base.absolute()} is a symlink: refusing to "
            "follow a replaceable cache base for dependency writes or trial reads"
        )
    cache = deps_base.resolve()
    corpus = corpus_root.resolve()
    anchor = Path(cache.anchor)

    # The invariant: `/` is the ONLY ancestor the two may share. Walking the
    # cache's own ancestry (not the corpus's) is what catches a cache nested
    # under the corpus as well as the two merely sharing `$HOME` or `/tmp`.
    for ancestor in (cache, *cache.parents):
        if ancestor == anchor:
            continue
        if os.path.commonpath([ancestor, corpus]) == str(ancestor):
            raise UnsafeDepsCache(
                f"dependency cache {cache} and corpus {corpus} share the walkable "
                f"ancestor {ancestor}: `..` from a trial's dep symlink reaches it and "
                f"descends into pristine source. They may share only {anchor}. "
                f"Set $TMPDIR (or pass deps_base=) to a location that diverges from "
                f"the corpus checkout at the filesystem root."
            )

    # A private leaf under a parent others can write is still swappable: they cannot
    # read into it, but they can rename it away and leave their own in its place
    # after the checks below pass -- defeating the ownership check rather than
    # tripping it. Two conditions make a writable ancestor tolerable, and BOTH are
    # needed:
    #   - sticky: in a sticky dir, only an entry's owner may rename or delete it.
    #   - owned by us or root: sticky exempts the DIRECTORY'S owner too, so a sticky
    #     dir owned by an attacker can still be swapped.
    # Together these admit exactly the real defaults -- Linux's root-owned 1777 /tmp
    # and macOS's per-user 0700 /var/folders/... -- and refuse the rest.
    for ancestor in cache.parents:
        if not ancestor.exists():
            continue  # created privately by _mkdir_private below
        st_ancestor = ancestor.stat()
        if not st_ancestor.st_mode & 0o022:
            continue
        if not st_ancestor.st_mode & stat.S_ISVTX:
            raise UnsafeDepsCache(
                f"dependency cache ancestor {ancestor} is writable by other users "
                f"and not sticky (mode {st_ancestor.st_mode & 0o7777:04o}): another "
                f"uid could replace the cache directory after it is validated. "
                f"Place the cache under a private or sticky parent."
            )
        if st_ancestor.st_uid not in (os.getuid(), 0):
            raise UnsafeDepsCache(
                f"dependency cache ancestor {ancestor} is world-writable and sticky "
                f"but owned by uid {st_ancestor.st_uid}: sticky still lets the "
                f"directory's OWNER rename entries, so that uid could replace the "
                f"cache after it is validated."
            )

    if not cache.exists():
        try:
            _mkdir_private(deps_base)
        except FileExistsError as exc:
            raise UnsafeDepsCache(
                f"dependency cache {deps_base.absolute()} changed while it was "
                "being validated; refusing to follow it"
            ) from exc

    # Validate whatever is now there -- created or pre-existing alike. Checking the
    # dir we just made is not redundant: it is what catches a hostile umask, or
    # another uid winning a race to create the path first.
    try:
        st = deps_base.lstat()
    except FileNotFoundError as exc:
        raise UnsafeDepsCache(
            f"dependency cache {deps_base.absolute()} disappeared while it was "
            "being validated"
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeDepsCache(
            f"dependency cache {deps_base.absolute()} became a symlink while it "
            "was being validated"
        )
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeDepsCache(
            f"dependency cache {deps_base.absolute()} is not a directory"
        )
    if st.st_uid != os.getuid():
        raise UnsafeDepsCache(
            f"dependency cache {cache} is owned by uid {st.st_uid}, not by this "
            f"user (uid {os.getuid()}). Its contents are symlinked into every "
            f"trial tree and executed by the oracles; refusing to trust it."
        )
    if st.st_mode & 0o077:
        raise UnsafeDepsCache(
            f"dependency cache {cache} is group/world accessible "
            f"(mode {st.st_mode & 0o777:03o}); another uid could plant a "
            f"node_modules or venv that the oracles then execute. Expected 700."
        )


def _deps_root(deps_base: Path, repo: str) -> Path:
    return deps_base / repo


def _git_show_text(repo_path: Path, sha: str, rel_path: str) -> str:
    """Read `rel_path` at `sha` from `repo_path` without mutating its checkout."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "show", f"{sha}:{rel_path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _extract_pinned_tree(repo_path: Path, sha: str, dest: Path) -> None:
    """Export the tree at `sha` into `dest` (plain files, no history)."""
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


def _load_manifest(manifest_path: Path | None) -> dict[str, Any]:
    """Read the packaged manifest unless a custom corpus opts into its own."""
    path = MANIFEST_PATH if manifest_path is None else manifest_path
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _read_deps_stamp(deps_root: Path) -> str | None:
    stamp = deps_root / _MANIFEST_SHA_STAMP
    if not stamp.is_file():
        return None
    return stamp.read_text(encoding="utf-8").strip()


def _write_deps_stamp(deps_root: Path, sha: str) -> None:
    deps_root.mkdir(parents=True, exist_ok=True)
    (deps_root / _MANIFEST_SHA_STAMP).write_text(f"{sha}\n", encoding="utf-8")


def _invalidate_deps_if_stale(
    deps_root: Path, sha: str, entry: dict[str, Any]
) -> None:
    """Drop cached dep trees when the manifest SHA drifts or the stamp is absent.

    PR #99 pinned manifest reads to the packaged SHA, but a plain
    ``target.exists()`` skip left ``node_modules`` built for an older SHA in
    place after a pull bumps the manifest -- trials then archive the new SHA
    while oracles execute against stale dependencies.
    """
    if _read_deps_stamp(deps_root) == sha:
        return
    for dep in entry.get("deps", []):
        target = deps_root / dep["path"]
        if not target.exists():
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    stamp = deps_root / _MANIFEST_SHA_STAMP
    if stamp.exists():
        stamp.unlink()


def ensure_deps(
    corpus_root: Path,
    repo: str,
    deps_base: Path | None = None,
    manifest_path: Path | None = None,
) -> None:
    """Build `repo`'s dependency cache under `<deps_base>/<repo>/` if absent.

    The cache lives outside `corpus_root` (see the module note above): its default
    is under `tempfile.gettempdir()`, which diverges from a `$HOME` corpus checkout
    at the filesystem root, so no cache ancestor is walkable back to corpus source.

    `manifest_path` defaults to the packaged source of truth. Custom corpora must
    pass their own manifest explicitly; a generated `corpus/manifest.json` is
    never selected implicitly because it can survive a pull and silently pin
    trials to obsolete SHAs.

    Idempotent for a fixed manifest SHA: a dep whose target path already exists
    and matches the stamped SHA is left alone, so this is cheap to call before
    every trial. When the packaged SHA changes, cached dep trees are rebuilt.
    All work here happens OUTSIDE the measured window -- it must never appear in
    an agent transcript.

    npm deps are built from a COPY of the package manifests in a source-free cache
    dir (so `npm ci`'s `node_modules/..` cannot reach corpus source); the rich venv
    installs pytest and rich's runtime deps but NOT rich itself, so pytest imports
    the trial tree's own `rich/` (an editable `-e .` install would instead pin
    imports to whichever tree pip ran in, hiding the defect).
    """
    deps_base = deps_base or _default_deps_base()
    manifest = _load_manifest(manifest_path)
    entry = manifest[repo]
    sha = str(entry["sha"])
    repo_src = corpus_root / repo
    deps_root = _deps_root(deps_base, repo)

    # Only repos that declare deps get a symlink into the cache, so only they can
    # leak through it -- and only they need a cache worth trusting.
    if entry.get("deps"):
        _assert_deps_base_safe(deps_base, corpus_root)
        _invalidate_deps_if_stale(deps_root, sha, entry)

    for dep in entry.get("deps", []):
        target = deps_root / dep["path"]
        if target.exists():
            continue
        if "npm_ci" in dep:
            subdir = dep["npm_ci"]
            cache_subdir = deps_root / subdir
            cache_subdir.mkdir(parents=True, exist_ok=True)
            prefix = f"{subdir}/" if subdir else ""
            for name in ("package.json", "package-lock.json"):
                rel = f"{prefix}{name}"
                (cache_subdir / name).write_text(
                    _git_show_text(repo_src, sha, rel),
                    encoding="utf-8",
                )
            subprocess.run(
                ["npm", "ci", "--no-audit", "--no-fund"],
                cwd=cache_subdir,
                check=True,
                capture_output=True,
                text=True,
            )
        elif "venv" in dep:
            venv_dir = deps_root / dep["path"]
            venv_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            pip = venv_dir / "bin" / "pip"
            subprocess.run(
                [str(pip), "install", "--quiet", "--upgrade", "pip"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [str(pip), "install", "--quiet", *dep["venv"]],
                check=True,
                capture_output=True,
                text=True,
            )
        else:  # pragma: no cover - guards against a malformed manifest dep
            raise ValueError(f"{repo}: dep {dep!r} declares no known build kind")

    if entry.get("deps"):
        _write_deps_stamp(deps_root, sha)

    warmup = entry.get("warmup", [])
    if warmup:
        # Warm-up populates a GLOBAL cache (e.g. ~/.cargo) rather than a tree path,
        # so the rust oracle can build in the trial tree without a network fetch
        # inside the trial. It has no symlink; it is pure environment setup.
        with tempfile.TemporaryDirectory() as tmp:
            pinned_root = Path(tmp)
            _extract_pinned_tree(repo_src, sha, pinned_root)
            for step in warmup:
                subprocess.run(
                    list(step),
                    cwd=pinned_root,
                    check=True,
                    capture_output=True,
                    text=True,
                )


def _link_deps(entry: dict[str, object], deps_base: Path, repo: str, dest: Path) -> None:
    """Symlink each of `repo`'s cached deps into the trial tree at its own path.

    Raises if a declared dep is missing from the cache rather than leaving a
    dangling link: a silently-absent `node_modules` would make the oracle fail as
    if the fix were wrong, which is the C7 failure this whole path exists to
    prevent. Call `ensure_deps` first.
    """
    deps_root = _deps_root(deps_base, repo)
    deps = entry.get("deps", [])
    assert isinstance(deps, list)
    for dep in deps:
        rel = dep["path"]
        src = deps_root / rel
        if not src.exists():
            raise FileNotFoundError(
                f"{repo}: dependency cache {src} is missing -- run ensure_deps "
                f"before provisioning a trial tree that needs it"
            )
        link = dest / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(src)


def provision_worktree(
    defect: DefectSpec,
    arm: ArmSpec,
    trial: int,
    corpus_root: Path,
    dest: Path,
    fixture_root: Path = DEFAULT_FIXTURE_ROOT,
    apply_defect: bool = True,
    deps_base: Path | None = None,
    manifest_path: Path | None = None,
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

    Pinned to the SHA in the packaged manifest by default, never to the corpus
    repo's current HEAD or its generated manifest copy -- the corpus repo is a
    shared clone that other trials (and a future `vendor.sh` re-run) may advance,
    while a generated manifest can remain stale across a pull. Either source
    silently drifting would make trials measure a different repo state than their
    pre-registered defects. `corpus_root`, `fixture_root`, and `manifest_path` are
    parameters so this is testable against a throwaway corpus: the fast suite must
    not depend on the vendored corpus existing.

    `apply_defect=False` provisions the clean pinned tree (C7 asserts the oracle is
    GREEN there before proving the defect turns it RED).
    """
    deps_base = deps_base or _default_deps_base()
    manifest = _load_manifest(manifest_path)
    sha = manifest[defect.repo]["sha"]
    repo_path = corpus_root / defect.repo
    branch = branch_name(defect, arm, trial)

    # Before exporting anything: this tree is about to receive symlinks INTO the
    # cache (`_link_deps` below), so an unsafe cache must abort the trial rather
    # than be discovered after a tree exists to leak through.
    if manifest[defect.repo].get("deps"):
        _assert_deps_base_safe(deps_base, corpus_root)

    # Refuse a dest that already holds files (a prior failed/partial trial): with
    # `exist_ok=True` those stale files would be swept into `git add -A` and into
    # the "hermetic" initial commit. An absent or empty dest is fine.
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(
            f"{dest} is not empty: refusing to provision over a non-empty tree "
            "(a stale trial dir would leak files into the hermetic commit). "
            "Remove it first."
        )
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

    # Symlinked before the commit so the links are part of the single initial add
    # and `git status` stays clean. The targets live in the out-of-tree dep cache
    # (`deps_base`), whose ancestry holds no corpus source -- committing the link
    # exposes nothing, and following it upward reaches only other dependencies.
    _link_deps(manifest[defect.repo], deps_base, defect.repo, dest)

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
