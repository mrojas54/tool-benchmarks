"""Optional, privacy-preserving Laminar tracing for short-lived CLI commands."""

from __future__ import annotations

from collections.abc import Callable

from toolbench.observability import setup_tracing
from toolbench.observability.setup_tracing import _load_laminar


def run_traced(command: str, operation: Callable[[], int]) -> int:
    """Run one CLI command as one Laminar trace when the optional SDK is present.

    Arguments and report contents are deliberately excluded: transcript paths,
    session identifiers, prompts, and outputs can contain private material.
    """
    if not setup_tracing():
        return operation()

    Laminar = _load_laminar()

    try:
        with Laminar.start_as_current_span(
            "toolbench.cli",
            tags=["toolbench", command],
        ):
            Laminar.set_trace_metadata({"command": command})
            exit_code = operation()
            Laminar.set_span_output({"exit_code": exit_code})
            return exit_code
    finally:
        Laminar.flush()
