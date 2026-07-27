"""Evals for the linked-worktree reporter (`toolbench worktrees`).

Every case drives `classify()` through the `Runner` seam (S24) with git output
captured from this repository, so no test reads live worktree state: the
inventory a test sees is the one it scripted, at the instant it names via `now`.

The fixture below is the state at `2ff6ed2` -- the main checkout plus the three
foreign trees PR #88 deliberately left in place. It is the reference the
structure outline's four-row table records, and the idle ages and sizes asserted
here are the ones measured there (1 d / 15 d / 16 d, 112 / 103 / 103 MB).
"""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.fakes import FakeRunner, completed
from toolbench.worktrees import (
    UNKNOWN,
    Tree,
    WorktreeProbeFailed,
    _parse_upstreams,
    _parse_worktree_list,
    _render,
    classify,
    main,
)

REPO = Path("/Users/michellerojas/tool-benchmarks")
ADMIN = "/Users/michellerojas/tool-benchmarks/.git/worktrees"

# 2026-07-27T04:53:20Z -- the hour the outline's table was measured. Injected
# rather than read from the clock so the asserted ages below are facts about the
# fixture, not about the day the suite happens to run.
NOW = 1785128000.0

MAIN_SHA = "2ff6ed24362afde758db80a3193760cdcf719c6b"
HARBOR_SHA = "e092d9e73427b9504721c1747a2028ac9f30b507"
CURSOR_0Y57_SHA = "47e30c8991f9daed61d7dfd23a876c451fe4e59e"
CURSOR_MS0R_SHA = "ab841442432b715638eb5bb63b6edb786283abaa"

HARBOR = "/private/tmp/tool-benchmarks-harbor-wids-d2"
CURSOR_0Y57 = "/Users/michellerojas/.cursor/worktrees/tool-benchmarks/0y57"
CURSOR_MS0R = "/Users/michellerojas/.cursor/worktrees/tool-benchmarks/ms0r"

LISTING = f"""\
worktree {REPO}
HEAD {MAIN_SHA}
branch refs/heads/main

worktree {HARBOR}
HEAD {HARBOR_SHA}
branch refs/heads/codex-harbor-wids-d2-task

worktree {CURSOR_0Y57}
HEAD {CURSOR_0Y57_SHA}
branch refs/heads/fix/raw-discovery-project-attribution

worktree {CURSOR_MS0R}
HEAD {CURSOR_MS0R_SHA}
branch refs/heads/refactor/single-pass-probe-passive-split

"""


def _ref(branch: str, sha: str, upstream: str = "") -> str:
    """One `git for-each-ref --format=%(refname)%09%(objectname)%09%(upstream)` line.

    An empty third field is a branch with NO upstream. That is a different fact
    from `%(upstream:track)` being empty, which is also what an in-sync live
    upstream prints -- the ambiguity this module refuses to read.
    """
    return "\t".join((f"refs/heads/{branch}", sha, upstream))


REFS = (
    "\n".join(
        (
            _ref(
                "codex-harbor-wids-d2-task",
                HARBOR_SHA,
                "refs/remotes/origin/codex-harbor-wids-d2-task",
            ),
            _ref("feat/s41", "b7e044e99de30f62cd7cbc3888c7c2ee70a69ec1"),
            _ref(
                "fix/raw-discovery-project-attribution",
                CURSOR_0Y57_SHA,
                "refs/remotes/origin/fix/raw-discovery-project-attribution",
            ),
            _ref("main", MAIN_SHA, "refs/remotes/origin/main"),
            _ref(
                "refactor/single-pass-probe-passive-split",
                CURSOR_MS0R_SHA,
                "refs/remotes/origin/refactor/single-pass-probe-passive-split",
            ),
        )
    )
    + "\n"
)


def _probe(
    *,
    dirty: str = "",
    contains: str,
    gitdir: str,
    mtime: int,
    kilobytes: int,
) -> list[subprocess.CompletedProcess[str]]:
    """The five responses one clean linked tree consumes, in issue order:
    status, reachability, admin-dir lookup, gitdir mtime, size."""
    return [
        completed(stdout=dirty),
        completed(stdout=contains),
        completed(stdout=f"{gitdir}\n"),
        completed(stdout=f"{mtime}\n"),
        completed(stdout=f"{kilobytes}\t{gitdir}\n"),
    ]


