"""Optional, privacy-preserving Laminar tracing for short-lived CLI commands."""

from __future__ import annotations

import os
from collections.abc import Callable


def run_traced(command: str, operation: Callable[[], int]) -> int:
    """Run one CLI command as one Laminar trace when the optional SDK is present.

    Arguments and report contents are deliberately excluded: transcript paths,
    session identifiers, prompts, and outputs can contain private material.
    """
    try:
        from lmnr import Laminar
    except ModuleNotFoundError as exc:
        if exc.name != "lmnr":
            raise
        return operation()

    try:
        Laminar.initialize(
            project_api_key=os.environ.get("LMNR_PROJECT_API_KEY"),
            instruments=set(),
        )
    except ValueError:
        # Installing the optional dependency must not make the standard CLI
        # require a Laminar key. The SDK also checks a local .env file.
        return operation()

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
