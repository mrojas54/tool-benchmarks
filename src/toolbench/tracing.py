"""Optional, privacy-preserving Laminar tracing for short-lived CLI commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from inspect import iscoroutine, iscoroutinefunction
from typing import ParamSpec, TypeVar, cast

from toolbench.observability import setup_tracing
from toolbench.observability.setup_tracing import _load_laminar

P = ParamSpec("P")
R = TypeVar("R", int, Awaitable[int])


def _is_async_operation(operation: object) -> bool:
    return iscoroutinefunction(operation) or iscoroutinefunction(
        getattr(operation, "__call__", None)
    )


def _require_exit_code(result: object) -> int:
    if isinstance(result, int):
        return result
    if iscoroutine(result):
        result.close()
    raise TypeError("run_traced operations must return an int exit code")


def _run_sync(
    command: str,
    operation: Callable[P, int],
    *args: P.args,
    **kwargs: P.kwargs,
) -> int:
    Laminar = _load_laminar()
    result: int | None = None
    error: BaseException | None = None
    try:
        with Laminar.start_as_current_span(
            "toolbench.cli",
            tags=["toolbench", command],
        ):
            try:
                Laminar.set_trace_metadata({"command": command})
                result = _require_exit_code(operation(*args, **kwargs))
                Laminar.set_span_output({"exit_code": result})
            except BaseException as exc:
                error = exc
    finally:
        Laminar.flush()
    if error is not None:
        raise error.with_traceback(error.__traceback__)
    assert result is not None
    return result


async def _run_async(
    command: str,
    operation: Callable[P, Awaitable[int]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> int:
    Laminar = _load_laminar()
    result: int | None = None
    error: BaseException | None = None
    try:
        with Laminar.start_as_current_span(
            "toolbench.cli",
            tags=["toolbench", command],
        ):
            try:
                Laminar.set_trace_metadata({"command": command})
                result = _require_exit_code(await operation(*args, **kwargs))
                Laminar.set_span_output({"exit_code": result})
            except BaseException as exc:
                error = exc
    finally:
        Laminar.flush()
    if error is not None:
        raise error.with_traceback(error.__traceback__)
    assert result is not None
    return result


def run_traced(command: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate one CLI operation with an optional Laminar trace.

    Arguments and report contents are deliberately excluded: transcript paths,
    session identifiers, prompts, and outputs can contain private material.
    Sync operations must return an ``int``. Async functions and async callable
    objects must return an awaitable of ``int``. Synchronous wrappers that
    return awaitables are rejected instead of recording the coroutine object.
    The async wrapper keeps the span open until the decorated coroutine has
    finished, rather than tracing only the coroutine object's construction.
    """

    def decorator(operation: Callable[P, R]) -> Callable[P, R]:
        operation_object: object = operation
        if _is_async_operation(operation_object):
            async_operation = cast(Callable[P, Awaitable[int]], operation_object)

            @wraps(operation)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
                if not setup_tracing():
                    return _require_exit_code(await async_operation(*args, **kwargs))
                return await _run_async(command, async_operation, *args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        sync_operation = cast(Callable[P, int], operation_object)

        @wraps(operation)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
            if not setup_tracing():
                return _require_exit_code(sync_operation(*args, **kwargs))
            return _run_sync(command, sync_operation, *args, **kwargs)

        return cast(Callable[P, R], sync_wrapper)

    return decorator