def _todays_runner() -> FakeRunner:
    """The four-tree fixture: main plus the three trees left in place at `2ff6ed2`."""
    return FakeRunner(
        [
            completed(stdout=LISTING),
            completed(stdout=REFS),
            *_probe(
                contains="refs/remotes/origin/codex-harbor-wids-d2-task\n",
                gitdir=f"{ADMIN}/tool-benchmarks-harbor-wids-d2",
                mtime=1784961121,
                kilobytes=113928,
            ),
            *_probe(
                contains="refs/remotes/origin/fix/raw-discovery-project-attribution\n",
                gitdir=f"{ADMIN}/0y57",
                mtime=1783749011,
                kilobytes=105164,
            ),
            *_probe(
                contains="refs/remotes/origin/refactor/single-pass-probe-passive-split\n",
                gitdir=f"{ADMIN}/ms0r",
                mtime=1783741578,
                kilobytes=105452,
            ),
        ]
    )


def _one_tree(
    path: str,
    sha: str,
    *,
    branch: str | None = "feature",
    attributes: str = "",
) -> str:
    """A two-stanza listing: the main checkout, then one linked tree."""
    head = f"branch refs/heads/{branch}" if branch else "detached"
    return (
        f"worktree {REPO}\nHEAD {MAIN_SHA}\nbranch refs/heads/main\n\n"
        f"worktree {path}\nHEAD {sha}\n{head}\n{attributes}\n"
    )


class TodaysInventoryTests(unittest.TestCase):
    def test_the_main_checkout_is_excluded_and_the_three_foreign_trees_classify(
        self,
    ) -> None:
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        self.assertEqual(
            [str(t.path) for t in trees], [HARBOR, CURSOR_0Y57, CURSOR_MS0R]
        )
        self.assertNotIn(str(REPO), [str(t.path) for t in trees])

    def test_all_three_are_safe_because_their_work_lives_on_a_remote_ref(self) -> None:
        """SAFE here is a claim about the WORK, not about ownership: none of the
        three holds a commit that exists nowhere else. Whether the tree is
        somebody's live checkout is the CLAIMED verdict, which Phase 2 adds."""
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        self.assertEqual([t.verdict for t in trees], ["SAFE", "SAFE", "SAFE"])
        self.assertIn("refs/remotes/origin/codex-harbor-wids-d2-task", trees[0].reason)

    def test_idle_age_and_size_match_the_measured_table(self) -> None:
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        self.assertEqual([t.idle_days for t in trees], [1, 15, 16])
        self.assertEqual([t.megabytes for t in trees], [112, 103, 103])

    def test_branch_names_are_short_not_full_refs(self) -> None:
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        self.assertEqual(
            [t.branch for t in trees],
            [
                "codex-harbor-wids-d2-task",
                "fix/raw-discovery-project-attribution",
                "refactor/single-pass-probe-passive-split",
            ],
        )

    def test_no_unexpected_git_call_is_issued(self) -> None:
        """FakeRunner raises on exhaustion, so a classifier that grew a call would
        fail here rather than silently shell out during a report."""
        runner = _todays_runner()
        classify(runner, repo=REPO, now=NOW)
        self.assertEqual(len(runner.calls), 2 + 3 * 5)
        self.assertEqual(
            runner.calls[:2],
            [
                ["git", "-C", str(REPO), "worktree", "list", "--porcelain"],
                [
                    "git",
                    "-C",
                    str(REPO),
                    "for-each-ref",
                    "--format=%(refname)%09%(objectname)%09%(upstream)",
                    "refs/heads/",
                ],
            ],
        )
        self.assertEqual(
            runner.calls[2:7],
            [
                ["git", "-C", HARBOR, "status", "--porcelain"],
                [
                    "git",
                    "-C",
                    str(REPO),
                    "for-each-ref",
                    "--contains",
                    HARBOR_SHA,
                    "--format=%(refname)",
                    "refs/heads/main",
                    "refs/remotes/",
                ],
                ["git", "-C", HARBOR, "rev-parse", "--absolute-git-dir"],
                ["stat", "-f", "%m", f"{ADMIN}/tool-benchmarks-harbor-wids-d2/gitdir"],
                ["du", "-sk", HARBOR],
            ],
        )


