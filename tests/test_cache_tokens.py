"""Evals for the standalone cache-token benchmark reader (toolbench.cache_tokens, S39).

Fixture-backed: each asserts one contract of steps 3-4 (per-session sum, run
aggregation, per-ticket normalization) plus the lever-3 counter-metric trap that
makes tracking creation-alongside-read non-optional.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from toolbench.cache_tokens import (
    RunUsage,
    per_ticket,
    sum_run,
    sum_session,
)

FIX = Path(__file__).parent / "fixtures" / "cache_tokens"


def _lines(name: str) -> list[str]:
    return (FIX / name).read_text(encoding="utf-8").splitlines()


def test_measured_session_sums_read_and_creation() -> None:
    session = sum_session(_lines("measured.jsonl"))
    assert (session.read, session.creation) == (300, 30)
    assert (session.input, session.output) == (10, 3)
    assert session.usage_messages == 2
    assert session.measured
    assert session.total_billed == 300 + 30 + 10 + 3


def test_zero_cache_is_measured_zero_not_none() -> None:
    session = sum_session(_lines("zero_cache.jsonl"))
    assert session.read == 0 and session.creation == 0
    assert session.measured  # usage present -> 0, not None


def test_no_usage_session_is_unmeasured_none() -> None:
    session = sum_session(_lines("no_usage.jsonl"))
    assert session.read is None and session.creation is None
    assert not session.measured
    assert session.total_billed == 0


def test_run_aggregates_and_counts_unmeasured() -> None:
    run = sum_run(
        [FIX / "measured.jsonl", FIX / "zero_cache.jsonl", FIX / "no_usage.jsonl"]
    )
    assert run.sessions == 3
    assert run.unmeasured_sessions == 1
    assert run.read == 300 and run.creation == 30
    assert run.input == 17 and run.output == 6  # 10+7+0 ; 3+3+0
    assert run.total_billed == 300 + 30 + 17 + 6


def test_per_ticket_normalizes() -> None:
    run = sum_run([FIX / "measured.jsonl"])
    norm = per_ticket(run, tickets=2)
    assert norm["cache_read"] == 150.0
    assert norm["total_billed"] == (300 + 30 + 10 + 3) / 2


def test_per_ticket_rejects_zero_tickets() -> None:
    run = sum_run([FIX / "measured.jsonl"])
    with pytest.raises(ValueError, match="tickets must be > 0"):
        per_ticket(run, tickets=0)


def test_prefix_sharing_trap_conserves_total() -> None:
    """The lever-3 counter-metric (S39): a change that cuts cache_read but raises
    cache_creation by the same amount leaves total_billed unchanged — so reading
    cache_read alone would score a wash as a win. This is why the reader always
    surfaces creation next to read."""
    baseline = RunUsage(
        read=1000, creation=100, input=50, output=50, sessions=1, unmeasured_sessions=0
    )
    treatment = RunUsage(
        read=200, creation=900, input=50, output=50, sessions=1, unmeasured_sessions=0
    )
    assert treatment.read < baseline.read  # looks like a win...
    assert treatment.total_billed == baseline.total_billed  # ...but nothing was saved
