import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from toolbench.complex import ArmSpec, DefectSpec, Truth, build_arms
from toolbench.complex_runner import (
    UnprovisionedWorktree,
    UnsafeDepsCache,
    _default_deps_base,
    branch_name,
    build_claude_argv,
    ensure_deps,
    provision_worktree,
    run_trial,
    shell_oracle,
)

FIXTURE = Path("tests/fixtures/complex_session_located.jsonl")
GATE = "Bash(npx vitest run:*)"

DEFECT = DefectSpec(
    id="DT",
    repo="wids",
    language="typescript",
    truth=Truth("web/src/lib/schedule.ts", "formatSlot", (12, 20)),
    predicted_winner="native",
    rationale="test fixture",
    oracle_cmd=("npx", "vitest", "run", "tests/schedule.test.ts"),
    oracle_cwd=".",
    test_gate=GATE,
)


def _arm(name: str) -> ArmSpec:
    return next(a for a in build_arms(GATE) if a.name == name)


class ClaudeArgvTests(unittest.TestCase):
    def test_allowed_tools_are_passed_and_agent_never_appears(self) -> None:
        argv = build_claude_argv("find the bug", _arm("serena"), Path("/tmp/wt"))
        self.assertIn("--allowedTools", argv)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertIn("mcp__plugin_serena_serena__find_symbol", allowed)
        self.assertNotIn("Task", allowed)
        self.assertNotIn("Agent", allowed)

    def test_the_ban_is_also_stated_explicitly(self) -> None:
        # --allowedTools alone is an allowlist; --disallowedTools states the ban so
        # a future permissive default cannot quietly reopen the subagent escape.
        argv = build_claude_argv("find the bug", _arm("control"), Path("/tmp/wt"))
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Task", argv[argv.index("--disallowedTools") + 1])


class RunTrialTests(unittest.TestCase):
    def test_oracle_verdict_flows_into_the_scored_result(self) -> None:
        launched: list[str] = []

        def fake_launch(argv: list[str], cwd: Path) -> Path:
            launched.append("launched")
            return FIXTURE

        def fake_oracle(cwd: Path) -> bool:
            return False  # suite still red

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "PROMPT.md").write_text("find the bug\n", encoding="utf-8")
            result = run_trial(DEFECT, _arm("native"), 1, workdir, fake_launch, fake_oracle)
        self.assertEqual(launched, ["launched"])
        self.assertFalse(result.fixed)
        self.assertTrue(result.located)


class RunTrialMissingPromptTests(unittest.TestCase):
    """A worktree with no PROMPT.md means `provision_worktree` never ran against
    it: the trial is broken, not merely under-prompted. `run_trial` must not
    silently substitute `defect.rationale` (the predicted-winner justification,
    e.g. "serena should win here because...") as a stand-in bug report -- that
    leaks the answer straight into the agent's input, the same defect class
    already fixed four times over in fixture prompts that leaked the answer to
    `rg`. It must fail loudly instead."""

    def test_missing_prompt_raises_instead_of_falling_back(self) -> None:
        calls: list[str] = []

        def fake_launch(argv: list[str], cwd: Path) -> Path:
            calls.append("launched")
            return FIXTURE

        def fake_oracle(cwd: Path) -> bool:
            return False

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)  # PROMPT.md deliberately absent
            with self.assertRaises(UnprovisionedWorktree):
                run_trial(DEFECT, _arm("native"), 1, workdir, fake_launch, fake_oracle)

        self.assertEqual(calls, [], "launch must not run against an unprovisioned worktree")

    def test_rationale_never_reaches_the_launched_prompt(self) -> None:
        captured: list[str] = []

        def fake_launch(argv: list[str], cwd: Path) -> Path:
            captured.append(argv[argv.index("-p") + 1])
            return FIXTURE

        def fake_oracle(cwd: Path) -> bool:
            return False

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)  # PROMPT.md deliberately absent
            try:
                run_trial(DEFECT, _arm("native"), 1, workdir, fake_launch, fake_oracle)
            except UnprovisionedWorktree:
                pass

        self.assertTrue(
            all(DEFECT.rationale not in prompt for prompt in captured),
            "defect.rationale (the predicted-winner justification) must never "
            "leak into the prompt handed to the agent under test",
        )


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