class UpstreamAmbiguityTests(unittest.TestCase):
    """The trap: `%(upstream:track)` is empty for an in-sync live upstream AND for
    a branch that has no upstream at all. Reading it as a predicate is the same
    class of error as `clean_gone`'s `grep '\\[gone\\]'`."""

    def _two_branches(self) -> FakeRunner:
        listing = (
            f"worktree {REPO}\nHEAD {MAIN_SHA}\nbranch refs/heads/main\n\n"
            f"worktree /wt/live\nHEAD aaaa111\nbranch refs/heads/live-upstream\n\n"
            f"worktree /wt/orphan\nHEAD bbbb222\nbranch refs/heads/no-upstream\n\n"
        )
        refs = (
            _ref("live-upstream", "aaaa111", "refs/remotes/origin/live-upstream")
            + "\n"
            + _ref("no-upstream", "bbbb222")
            + "\n"
        )
        return FakeRunner(
            [
                completed(stdout=listing),
                completed(stdout=refs),
                *_probe(
                    contains="refs/remotes/origin/live-upstream\n",
                    gitdir=f"{ADMIN}/live",
                    mtime=int(NOW),
                    kilobytes=1024,
                ),
                *_probe(
                    contains="",
                    gitdir=f"{ADMIN}/orphan",
                    mtime=int(NOW),
                    kilobytes=1024,
                ),
            ]
        )

    def test_identical_empty_track_fields_land_on_different_verdicts(self) -> None:
        """Both branches print an empty `%(upstream:track)`. One is in sync with a
        live upstream, the other has no upstream whatsoever, and the classifier
        must not confuse them."""
        trees = classify(self._two_branches(), repo=REPO, now=NOW)
        self.assertEqual([t.verdict for t in trees], ["SAFE", "UNIQUE-WORK"])

    def test_the_classifier_never_asks_git_for_the_track_field(self) -> None:
        runner = self._two_branches()
        classify(runner, repo=REPO, now=NOW)
        self.assertNotIn(
            "upstream:track", " ".join(arg for call in runner.calls for arg in call)
        )

    def test_the_unique_work_reason_names_the_missing_upstream(self) -> None:
        trees = classify(self._two_branches(), repo=REPO, now=NOW)
        self.assertIn("no upstream recorded", trees[1].reason)
        self.assertIn("bbbb222", trees[1].reason)


class ReachabilityTests(unittest.TestCase):
    def _single(
        self,
        *,
        contains: str,
        upstream: str = "",
        branch: str | None = "feature",
    ) -> list[Tree]:
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/one", "c0ffee1", branch=branch)),
                completed(
                    stdout=_ref("feature", "c0ffee1", upstream) + "\n"
                    if branch
                    else ""
                ),
                *_probe(
                    contains=contains,
                    gitdir=f"{ADMIN}/one",
                    mtime=int(NOW) - 86400 * 3,
                    kilobytes=2048,
                ),
            ]
        )
        return classify(runner, repo=REPO, now=NOW)

    def test_a_gone_upstream_tree_reachable_from_main_is_safe(self) -> None:
        """The `tb-*` shape PR #88 removed: an upstream is recorded, its remote ref
        no longer exists, and the work is merged. Nothing contains the head except
        main, and that is enough."""
        trees = self._single(
            contains="refs/heads/main\n", upstream="refs/remotes/origin/feature"
        )
        self.assertEqual(trees[0].verdict, "SAFE")
        self.assertIn("refs/heads/main", trees[0].reason)

    def test_a_tree_with_no_upstream_reachable_from_main_is_safe(self) -> None:
        """The `worktree-agent-*` shape: no upstream at all, so it is not `[gone]`
        and no existing cleanup path would ever have considered it."""
        self.assertEqual(self._single(contains="refs/heads/main\n")[0].verdict, "SAFE")

    def test_an_unreachable_head_is_unique_work(self) -> None:
        trees = self._single(contains="")
        self.assertEqual(trees[0].verdict, "UNIQUE-WORK")

    def test_a_detached_head_has_no_branch_and_is_judged_on_reachability_alone(
        self,
    ) -> None:
        trees = self._single(contains="refs/heads/main\n", branch=None)
        self.assertIsNone(trees[0].branch)
        self.assertEqual(trees[0].verdict, "SAFE")

    def test_several_containing_refs_are_summarized_not_dropped(self) -> None:
        trees = self._single(
            contains="refs/heads/main\nrefs/remotes/origin/main\nrefs/remotes/upstream/main\n"
        )
        self.assertIn("refs/heads/main", trees[0].reason)
        self.assertIn("+2 more", trees[0].reason)


