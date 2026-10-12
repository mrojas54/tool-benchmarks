"""Freeze-replay guards and restamping, split out of `passive.py` (tech-debt 20261011).

Everything here concerns what a `--freeze` replay does to, or refuses to do with, a
pinned manifest: re-deriving stale `is_subagent` flags, naming the refs that could
not be refreshed, and the two refusals for a pin that would scan nothing. The
corpus-resolution seam (`_resolve_corpus`, `_replay_census`) stays in `passive.py`
because it needs `CliArgs`/`_ResolvedCorpus`, which live there.

`passive` imports from this module, so `CliArgs` and `_FreezePlan` are referenced
under `TYPE_CHECKING` only: the runtime import graph stays acyclic.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import TYPE_CHECKING

from toolbench.sources import Runner, SessionRef, agentsview_classify_subagents

if TYPE_CHECKING:
    from toolbench.passive import CliArgs, _FreezePlan


def _restamp_agentsview_subagents(
    refs: list[SessionRef], runner: Runner | None, args: CliArgs
) -> tuple[list[SessionRef], int]:
    """Re-derive `is_subagent` for AgentsView refs on a `--exclude-subagents`
    freeze replay (Low finding, 20260921 tech-debt report).

    TB-29's self-heal (`freeze._is_subagent_from_manifest`) treats a session's
    PATH as ground truth and ORs it with the stored flag -- but AgentsView refs
    are built with `path=None`, so for them the self-heal can never run. A
    pre-TB-31 AgentsView manifest that recorded `is_subagent: false` for a real
    subagent keeps that stale value forever, and `--exclude-subagents` on replay
    then silently lets it through while the report claims it was excluded.

    Gated on `args.exclude_subagents`: replay is deliberately hermetic (S37 --
    it exists precisely so a run does not depend on a live corpus that may have
    moved since freeze), and `test_replay_uses_frozen_refs_not_live_discovery`
    pins that a plain replay issues no `session list` call at all. A stale flag
    only changes the SCAN when `--exclude-subagents` acts on it, so the one
    extra parent-probe pass this costs is paid only on the replay shape that can
    actually be wrong -- every other replay stays exactly as hermetic as before.
    A no-op, further, when the manifest carries no AgentsView refs at all (raw
    refs self-heal from `path` already).

    Returns the refs plus how many AgentsView refs could not be classified and so
    kept their frozen flag; the caller renders that count into the report.
    """
    if not args.exclude_subagents or not any(ref.source == "agentsview" for ref in refs):
        return refs, 0
    assert runner is not None
    parent_ids, child_ids = agentsview_classify_subagents(
        runner,
        agent=args.agent,
        project=None if args.all_projects else args.project,
        since=args.since,
        limit=args.limit if args.limit is not None else 500,
    )
    # Absence from the live listing is not evidence of being a child: the listing is
    # bounded by --limit/--since/--project and reflects the archive now, so a frozen
    # parent that was pruned or left the window is absent from it. Only ids the archive
    # positively classifies are restamped; the rest keep their stored flag.
    restamped: list[SessionRef] = []
    unclassifiable = 0
    for ref in refs:
        if ref.source != "agentsview":
            restamped.append(ref)
        elif ref.session_id in child_ids:
            restamped.append(replace(ref, is_subagent=True))
        elif ref.session_id in parent_ids:
            restamped.append(replace(ref, is_subagent=False))
        else:
            unclassifiable += 1
            restamped.append(ref)
    if unclassifiable:
        print(
            f"toolbench.passive: note: {unclassifiable} AgentsView ref(s) in the freeze "
            "are absent from the live archive listing and could not be re-classified "
            "as subagents; their frozen is_subagent flag was kept.",
            file=sys.stderr,
        )
    return restamped, unclassifiable


def _restamp_note(unclassifiable: int) -> str | None:
    """The Summary line for refs whose frozen `is_subagent` flag could not be refreshed.

    `None` when every ref was classified (or no restamp ran): the line is a caveat on
    a number, so it is withheld -- never rendered as "0" -- when the number is sound.
    """
    if not unclassifiable:
        return None
    return (
        f"**Subagent flags kept from freeze**: {unclassifiable} AgentsView ref(s) were "
        "absent from the live archive listing and could not be re-classified, so their "
        "frozen is_subagent flag was kept; the --exclude-subagents count may rest on "
        "stale flags."
    )


def _empty_replay_refusal(freeze_path: str) -> str:
    """Why a replay of a manifest pinning zero refs is refused (S37 / S23).

    The write side already refuses to pin nothing (`_empty_freeze_refusal`), but a
    manifest written before that guard, hand-edited, or truncated can still carry
    `refs: []`, and `read_manifest` is deliberately tolerant of it -- an empty list
    is well-formed. Replaying it scanned nothing and exited 0 with the same first
    line a genuinely empty archive prints; the provenance line underneath told a
    human, but nothing told a script keying on the exit status. The read side is
    now guarded the way the write side is, and the file is left exactly as found:
    the run refuses, it does not tidy.

    A pin whose refs have all VANISHED since the freeze is not this case: those
    refs are present, they simply no longer load, and the replay discloses the
    vanished count (TB-22) instead of refusing.
    """
    return (
        f"toolbench.passive: fatal freeze error: {freeze_path} pins zero sessions; "
        "refusing to replay an empty freeze manifest. A replay of nothing would "
        'answer "no sessions matched" at exit 0 in the same words a genuinely empty '
        "archive uses. Remove the manifest and re-run --freeze to pin a fresh "
        "selection, or drop --freeze."
    )


def _replay_emptied_by_exclude_subagents(
    freeze: _FreezePlan, refs: list[SessionRef], sessions_discovered: int
) -> bool:
    """True iff a replay pinned refs but `--exclude-subagents` just dropped
    every one of them -- the condition `_empty_replay_after_filter_refusal`
    guards. Split out of `main` to keep the refusal's three-part condition off
    `main`'s own complexity count (it is one caller-visible fact, not three)."""
    return freeze.replaying and not refs and sessions_discovered > 0


def _empty_replay_after_filter_refusal(freeze_path: str, pinned: int) -> str:
    """Why a replay is refused when `--exclude-subagents` empties a subagent-only
    pin (Medium finding, 20260921 tech-debt report).

    Distinct from `_empty_replay_refusal`: that guard catches a manifest that
    pinned zero refs to begin with. This one catches a manifest that pinned refs
    -- just all of them subagent sessions -- replayed with a flag the freeze was
    never written under. The remedy is the flag, not the manifest, so the two
    refusals do not share text.
    """
    return (
        f"toolbench.passive: fatal freeze error: {freeze_path} pins {pinned} "
        "session(s), but --exclude-subagents drops every one of them; refusing "
        "to replay a manifest that would scan zero sessions. A replay of "
        'nothing would answer "no sessions matched" at exit 0 in the same words '
        "a genuinely empty archive uses. Drop --exclude-subagents, or re-freeze "
        "a selection that still has parent sessions after it filters."
    )
