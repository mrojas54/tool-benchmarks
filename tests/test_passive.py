"""Facade re-exports for the passive analyzer split."""

from toolbench import passive


def test_passive_facade_reexports() -> None:
    for name in (
        "Reducer",
        "AgentStats",
        "ToolStats",
        "render_report",
        "corpus_fingerprint",
        "main",
        "parse_args",
        "_parse_ref",
        "_discover_refs",
        "OVERSIZED_OUTPUT_TOKENS",
        "UNKNOWN_MODEL",
    ):
        assert hasattr(passive, name), name


def test_passive_no_longer_imports_tempfile() -> None:
    assert not hasattr(passive, "tempfile")
