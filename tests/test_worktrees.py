"""Evals for the linked-worktree reporter (`toolbench worktrees`).

Every case drives `classify()` through the `Runner` seam (S24) with git output
captured from this repository, so no test reads live worktree state: the
inventory a test sees is the one it scripted, at the instant it names via `now`.

The fixture below is the state at `2ff6ed2` -- the main checkout plus the three
foreign trees PR #88 deliberately left in place. It is the reference the
structure outline's four-row table records, and the idle ages and sizes asserted
here are the ones measured there (1 d / 15 d / 16 d, 112 / 103 / 103 MB).

Two assertions in this file are the design made falsifiable rather than merely
stated. `test_todays_repo_has_nothing_to_report` pins the number the whole
reporter is built on -- today's reclaimable count is zero, so the hook is silent.
`test_the_five_trees_pr_88_removed_are_all_reclaimable` pins the other end: the
predicate reproduces a decision a human already made by hand. A change that
breaks either has changed what the reporter means, not just how it is written.
"""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.fakes import FakeRunner, completed
from toolbench.worktrees import (
    IDLE_DAYS,
    UNKNOWN,
    Tree,
    Verdict,
    WorktreeProbeFailed,
    _hook_line,
    _parse_upstreams,
    _parse_worktree_list,
    _render,
    _total_megabytes,
    classify,
    is_claimed,
    main,
    reclaimable,
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
    upstream_live: bool | None = None,
) -> list[subprocess.CompletedProcess[str]]:
    """The responses one clean linked tree consumes, in issue order.

    Five for a tree whose branch records no upstream -- status, reachability,
    admin-dir lookup, gitdir mtime, size. Six when `upstream_live` is set, which
    inserts the `rev-parse --verify` that decides CLAIMED after the reachability
    call. `None` means "no verify is issued": either the branch has no recorded
    upstream, or the head is unreachable and UNIQUE-WORK settles it first.
    """
    responses = [completed(stdout=dirty), completed(stdout=contains)]
    if upstream_live is not None:
        responses.append(
            completed(stdout="e092d9e73427b9504721c1747a2028ac9f30b507\n")
            if upstream_live
            else completed(
                stderr="fatal: Needed a single revision", returncode=128
            )
        )
    responses += [
        completed(stdout=f"{gitdir}\n"),
        completed(stdout=f"{mtime}\n"),
        completed(stdout=f"{kilobytes}\t{gitdir}\n"),
    ]
    return responses


def _todays_runner() -> FakeRunner:
    """The four-tree fixture: main plus the three trees left in place at `2ff6ed2`.

    All three record a live upstream, so all three verify and land on CLAIMED --
    which is the whole point: the two idlest trees in the repository are the two
    that must never be named.
    """
    return FakeRunner(
        [
            completed(stdout=LISTING),
            completed(stdout=REFS),
            *_probe(
                contains="refs/remotes/origin/codex-harbor-wids-d2-task\n",
                upstream_live=True,
                gitdir=f"{ADMIN}/tool-benchmarks-harbor-wids-d2",
                mtime=1784961121,
                kilobytes=113928,
            ),
            *_probe(
                contains="refs/remotes/origin/fix/raw-discovery-project-attribution\n",
                upstream_live=True,
                gitdir=f"{ADMIN}/0y57",
                mtime=1783749011,
                kilobytes=105164,
            ),
            *_probe(
                contains="refs/remotes/origin/refactor/single-pass-probe-passive-split\n",
                upstream_live=True,
                gitdir=f"{ADMIN}/ms0r",
                mtime=1783741578,
                kilobytes=105452,
            ),
        ]
    )


