"""Optional, privacy-preserving Laminar tracing for short-lived CLI commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from inspect import iscoroutinefunction
from typing import ParamSpec, TypeVar, cast

from toolbench.observability import setup_tracing
from toolbench.observability.setup_tracing import _load_laminar

P = ParamSpec("P")
R = TypeVar("R")


def run_traced(command: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate one CLI operation with an optional Laminar trace.

    Arguments and report contents are deliberately excluded: transcript paths,
    session identifiers, prompts, and outputs can contain private material.
    The async wrapper keeps the span open until the decorated coroutine has
    finished, rather than tracing only the coroutine object's construction.
    """

    def decorator(operation: Callable[P, R]) -> Callable[P, R]:
        if iscoroutinefunction(operation):
            async_operation = cast(Callable[P, Awaitable[int]], operation)

            @wraps(operation)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
                if not setup_tracing():
                    return await async_operation(*args, **kwargs)

                Laminar = _load_laminar()
                try:
                    with Laminar.start_as_current_span(
                        "toolbench.cli",
                        tags=["toolbench", command],
                    ):
                        Laminar.set_trace_metadata({"command": command})
                        exit_code = await async_operation(*args, **kwargs)
                        Laminar.set_span_output({"exit_code": exit_code})
                        return exit_code
                finally:
                    Laminar.flush()

            return cast(Callable[P, R], async_wrapper)

        sync_operation = cast(Callable[P, int], operation)

        @wraps(operation)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
            if not setup_tracing():
                return sync_operation(*args, **kwargs)

            Laminar = _load_laminar()
            try:
                with Laminar.start_as_current_span(
                    "toolbench.cli",
                    tags=["toolbench", command],
                ):
                    Laminar.set_trace_metadata({"command": command})
                    exit_code = sync_operation(*args, **kwargs)
                    Laminar.set_span_output({"exit_code": exit_code})
                    return exit_code
            finally:
                Laminar.flush()

        return cast(Callable[P, R], sync_wrapper)

    return decorator