class NonCandidateTests(unittest.TestCase):
    def test_a_dirty_tree_is_reported_dirty_and_never_probed_for_reachability(
        self,
    ) -> None:
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/dirty", "d1d1d1d")),
                completed(stdout=_ref("feature", "d1d1d1d") + "\n"),
                completed(stdout=" M src/toolbench/report.py\n?? scratch.txt\n"),
                completed(stdout=f"{ADMIN}/dirty\n"),
                completed(stdout=f"{int(NOW)}\n"),
                completed(stdout="4096\t/wt/dirty\n"),
            ]
        )
        trees = classify(runner, repo=REPO, now=NOW)
        self.assertEqual(trees[0].verdict, "DIRTY")
        self.assertIn("2 modified or untracked entries", trees[0].reason)
        self.assertNotIn("--contains", [arg for call in runner.calls for arg in call])

    def test_untracked_files_alone_make_a_tree_dirty(self) -> None:
        """`git worktree remove` refuses on untracked files alone, so a verdict
        that ignored them would promise a removal git will decline."""
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/untracked", "0f0f0f0")),
                completed(stdout=_ref("feature", "0f0f0f0") + "\n"),
                completed(stdout="?? notes.md\n"),
                completed(stdout=f"{ADMIN}/untracked\n"),
                completed(stdout=f"{int(NOW)}\n"),
                completed(stdout="4096\t/wt/untracked\n"),
            ]
        )
        trees = classify(runner, repo=REPO, now=NOW)
        self.assertEqual(trees[0].verdict, "DIRTY")
        self.assertIn("1 modified or untracked entry", trees[0].reason)

    def test_a_locked_stanza_is_locked_without_a_status_or_reachability_call(
        self,
    ) -> None:
        """`locked` comes from `--porcelain`, never from parsing the human format."""
        runner = FakeRunner(
            [
                completed(
                    stdout=_one_tree(
                        "/wt/locked", "1ocked00", attributes="locked on a network share"
                    )
                ),
                completed(stdout=_ref("feature", "1ocked00") + "\n"),
                completed(stdout=f"{ADMIN}/locked\n"),
                completed(stdout=f"{int(NOW) - 86400 * 40}\n"),
                completed(stdout="4096\t/wt/locked\n"),
            ]
        )
        trees = classify(runner, repo=REPO, now=NOW)
        self.assertEqual(trees[0].verdict, "LOCKED")
        self.assertIn("on a network share", trees[0].reason)
        self.assertEqual(trees[0].idle_days, 40)
        self.assertNotIn("status", [arg for call in runner.calls for arg in call])

    def test_a_locked_stanza_without_a_reason_still_locks(self) -> None:
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/locked", "1ocked00", attributes="locked")),
                completed(stdout=_ref("feature", "1ocked00") + "\n"),
                completed(stdout=f"{ADMIN}/locked\n"),
                completed(stdout=f"{int(NOW)}\n"),
                completed(stdout="4096\t/wt/locked\n"),
            ]
        )
        trees = classify(runner, repo=REPO, now=NOW)
        self.assertEqual(trees[0].verdict, "LOCKED")
        self.assertIn("no reason recorded", trees[0].reason)

    def test_a_prunable_entry_is_never_statted_or_sized(self) -> None:
        """Its working directory is already gone: `du` and `git status` would fail
        on a path that does not exist, so neither is issued."""
        runner = FakeRunner(
            [
                completed(
                    stdout=_one_tree(
                        "/wt/gone",
                        "9one000",
                        attributes="prunable gitdir file points to non-existent location",
                    )
                ),
                completed(stdout=_ref("feature", "9one000") + "\n"),
                completed(stdout="refs/heads/main\n"),
            ]
        )
        trees = classify(runner, repo=REPO, now=NOW)
        self.assertEqual(trees[0].verdict, "SAFE")
        self.assertIn("prunable", trees[0].reason)
        self.assertEqual(trees[0].idle_days, UNKNOWN)
        self.assertEqual(trees[0].megabytes, UNKNOWN)
        self.assertEqual(len(runner.calls), 3)