# The five trees PR #88 removed by hand at `5e93ba6`: four Agent-tool trees whose
# `tb-*` branches were merged and whose origin refs were deleted at merge time,
# and the detached Claude Desktop tree. Sizes and dates are that PR's measured
# ones (112 MB each; gitdir mtimes 2026-07-14 and 2026-07-15).
NESTED = f"{REPO}/.claude/worktrees"
PR88_TREES = (
    ("agent-a33e68186dd938d6d", "fix/tb-38-auto-fallback-mid-listing", "c921771"),
    ("agent-ad6bf2d495e07d7a7", "feat/tb-37-freeze-manifest-census", "52c45bf"),
    ("agent-ae85e382135ef3cf3", "chore/tb-36-probe-argv-sole-builder", "036a704"),
    ("agent-afee11e321190041b", "fix/tb-34-zero-match-census-disclosure", "b782e75"),
)
DETACHED_SHA = "ff102df8ba8f3659df14b7930f4d569aba0f960e"
MB_112 = 114688


def _pr88_runner() -> FakeRunner:
    """The five removed trees replayed as a fixture, at the ages they were removed."""
    listing = f"worktree {REPO}\nHEAD {MAIN_SHA}\nbranch refs/heads/main\n\n"
    refs: list[str] = []
    probes: list[subprocess.CompletedProcess[str]] = []
    for name, branch, sha in PR88_TREES:
        listing += f"worktree {NESTED}/{name}\nHEAD {sha}\nbranch refs/heads/{branch}\n\n"
        # A recorded upstream whose remote ref is gone: non-empty %(upstream),
        # failing rev-parse. This is the shape `clean_gone` meant to catch and
        # could not, and it is NOT a claim.
        refs.append(_ref(branch, sha, f"refs/remotes/origin/{branch}"))
        probes += _probe(
            contains="refs/heads/main\n",
            upstream_live=False,
            gitdir=f"{ADMIN}/{name}",
            mtime=int(NOW) - 86400 * 13,
            kilobytes=MB_112,
        )
    listing += (
        f"worktree {NESTED}/tech-debt-assessment-routine-fcf1e6\n"
        f"HEAD {DETACHED_SHA}\ndetached\n\n"
    )
    probes += _probe(
        contains="refs/heads/main\nrefs/remotes/origin/main\n",
        gitdir=f"{ADMIN}/tech-debt-assessment-routine-fcf1e6",
        mtime=int(NOW) - 86400 * 12,
        kilobytes=MB_112,
    )
    return FakeRunner(
        [completed(stdout=listing), completed(stdout="\n".join(refs) + "\n"), *probes]
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


def _tree(verdict: Verdict, idle_days: int) -> Tree:
    """A classified tree built directly, for predicate cases where the git output
    that produced the verdict is beside the point."""
    return Tree(
        path=Path("/wt/one"),
        branch="feature",
        head="c0ffee1",
        verdict=verdict,
        idle_days=idle_days,
        megabytes=112,
        reason="fixture",
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

    def test_all_three_are_claimed_because_a_live_upstream_still_backs_them(
        self,
    ) -> None:
        """Their WORK is safe -- none holds a commit that exists nowhere else --
        but the tree is somebody's live checkout, and that is the fact that
        decides whether this reporter is entitled to name it."""
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        self.assertEqual([t.verdict for t in trees], ["CLAIMED", "CLAIMED", "CLAIMED"])
        self.assertIn("refs/remotes/origin/codex-harbor-wids-d2-task", trees[0].reason)
        self.assertIn("never a candidate at any age", trees[0].reason)

    def test_todays_repo_has_nothing_to_report(self) -> None:
        """The number the whole feature is built on. Three trees, 318 MB, two of
        them the idlest things registered against this clone -- and zero of them
        reclaimable. If this fails, either the repository genuinely acquired a
        stale tree or the predicate regressed; both are worth a red suite."""
        trees = classify(_todays_runner(), repo=REPO, now=NOW)
        self.assertEqual(len(trees), 3)
        self.assertEqual(reclaimable(trees), [])

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
        fail here rather than silently shell out during a report. Six calls per
        claimed tree: the sixth is the liveness check, and it is issued once per
        tree rather than once per branch in the repository."""
        runner = _todays_runner()
        classify(runner, repo=REPO, now=NOW)
        self.assertEqual(len(runner.calls), 2 + 3 * 6)
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
            runner.calls[2:8],
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
                [
                    "git",
                    "-C",
                    str(REPO),
                    "rev-parse",
                    "--verify",
                    "refs/remotes/origin/codex-harbor-wids-d2-task",
                ],
                ["git", "-C", HARBOR, "rev-parse", "--absolute-git-dir"],
                ["stat", "-f", "%m", f"{ADMIN}/tool-benchmarks-harbor-wids-d2/gitdir"],
                ["du", "-sk", HARBOR],
            ],
        )


class IsClaimedTests(unittest.TestCase):
    """The ownership predicate on its own. `%(upstream)` establishes that an
    upstream was recorded; only `rev-parse --verify` establishes that it still
    exists, and the gap between those two is exactly the `[gone]` case."""

    def test_a_live_remote_tracking_ref_claims_the_branch(self) -> None:
        runner = FakeRunner([completed(stdout=f"{CURSOR_0Y57_SHA}\n")])
        self.assertTrue(
            is_claimed(
                runner,
                "feature",
                repo=REPO,
                upstreams={"feature": "refs/remotes/origin/feature"},
            )
        )
        self.assertEqual(
            runner.calls,
            [
                [
                    "git",
                    "-C",
                    str(REPO),
                    "rev-parse",
                    "--verify",
                    "refs/remotes/origin/feature",
                ]
            ],
        )

    def test_a_gone_upstream_is_recorded_but_not_live_so_it_is_not_a_claim(
        self,
    ) -> None:
        """The four `tb-*` branches PR #88 deleted: `%(upstream)` non-empty, remote
        ref absent. Reading the recorded field alone would exempt every one of
        them forever."""
        runner = FakeRunner(
            [completed(stderr="fatal: Needed a single revision", returncode=128)]
        )
        self.assertFalse(
            is_claimed(
                runner,
                "feature",
                repo=REPO,
                upstreams={"feature": "refs/remotes/origin/feature"},
            )
        )

    def test_no_upstream_at_all_is_unclaimed_and_asks_git_nothing(self) -> None:
        """The `worktree-agent-*` shape. FakeRunner raises on any call, so this
        also pins that a branch with no upstream costs no subprocess."""
        runner = FakeRunner([])
        self.assertFalse(
            is_claimed(runner, "feature", repo=REPO, upstreams={"feature": ""})
        )
        self.assertEqual(runner.calls, [])

    def test_a_detached_head_has_no_branch_to_claim(self) -> None:
        runner = FakeRunner([])
        self.assertFalse(is_claimed(runner, None, repo=REPO, upstreams={}))
        self.assertEqual(runner.calls, [])

    def test_a_branch_absent_from_the_ref_scan_is_unclaimed(self) -> None:
        runner = FakeRunner([])
        self.assertFalse(is_claimed(runner, "ghost", repo=REPO, upstreams={}))
        self.assertEqual(runner.calls, [])

    def test_a_local_tracking_upstream_is_not_a_remote_claim(self) -> None:
        """`branch.<name>.remote = .` records an upstream under refs/heads/. That
        ref is live, but it is evidence about this clone, not about somebody
        else's checkout -- and it is never verified, so no call is issued."""
        runner = FakeRunner([])
        self.assertFalse(
            is_claimed(
                runner, "feature", repo=REPO, upstreams={"feature": "refs/heads/main"}
            )
        )
        self.assertEqual(runner.calls, [])


class ReclaimableTests(unittest.TestCase):
    def test_the_five_trees_pr_88_removed_are_all_reclaimable(self) -> None:
        """The predicate reproduces a decision that was made by hand: four
        `[gone]`-upstream Agent trees and one detached Desktop tree, 560 MB."""
        trees = classify(_pr88_runner(), repo=REPO, now=NOW)
        self.assertEqual(len(trees), 5)
        self.assertEqual([t.verdict for t in trees], ["SAFE"] * 5)
        self.assertEqual(reclaimable(trees), trees)
        self.assertEqual(sum(t.megabytes for t in trees), 560)

    def test_a_claimed_tree_is_exempt_four_hundred_days_later(self) -> None:
        """A claim never expires, and this is the rule most likely to be
        'simplified' away later. The eldest tree here is already 16 days idle and
        deliberately left in place, so any expiry at or below 16 flags it today
        and any number above it fails on a later morning."""
        far_future = NOW + 86400 * 400
        trees = classify(_todays_runner(), repo=REPO, now=far_future)
        self.assertEqual([t.verdict for t in trees], ["CLAIMED"] * 3)
        self.assertEqual([t.idle_days for t in trees], [401, 415, 416])
        self.assertEqual(reclaimable(trees), [])

    def test_dirty_locked_and_unique_work_are_never_reclaimable_at_any_age(
        self,
    ) -> None:
        """They are not silent either -- the table still prints them, because
        'I could not prove this one safe' is what a human sweeping by hand needs.
        They simply never drive the count."""
        aged = [
            _tree("DIRTY", 900),
            _tree("LOCKED", 900),
            _tree("UNIQUE-WORK", 900),
            _tree("CLAIMED", 900),
        ]
        self.assertEqual(reclaimable(aged), [])

    def test_an_unmeasurable_idle_age_is_not_evidence_that_a_tree_is_old(
        self,
    ) -> None:
        self.assertEqual(reclaimable([_tree("SAFE", UNKNOWN)]), [])

    def test_an_empty_inventory_reclaims_nothing(self) -> None:
        self.assertEqual(reclaimable([]), [])


class ThresholdTests(unittest.TestCase):
    """`IDLE_DAYS` gates unclaimed trees only, and its job is narrow: don't name
    a tree somebody created an hour ago."""

    def _unclaimed_at(self, mtime: int) -> FakeRunner:
        return FakeRunner(
            [
                completed(stdout=_one_tree("/wt/fresh", "abc1234")),
                completed(stdout=_ref("feature", "abc1234") + "\n"),
                *_probe(
                    contains="refs/heads/main\n",
                    gitdir=f"{ADMIN}/fresh",
                    mtime=mtime,
                    kilobytes=MB_112,
                ),
            ]
        )

    def test_an_unclaimed_tree_below_the_threshold_is_reported_but_not_reclaimable(
        self,
    ) -> None:
        trees = classify(
            self._unclaimed_at(int(NOW) - 86400 * (IDLE_DAYS - 1)), repo=REPO, now=NOW
        )
        self.assertEqual(trees[0].verdict, "SAFE")
        self.assertEqual(trees[0].idle_days, IDLE_DAYS - 1)
        self.assertEqual(reclaimable(trees), [])

    def test_the_same_tree_past_the_threshold_is_reclaimable(self) -> None:
        trees = classify(
            self._unclaimed_at(int(NOW) - 86400 * IDLE_DAYS), repo=REPO, now=NOW
        )
        self.assertEqual(trees[0].idle_days, IDLE_DAYS)
        self.assertEqual(reclaimable(trees), trees)


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
                    upstream_live=True,
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
        self.assertEqual([t.verdict for t in trees], ["CLAIMED", "UNIQUE-WORK"])

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
        upstream_live: bool | None = None,
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
                    upstream_live=upstream_live,
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
            contains="refs/heads/main\n",
            upstream="refs/remotes/origin/feature",
            upstream_live=False,
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

    def test_unique_work_outranks_a_live_upstream_and_skips_the_claim_check(
        self,
    ) -> None:
        """A tree ahead of its own live upstream holds the only copy of something.
        That is the more urgent thing to tell a human, and it is settled before
        ownership is asked -- so no verify call is issued at all."""
        trees = self._single(contains="", upstream="refs/remotes/origin/feature")
        self.assertEqual(trees[0].verdict, "UNIQUE-WORK")
        self.assertEqual(reclaimable(trees), [])

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

    def test_a_dirty_tree_with_a_live_upstream_stays_dirty_not_claimed(self) -> None:
        """Precedence: DIRTY outranks CLAIMED. Both are non-candidates, but the
        one that names what git itself would refuse is the more useful row."""
        runner = FakeRunner(
            [
                completed(stdout=_one_tree("/wt/dirty", "d1d1d1d")),
                completed(
                    stdout=_ref("feature", "d1d1d1d", "refs/remotes/origin/feature")
                    + "\n"
                ),
                completed(stdout=" M src/toolbench/report.py\n"),
                completed(stdout=f"{ADMIN}/dirty\n"),
                completed(stdout=f"{int(NOW) - 86400 * 90}\n"),
                completed(stdout="4096\t/wt/dirty\n"),
            ]
        )
        trees = classify(runner, repo=REPO, now=NOW)
        self.assertEqual(trees[0].verdict, "DIRTY")
        self.assertEqual(reclaimable(trees), [])

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
        self.assertEqual(reclaimable(trees), [])
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
        # Unmeasurable age, so the threshold cannot be satisfied: `git worktree
        # prune` already reclaims this one, and the reporter does not guess.
        self.assertEqual(reclaimable(trees), [])


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
        number the idle threshold would then trust."""
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
        self.assertEqual(reclaimable([tree]), [])

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
        self.assertIn("CLAIMED", table)
        self.assertIn("fix/raw-discovery-project-attribution", table)

    def test_a_non_candidate_still_prints_even_though_it_never_drives_the_count(
        self,
    ) -> None:
        """The table reports everything; `reclaimable` narrows it. 'I could not
        prove this one safe' is information, not noise."""
        table = _render(None, [_tree("UNIQUE-WORK", 40), _tree("LOCKED", 40)])
        self.assertIn("UNIQUE-WORK", table)
        self.assertIn("LOCKED", table)

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
            head=DETACHED_SHA,
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
        self.assertIn("CLAIMED", out.getvalue())

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


class ReclaimableOnlyTests(unittest.TestCase):
    def test_nothing_reclaimable_prints_nothing_at_all_and_exits_0(self) -> None:
        """Not a header, not "none found" -- an empty stdout, so any output at all
        is the signal. This is what earns the reporter the right to speak."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                main(["--reclaimable-only"], runner=_todays_runner()), 0
            )
        self.assertEqual(out.getvalue(), "")

    def test_candidates_print_as_a_table_without_the_main_checkout_row(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--reclaimable-only"], runner=_pr88_runner()), 0)
        printed = out.getvalue().splitlines()
        self.assertEqual(printed[0].split()[0], "PATH")
        self.assertEqual(len(printed), 1 + 5)
        self.assertNotIn("main checkout; never a candidate", out.getvalue())
        self.assertIn("agent-a33e68186dd938d6d", out.getvalue())

    def test_the_full_table_still_reports_the_trees_the_flag_filters_out(self) -> None:
        """Same inventory, two questions: `--reclaimable-only` answers "what may I
        remove", the bare command answers "what is registered and why"."""
        out = io.StringIO()
        with redirect_stdout(out):
            main([], runner=_todays_runner())
        self.assertEqual(out.getvalue().count("CLAIMED"), 3)


def _hook(payload: str, runner: FakeRunner) -> tuple[int, str, str]:
    """Run `--hook` with a scripted payload and runner. Returns (code, out, err).

    stdin is injected for the same reason `runner` and `now` are: a hook test
    that read the real stdin would depend on how pytest happened to be invoked.
    stderr is captured as well as stdout, because "silent" has to mean both --
    a traceback on stderr is exactly the hook-error notice this mode exists to
    never produce.
    """
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--hook"], runner=runner, stdin=io.StringIO(payload))
    return code, out.getvalue(), err.getvalue()


def _payload(source: str) -> str:
    return json.dumps({"hook_event_name": "SessionStart", "source": source})


class HookEnvelopeTests(unittest.TestCase):
    """What a session actually receives when there IS something to say."""

    def test_a_startup_session_with_candidates_gets_the_documented_envelope(
        self,
    ) -> None:
        code, out, err = _hook(_payload("startup"), _pr88_runner())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        envelope = json.loads(out)
        self.assertEqual(
            envelope["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        self.assertIn("additionalContext", envelope["hookSpecificOutput"])

    def test_the_context_names_the_count_the_size_and_the_procedure(self) -> None:
        """Count, megabytes, and where the procedure lives -- read cold, the line
        has to say what to run, not merely that something is wrong."""
        _, out, _ = _hook(_payload("startup"), _pr88_runner())
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("5 reclaimable git worktrees", context)
        self.assertIn("560 MB", context)
        self.assertIn("AGENTS.md", context)
        self.assertIn("git worktree remove", context)
        self.assertIn("git branch -d", context)

    def test_the_context_says_outright_that_nothing_was_deleted(self) -> None:
        """The hook never deletes. A notice about reclaimable disk that does not
        say so leaves the reader guessing whether it already acted."""
        _, out, _ = _hook(_payload("startup"), _pr88_runner())
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Nothing has been deleted", context)

    def test_it_is_one_line_of_context_not_the_table(self) -> None:
        """Context is injected into every session that gets it, so it is a
        recurring tax on the window. The table is one command away."""
        _, out, _ = _hook(_payload("startup"), _pr88_runner())
        self.assertEqual(len(out.strip().splitlines()), 1)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(len(context.splitlines()), 1)
        self.assertNotIn("VERDICT", context)
        self.assertNotIn("PATH", context)

    def test_a_resume_reports_the_same_as_a_startup(self) -> None:
        _, startup, _ = _hook(_payload("startup"), _pr88_runner())
        _, resume, _ = _hook(_payload("resume"), _pr88_runner())
        self.assertEqual(startup, resume)
        self.assertNotEqual(startup, "")

    def test_one_candidate_is_named_in_the_singular(self) -> None:
        trees = classify(
            FakeRunner(
                [
                    completed(stdout=_one_tree("/wt/one", "c0ffee1")),
                    completed(stdout=_ref("feature", "c0ffee1") + "\n"),
                    *_probe(
                        contains="refs/heads/main\n",
                        gitdir=f"{ADMIN}/one",
                        mtime=int(NOW) - 86400 * 30,
                        kilobytes=MB_112,
                    ),
                ]
            ),
            repo=REPO,
            now=NOW,
        )
        self.assertIn("1 reclaimable git worktree ", _hook_line(trees))

    def test_a_partly_unmeasured_total_says_so_instead_of_understating_itself(
        self,
    ) -> None:
        """A sum over a set with a hole in it is a number that can lie by
        omission -- so it names the hole, in the direction that keeps the notice
        harder rather than easier to ignore."""
        sized = _tree("SAFE", 30)
        unsized = Tree(
            path=Path("/wt/two"),
            branch="feature",
            head="c0ffee2",
            verdict="SAFE",
            idle_days=30,
            megabytes=UNKNOWN,
            reason="fixture",
        )
        self.assertEqual(_total_megabytes([sized]), "112 MB")
        self.assertEqual(_total_megabytes([sized, unsized]), "112+ MB (1 unmeasured)")
        self.assertIn("112+ MB (1 unmeasured)", _hook_line([sized, unsized]))


class HookSilenceTests(unittest.TestCase):
    """Silence is the answer this repository gives today, and it is the whole
    reason the hook is worth installing: any output at all is the signal."""

    def test_todays_repo_injects_nothing_at_all(self) -> None:
        code, out, err = _hook(_payload("startup"), _todays_runner())
        self.assertEqual((code, out, err), (0, "", ""))

    def test_no_linked_trees_at_all_is_silent(self) -> None:
        runner = FakeRunner(
            [
                completed(
                    stdout=f"worktree {REPO}\nHEAD {MAIN_SHA}\nbranch refs/heads/main\n\n"
                ),
                completed(stdout=REFS),
            ]
        )
        self.assertEqual(_hook(_payload("startup"), runner), (0, "", ""))


class HookSourceGateTests(unittest.TestCase):
    """`source` ∈ startup|resume|clear|compact|fork. Only the first two speak.

    Every case here scripts an EMPTY runner, so `calls == []` is the assertion
    that matters: a gated source must not even ask git, or a compacting session
    pays for a report it will never be shown.
    """

    def _gated(self, payload: str) -> None:
        runner = FakeRunner([])
        self.assertEqual(_hook(payload, runner), (0, "", ""))
        self.assertEqual(runner.calls, [])

    def test_a_compaction_does_not_re_inject_the_notice(self) -> None:
        self._gated(_payload("compact"))

    def test_clear_and_fork_are_also_gated(self) -> None:
        self._gated(_payload("clear"))
        self._gated(_payload("fork"))

    def test_a_payload_with_no_source_key_is_gated(self) -> None:
        self._gated(json.dumps({"hook_event_name": "SessionStart"}))

    def test_an_unknown_source_is_gated_rather_than_assumed_to_be_startup(
        self,
    ) -> None:
        self._gated(_payload("teleport"))

    def test_a_non_string_source_is_gated(self) -> None:
        self._gated(json.dumps({"source": ["startup"]}))


class HookNeverFailsTests(unittest.TestCase):
    """Exit 0 unconditionally. A hook that paints an error notice on every
    session start gets the whole hook disabled -- and then the one morning it
    would have been right, it is not installed."""

    def test_malformed_stdin_exits_0_with_no_traceback(self) -> None:
        runner = FakeRunner([])
        self.assertEqual(_hook("not json", runner), (0, "", ""))
        self.assertEqual(runner.calls, [])

    def test_empty_stdin_exits_0(self) -> None:
        self.assertEqual(_hook("", FakeRunner([])), (0, "", ""))

    def test_json_that_is_not_an_object_exits_0(self) -> None:
        """`json.load` succeeds on a bare list or string; `.get` would not."""
        self.assertEqual(_hook("[1, 2]", FakeRunner([])), (0, "", ""))
        self.assertEqual(_hook('"startup"', FakeRunner([])), (0, "", ""))
        self.assertEqual(_hook("null", FakeRunner([])), (0, "", ""))

    def test_a_git_failure_is_swallowed_rather_than_reported(self) -> None:
        """`WorktreeProbeFailed` is the right answer at a terminal -- `classify`
        still raises it there -- and the wrong one here."""
        runner = FakeRunner(
            [completed(stderr="fatal: not a git repository", returncode=128)]
        )
        self.assertEqual(_hook(_payload("startup"), runner), (0, "", ""))
        self.assertEqual(len(runner.calls), 1)

    def test_a_missing_git_binary_is_swallowed_too(self) -> None:
        """The runner raises rather than returning non-zero. Nothing about a
        session start should depend on this reporter being installable."""
        runner = FakeRunner([FileNotFoundError("git")])
        self.assertEqual(_hook(_payload("startup"), runner), (0, "", ""))

    def test_a_hung_git_that_times_out_is_swallowed(self) -> None:
        runner = FakeRunner([subprocess.TimeoutExpired(cmd=["git"], timeout=60.0)])
        self.assertEqual(_hook(_payload("startup"), runner), (0, "", ""))

    def test_the_terminal_path_still_raises_where_the_hook_would_not(self) -> None:
        """Same failure, two answers, and the difference is who asked."""
        with self.assertRaises(WorktreeProbeFailed):
            classify(
                FakeRunner(
                    [completed(stderr="fatal: not a git repository", returncode=128)]
                ),
                repo=REPO,
                now=NOW,
            )


class HookModeExclusivityTests(unittest.TestCase):
    def test_hook_and_reclaimable_only_are_modes_not_filters(self) -> None:
        """Asking for both is a mistake in a settings file worth naming loudly at
        the terminal, not silently resolving in one mode's favour."""
        with (
            self.assertRaises(SystemExit) as ctx,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main(["--hook", "--reclaimable-only"], runner=_todays_runner())
        self.assertEqual(ctx.exception.code, 2)

    def test_the_help_text_lists_the_hook_mode(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
            main(["--help"], runner=_todays_runner())
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--hook", out.getvalue())

    def test_the_hook_mode_does_not_accept_an_abbreviated_option(self) -> None:
        with (
            self.assertRaises(SystemExit) as ctx,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main(["--ho"], runner=_todays_runner())
        self.assertEqual(ctx.exception.code, 2)