class ProvisionWorktreeTests(unittest.TestCase):
    """The fast suite must not depend on `corpus/` existing: a tiny throwaway
    git repo, built fresh per test, stands in for a vendored corpus repo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.corpus_root = self.root / "corpus"
        self.repo_dir = self.corpus_root / "toy"
        self.repo_dir.mkdir(parents=True)
        _run(["git", "init", "-q"], self.repo_dir)
        _run(["git", "config", "user.email", "test@example.com"], self.repo_dir)
        _run(["git", "config", "user.name", "Test"], self.repo_dir)
        (self.repo_dir / "a.txt").write_text("original\n", encoding="utf-8")
        _run(["git", "add", "a.txt"], self.repo_dir)
        _run(["git", "commit", "-q", "-m", "init"], self.repo_dir)
        self.sha = _run(
            ["git", "rev-parse", "HEAD"], self.repo_dir
        ).stdout.strip()

        # A real patch, produced by git itself, so the fixture cannot be a typo'd
        # hand-written diff that happens to look right.
        (self.repo_dir / "a.txt").write_text("modified\n", encoding="utf-8")
        patch_text = subprocess.run(
            ["git", "diff"], cwd=self.repo_dir, check=True, capture_output=True, text=True
        ).stdout
        _run(["git", "checkout", "-q", "--", "a.txt"], self.repo_dir)

        (self.corpus_root / "manifest.json").write_text(
            json.dumps({"toy": {"sha": self.sha}}), encoding="utf-8"
        )

        self.fixture_root = self.root / "probes" / "complex"
        fixture_dir = self.fixture_root / "toy-D1-slug"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "defect.patch").write_text(patch_text, encoding="utf-8")
        (fixture_dir / "prompt.md").write_text("find the toy bug\n", encoding="utf-8")

        self.defect = DefectSpec(
            id="D1",
            repo="toy",
            language="typescript",
            truth=Truth("a.txt", "a", (1, 1)),
            predicted_winner="neutral",
            rationale="toy fixture",
            oracle_cmd=("true",),
            oracle_cwd=".",
            test_gate="Bash(true:*)",
        )

    def test_creates_worktree_applies_patch_and_writes_prompt(self) -> None:
        dest = self.root / "wt"
        arm = _arm("native")
        result = provision_worktree(
            self.defect, arm, 1, self.corpus_root, dest, fixture_root=self.fixture_root
        )
        self.assertEqual(result, dest)
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "modified\n")
        self.assertEqual((dest / "PROMPT.md").read_text(encoding="utf-8"), "find the toy bug\n")

    def test_branch_is_named_probe_repo_defect_arm_trial(self) -> None:
        dest = self.root / "wt2"
        arm = _arm("serena")
        provision_worktree(
            self.defect, arm, 3, self.corpus_root, dest, fixture_root=self.fixture_root
        )
        current = _run(["git", "branch", "--show-current"], dest).stdout.strip()
        self.assertEqual(current, "probe/toy/D1/serena/t3")
        self.assertEqual(branch_name(self.defect, arm, 3), "probe/toy/D1/serena/t3")

    def test_the_trial_tree_is_a_standalone_repo_committed_clean(self) -> None:
        # C1: a `git worktree add` + unstaged `git apply` hands the bash/control
        # arms the defect for free -- `git diff` prints file, symbol and line. The
        # tree must instead be a standalone repo with the defect COMMITTED, so
        # `git status` is clean and there is nothing to diff against.
        dest = self.root / "wt_hermetic"
        provision_worktree(
            self.defect, _arm("bash"), 1, self.corpus_root, dest, fixture_root=self.fixture_root
        )
        status = _run(["git", "status", "--porcelain"], dest).stdout
        self.assertEqual(status, "", "a dirty tree leaks the defect via git diff/status")
        log = _run(["git", "log", "--oneline"], dest).stdout.strip().splitlines()
        self.assertEqual(len(log), 1, "the trial tree must have exactly one commit")

    def test_git_diff_reveals_nothing_because_the_defect_is_committed(self) -> None:
        # The direct C1 reproduction: with the old worktree+apply path this diff
        # printed the seeded change verbatim.
        dest = self.root / "wt_nodiff"
        provision_worktree(
            self.defect, _arm("bash"), 1, self.corpus_root, dest, fixture_root=self.fixture_root
        )
        self.assertEqual(_run(["git", "diff"], dest).stdout, "")
        self.assertEqual(_run(["git", "diff", "HEAD"], dest).stdout, "")
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "modified\n")

    def test_the_corpus_object_store_is_not_shared_so_no_pristine_blob_is_reachable(
        self,
    ) -> None:
        # A worktree shares the corpus clone's object store, so the pre-defect
        # blobs stay reachable (git log --all, git diff <sha> HEAD). A standalone
        # repo does not: the pinned sha is not even an object here.
        dest = self.root / "wt_noshare"
        provision_worktree(
            self.defect, _arm("bash"), 1, self.corpus_root, dest, fixture_root=self.fixture_root
        )
        proc = subprocess.run(
            ["git", "cat-file", "-e", self.sha], cwd=dest, capture_output=True
        )
        self.assertNotEqual(
            proc.returncode, 0, "the pinned sha must not resolve in the trial repo"
        )
        all_log = _run(["git", "log", "--all", "--oneline"], dest).stdout.strip().splitlines()
        self.assertEqual(len(all_log), 1)

    def test_apply_defect_false_provisions_a_clean_committed_tree(self) -> None:
        # C7 needs to provision a clean tree to assert the oracle is GREEN before
        # the defect is applied.
        dest = self.root / "wt_clean"
        provision_worktree(
            self.defect,
            _arm("bash"),
            1,
            self.corpus_root,
            dest,
            fixture_root=self.fixture_root,
            apply_defect=False,
        )
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "original\n")
        self.assertEqual(_run(["git", "status", "--porcelain"], dest).stdout, "")

    def test_a_non_empty_dest_is_refused_not_provisioned_over(self) -> None:
        # F2: `dest.mkdir(exist_ok=True)` + extract meant a dest still holding
        # files from a prior failed/partial trial got provisioned OVER -- the stale
        # files swept into `git add -A` and into the "hermetic" initial commit. The
        # old `git worktree add` refused a non-empty dest; provisioning must too.
        dest = self.root / "wt_dirty"
        dest.mkdir(parents=True)
        (dest / "stale.txt").write_text("left over from a failed trial\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            provision_worktree(
                self.defect, _arm("bash"), 1, self.corpus_root, dest,
                fixture_root=self.fixture_root,
            )

    def test_worktree_is_pinned_to_the_manifest_sha_not_whatever_head_is(self) -> None:
        # Advance the source repo past the pinned sha; the worktree must still
        # land on the pinned commit, not on whatever the corpus repo's HEAD is.
        (self.repo_dir / "b.txt").write_text("later\n", encoding="utf-8")
        _run(["git", "add", "b.txt"], self.repo_dir)
        _run(["git", "commit", "-q", "-m", "later commit"], self.repo_dir)

        dest = self.root / "wt3"
        provision_worktree(
            self.defect, _arm("bash"), 1, self.corpus_root, dest, fixture_root=self.fixture_root
        )
        self.assertFalse((dest / "b.txt").exists())


class DepsCacheAncestryTests(unittest.TestCase):
    """F1 -- the real guard, not a proxy. The dependency cache is symlinked into
    each trial tree (`web/node_modules -> <cache>/web/node_modules`). Whatever the
    symlink resolves to, none of its realpath ancestors may be a pristine corpus
    clone: otherwise a bash/control arm can `cd node_modules && cd ../../..` back
    into unpatched source. `corpus/.deps/<repo>` failed exactly this -- corpus_root
    is its ancestor and `corpus_root/<repo>` is a live clone. The fix moves the
    cache outside corpus_root entirely (default under `tempfile.gettempdir()`).

    Still necessary but NOT sufficient -- see DepsCacheRootDivergenceTests for the
    filesystem-root-divergence invariant that the `~/.cache` default failed.
    """

    def setUp(self) -> None:
        # Model the real deployment, and satisfy the runtime guard: the corpus lives
        # under $HOME and the cache under the temp root, so the two diverge at `/`.
        # A single throwaway root for both (the previous fixture) is itself the leak
        # -- `..` from the cache reaches that root and descends into corpus/toy --
        # and `_assert_deps_base_safe` now refuses it, correctly.
        self._tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.corpus_root = self.root / "corpus"
        self.repo_dir = self.corpus_root / "toy"
        (self.repo_dir / "web").mkdir(parents=True)
        _run(["git", "init", "-q"], self.repo_dir)
        _run(["git", "config", "user.email", "test@example.com"], self.repo_dir)
        _run(["git", "config", "user.name", "Test"], self.repo_dir)
        (self.repo_dir / "web" / "app.js").write_text("x\n", encoding="utf-8")
        _run(["git", "add", "-A"], self.repo_dir)
        _run(["git", "commit", "-q", "-m", "init"], self.repo_dir)
        self.sha = _run(["git", "rev-parse", "HEAD"], self.repo_dir).stdout.strip()

        (self.corpus_root / "manifest.json").write_text(
            json.dumps(
                {
                    "toy": {
                        "sha": self.sha,
                        "deps": [{"path": "web/node_modules", "npm_ci": "web"}],
                    }
                }
            ),
            encoding="utf-8",
        )

        self.fixture_root = self.root / "probes" / "complex"
        fixture_dir = self.fixture_root / "toy-D1-slug"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "prompt.md").write_text("find the toy bug\n", encoding="utf-8")

        self.defect = DefectSpec(
            id="D1",
            repo="toy",
            language="typescript",
            truth=Truth("web/app.js", "a", (1, 1)),
            predicted_winner="neutral",
            rationale="toy fixture",
            oracle_cmd=("true",),
            oracle_cwd=".",
            test_gate="Bash(true:*)",
        )

        # Point the default dep-cache base at a throwaway dir UNDER THE TEMP ROOT --
        # divergent from the $HOME corpus above -- and pre-populate the cached target
        # there, so the symlink resolves and the test turns on ancestry, not on a
        # missing cache. `_link_deps` only symlinks an existing target; no npm ci
        # needed.
        self._cache_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache_tmp.cleanup)
        self.deps_base = Path(self._cache_tmp.name) / "cache"
        self._populate(self.deps_base / "toy" / "web" / "node_modules")
        patcher = mock.patch(
            "toolbench.complex_runner._default_deps_base",
            return_value=self.deps_base,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _populate(node_modules: Path) -> None:
        node_modules.mkdir(parents=True)
        (node_modules / "pkg.js").write_text("dep\n", encoding="utf-8")
        # The guard requires a private cache base; mkdir honors umask (0755), so set
        # it explicitly rather than depending on the runner's umask.
        node_modules.parents[2].chmod(0o700)

    def test_resolved_dep_symlink_has_no_corpus_clone_in_its_ancestry(self) -> None:
        dest = self.root / "wt"
        provision_worktree(
            self.defect,
            _arm("bash"),
            1,
            self.corpus_root,
            dest,
            fixture_root=self.fixture_root,
            apply_defect=False,
        )
        link = dest / "web" / "node_modules"
        self.assertTrue(link.is_symlink(), "dep must be symlinked into the trial tree")
        real = link.resolve()
        ancestry = [real, *real.parents]
        for forbidden in (self.corpus_root.resolve(), (self.corpus_root / "toy").resolve()):
            self.assertNotIn(
                forbidden,
                ancestry,
                f"dep symlink resolves under {forbidden}; `..` walks back to source",
            )


class DepsCacheRootDivergenceTests(unittest.TestCase):
    """F1 residual (codex re-review of the `~/.cache` fix) -- location alone is not
    sufficient. Any two paths on one filesystem share a common ancestor (at worst
    `/`); only when the dep cache and the corpus checkout diverge at the filesystem
    ROOT -- their sole common ancestor is `/` -- is there no walkable path from a
    cache ancestor back into pristine source. A `~/.cache` default shares `$HOME`
    with a corpus checked out under `$HOME`, and `$HOME` is walkable: the identical
    leak in a new costume. This encodes codex's invariant: for every ancestor of the
    resolved cache target other than `/`, that ancestor must NOT also be an ancestor
    of the corpus checkout.
    """

    def setUp(self) -> None:
        # Model the real deployment: tool-benchmarks (hence `corpus/`) lives under
        # $HOME. A throwaway dir under $HOME -- cleaned up -- reproduces the shared
        # `$HOME` ancestor that made the `~/.cache` default leaky, without touching
        # the real corpus or the real dep cache.
        self._tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self._tmp.cleanup)
        self.corpus_root = Path(self._tmp.name) / "tool-benchmarks" / "corpus"
        self.corpus_root.mkdir(parents=True)

    def test_default_cache_diverges_from_corpus_at_filesystem_root(self) -> None:
        # The resolved target a trial tree's `web/node_modules` symlink would point
        # at, using the real default base -- no override.
        cache_target = (
            _default_deps_base() / "toy" / "web" / "node_modules"
        ).resolve()
        corpus = self.corpus_root.resolve()
        root = Path(cache_target.anchor)
        for ancestor in cache_target.parents:
            if ancestor == root:
                continue
            self.assertNotEqual(
                os.path.commonpath([ancestor, corpus]),
                str(ancestor),
                f"cache ancestor {ancestor} is also an ancestor of the corpus "
                f"checkout {corpus}: `..` from the cache walks back to pristine "
                "source",
            )


# A repo that declares a dep is the only kind that gets a symlink into the cache,
# so it is the only kind the guard fires on.
_DEPS_MANIFEST = {
    "toy": {"sha": "0" * 40, "deps": [{"path": "web/node_modules", "npm_ci": "web"}]}
}


class DepsCacheRuntimeGuardTests(unittest.TestCase):
    """The root-divergence invariant, enforced at RUNTIME rather than asserted in a
    comment and a single test. `DepsCacheRootDivergenceTests` checks the default the
    *current* environment happens to produce; it cannot catch a `TMPDIR` pointing
    under `$HOME`, nor a corpus that itself lives under the temp root (a checkout
    under `/tmp` on Linux CI). Both re-open the C1/F1 leak: cache and corpus regain a
    walkable common ancestor, so `..` from a trial's `web/node_modules` symlink target
    reaches pristine source. The guard must run on the real `corpus_root`, on every
    run, and refuse rather than proceed.
    """

    def setUp(self) -> None:
        # Corpus under $HOME, the real deployment shape.
        self._home = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self._home.cleanup)
        self.corpus_root = Path(self._home.name) / "tool-benchmarks" / "corpus"
        self.corpus_root.mkdir(parents=True)
        (self.corpus_root / "manifest.json").write_text(
            json.dumps(_DEPS_MANIFEST), encoding="utf-8"
        )

    def test_ensure_deps_refuses_a_cache_sharing_a_walkable_ancestor_with_the_corpus(
        self,
    ) -> None:
        # TMPDIR under $HOME: cache and corpus share the throwaway $HOME dir, which
        # is walkable -- `..` from the cache reaches it and descends into corpus/toy.
        leaky_base = Path(self._home.name) / "tmp" / "vendor-cache"
        with self.assertRaises(UnsafeDepsCache):
            ensure_deps(self.corpus_root, "toy", deps_base=leaky_base)

    def test_ensure_deps_refuses_a_corpus_that_lives_under_the_cache_temp_root(self) -> None:
        # Linux CI: the checkout itself sits under /tmp, so the default cache and the
        # corpus share the temp root. The default base is fine in isolation -- it is
        # only unsafe *relative to this corpus*, which is why the check needs both.
        with tempfile.TemporaryDirectory() as tmp:
            corpus_in_tmp = Path(tmp) / "corpus"
            corpus_in_tmp.mkdir()
            (corpus_in_tmp / "manifest.json").write_text(
                json.dumps(_DEPS_MANIFEST), encoding="utf-8"
            )
            with self.assertRaises(UnsafeDepsCache):
                ensure_deps(corpus_in_tmp, "toy", deps_base=_default_deps_base())

    def test_provision_worktree_refuses_a_leaky_cache_base(self) -> None:
        # The symlink is written by provision_worktree, so the guard has to hold on
        # that path too -- not only on the ensure_deps build path.
        leaky_base = Path(self._home.name) / "tmp" / "vendor-cache"
        defect = DefectSpec(
            id="D1",
            repo="toy",
            language="typescript",
            truth=Truth("web/app.js", "a", (1, 1)),
            predicted_winner="neutral",
            rationale="toy fixture",
            oracle_cmd=("true",),
            oracle_cwd=".",
            test_gate="Bash(true:*)",
        )
        with self.assertRaises(UnsafeDepsCache):
            provision_worktree(
                defect,
                _arm("bash"),
                1,
                self.corpus_root,
                Path(self._home.name) / "wt",
                fixture_root=Path(self._home.name) / "probes" / "complex",
                deps_base=leaky_base,
            )

    def test_default_cache_base_is_private_to_the_current_user(self) -> None:
        # On Linux gettempdir() is the shared, world-writable /tmp, so an unqualified
        # `vendor-cache` leaf is a path another uid can pre-create and own. Everything
        # under it is symlinked into every trial tree and EXECUTED by the oracles
        # (`npx vitest run`, the venv's pytest), so a foreign cache is code execution.
        self.assertIn(str(os.getuid()), _default_deps_base().name)

    def test_ensure_deps_refuses_a_pre_existing_group_or_world_writable_cache(self) -> None:
        # `if target.exists(): continue` trusts whatever is already on disk. A cache
        # dir another user can write is a cache another user can poison.
        hostile = Path(tempfile.gettempdir()) / f"vendor-cache-hostile-{os.getpid()}"
        hostile.mkdir(mode=0o777)
        self.addCleanup(shutil.rmtree, hostile, True)
        os.chmod(hostile, 0o777)  # defeat umask
        with self.assertRaises(UnsafeDepsCache):
            ensure_deps(self.corpus_root, "toy", deps_base=hostile)


class ShellOracleTests(unittest.TestCase):
    def test_returns_true_iff_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "sub").mkdir()
            oracle_true = shell_oracle(["true"], ".")
            oracle_false = shell_oracle(["false"], ".")
            self.assertTrue(oracle_true(workdir))
            self.assertFalse(oracle_false(workdir))


if __name__ == "__main__":
    unittest.main()