class DegradationTests(unittest.TestCase):
    def test_stat_falls_back_to_the_gnu_spelling(self) -> None:
        """macOS wants `stat -f %m`, GNU wants `stat -c %Y`, and each rejects the
        other's flag. Both are tried rather than sniffing the platform."""
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/one", "c0ffee1")),
                completed(stdout=_ref("feature", "c0ffee1") + "\n"),
                completed(stdout=""),
                completed(stdout="refs/heads/main\n"),
                completed(stdout=f"{ADMIN}/one\n"),
                completed(stderr="stat: illegal option -- f", returncode=1),
                completed(stdout=f"{int(NOW) - 86400 * 9}\n"),
                completed(stdout="4096\t/wt/one\n"),
            ]
        )
        self.assertEqual(classify(runner, repo=REPO, now=NOW)[0].idle_days, 9)
        self.assertEqual(runner.calls[-3][:2], ["stat", "-f"])
        self.assertEqual(runner.calls[-2][:2], ["stat", "-c"])

    def test_an_unmeasurable_age_or_size_is_unknown_not_zero(self) -> None:
        """A tree of unknown age must not read as touched today: 0 would be a
        number Phase 2's idle threshold would then trust."""
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/one", "c0ffee1")),
                completed(stdout=_ref("feature", "c0ffee1") + "\n"),
                completed(stdout=""),
                completed(stdout="refs/heads/main\n"),
                completed(stderr="not a git repository", returncode=128),
                completed(stderr="du: No such file or directory", returncode=1),
            ]
        )
        tree = classify(runner, repo=REPO, now=NOW)[0]
        self.assertEqual(tree.idle_days, UNKNOWN)
        self.assertEqual(tree.megabytes, UNKNOWN)
        self.assertEqual(tree.verdict, "SAFE")

    def test_a_failed_status_refuses_to_issue_a_verdict(self) -> None:
        """A verdict is a claim. Guessing one for a tree we could not inspect is
        the `UnprovisionedWorktree` failure mode applied to a report."""
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/one", "c0ffee1")),
                completed(stdout=_ref("feature", "c0ffee1") + "\n"),
                completed(stderr="fatal: not a git repository", returncode=128),
            ]
        )
        with self.assertRaises(WorktreeProbeFailed) as ctx:
            classify(runner, repo=REPO, now=NOW)
        self.assertIn("status", str(ctx.exception))

    def test_a_failed_listing_refuses_to_report_an_empty_inventory(self) -> None:
        runner = FakeRunner([completed(stderr="fatal: not a git repository", returncode=128)])
        with self.assertRaises(WorktreeProbeFailed):
            classify(runner, repo=REPO, now=NOW)

    def test_a_repo_with_no_linked_trees_classifies_to_nothing(self) -> None:
        runner = FakeRunner(
            [
                completed(
                    stdout=f"worktree {REPO}\nHEAD {MAIN_SHA}\nbranch refs/heads/main\n\n"
                ),
                completed(stdout=REFS),
            ]
        )
        self.assertEqual(classify(runner, repo=REPO, now=NOW), [])


class ParsingTests(unittest.TestCase):
    def test_porcelain_attributes_are_read_as_fields_not_prose(self) -> None:
        stanzas = _parse_worktree_list(
            "worktree /a\nHEAD aaa\nbranch refs/heads/feature\n\n"
            "worktree /b\nHEAD bbb\ndetached\nlocked on a removable drive\n\n"
            "worktree /c\nHEAD ccc\nbranch refs/heads/x\n"
            "prunable gitdir file points to non-existent location\n\n"
        )
        self.assertEqual([str(s.path) for s in stanzas], ["/a", "/b", "/c"])
        self.assertEqual(stanzas[0].branch, "feature")
        self.assertIsNone(stanzas[1].branch)
        self.assertTrue(stanzas[1].locked)
        self.assertEqual(stanzas[1].lock_reason, "on a removable drive")
        self.assertTrue(stanzas[2].prunable)
        self.assertFalse(stanzas[0].locked)

    def test_a_trailing_record_without_a_blank_line_is_not_dropped(self) -> None:
        stanzas = _parse_worktree_list("worktree /a\nHEAD aaa\nbranch refs/heads/main\n")
        self.assertEqual(len(stanzas), 1)

    def test_a_bare_main_repository_has_no_head(self) -> None:
        stanzas = _parse_worktree_list("worktree /bare\nbare\n\n")
        self.assertTrue(stanzas[0].bare)
        self.assertEqual(stanzas[0].head, "")

    def test_upstreams_map_short_names_and_record_absence_as_empty(self) -> None:
        upstreams = _parse_upstreams(
            _ref("a", "aaa", "refs/remotes/origin/a") + "\n" + _ref("b", "bbb") + "\n"
        )
        self.assertEqual(upstreams["a"], "refs/remotes/origin/a")
        self.assertEqual(upstreams["b"], "")


class RenderTests(unittest.TestCase):
    def test_one_row_per_stanza_with_the_main_checkout_labelled(self) -> None:
        runner = _todays_runner()
        trees = classify(runner, repo=REPO, now=NOW)
        stanzas = _parse_worktree_list(LISTING)
        table = _render(stanzas[0], trees)
        rows = table.splitlines()
        self.assertEqual(rows[0].split()[0], "PATH")
        self.assertEqual(len(rows) - 1, len(stanzas))
        self.assertIn("main checkout; never a candidate", rows[1])

    def test_the_measured_age_size_and_verdict_all_reach_the_table(self) -> None:
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        table = _render(_parse_worktree_list(LISTING)[0], trees)
        self.assertIn("15 d", table)
        self.assertIn("103", table)
        self.assertIn("SAFE", table)
        self.assertIn("fix/raw-discovery-project-attribution", table)

    def test_the_home_prefix_is_abbreviated(self) -> None:
        """Cosmetic, and only in the table: every command is issued with the
        absolute path, which is what the argv assertions above pin."""
        tree = Tree(
            path=Path.home() / "wt" / "one",
            branch="feature",
            head="c0ffee1",
            verdict="SAFE",
            idle_days=3,
            megabytes=12,
            reason="clean, unlocked; head is in refs/heads/main",
        )
        table = _render(None, [tree])
        self.assertIn("~/wt/one", table)
        self.assertNotIn(str(Path.home() / "wt"), table)

    def test_a_bare_main_repository_is_labelled_bare_not_detached(self) -> None:
        stanza = _parse_worktree_list("worktree /bare\nbare\n\n")[0]
        self.assertIn("(bare)", _render(stanza, []))

    def test_a_detached_head_row_names_the_commit_it_is_parked_on(self) -> None:
        tree = Tree(
            path=Path("/wt/detached"),
            branch=None,
            head="ff102df8ba8f3659df14b7930f4d569aba0f960e",
            verdict="SAFE",
            idle_days=12,
            megabytes=112,
            reason="clean, unlocked; head is in refs/heads/main",
        )
        self.assertIn("(detached ff102df)", _render(None, [tree]))

    def test_unknown_age_and_size_print_as_question_marks(self) -> None:
        tree = Tree(
            path=Path("/wt/one"),
            branch="feature",
            head="c0ffee1",
            verdict="SAFE",
            idle_days=UNKNOWN,
            megabytes=UNKNOWN,
            reason="clean, unlocked; head is in refs/heads/main",
        )
        self.assertIn("?", _render(None, [tree]))
        self.assertNotIn("-1", _render(None, [tree]))


class MainTests(unittest.TestCase):
    def test_main_prints_the_table_and_returns_0(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main([], runner=_todays_runner()), 0)
        printed = out.getvalue().splitlines()
        self.assertEqual(printed[0].split()[0], "PATH")
        self.assertEqual(len(printed), 1 + 4)  # header + one row per stanza
        self.assertIn("SAFE", out.getvalue())

    def test_main_reports_from_the_current_directory(self) -> None:
        runner = _todays_runner()
        with redirect_stdout(io.StringIO()):
            main([], runner=runner)
        self.assertEqual(runner.calls[0][:3], ["git", "-C", str(Path.cwd())])

    def test_an_unknown_flag_fails_loudly_instead_of_being_ignored(self) -> None:
        with (
            self.assertRaises(SystemExit) as ctx,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main(["--delete"], runner=_todays_runner())
        self.assertEqual(ctx.exception.code, 2)
