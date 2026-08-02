"""Optional, privacy-preserving Laminar tracing for short-lived CLI commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from inspect import iscoroutine, iscoroutinefunction
from typing import Any, ParamSpec, TypeVar, cast

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


def _system_exit_code(error: SystemExit) -> int:
    if error.code is None:
        return 0
    return error.code if isinstance(error.code, int) else 1


def _tracing_is_available() -> bool:
    try:
        return setup_tracing()
    except BaseException:
        return False


def _load_laminar_best_effort() -> Any | None:
    try:
        return _load_laminar()
    except BaseException:
        return None


def _safe_report(laminar: Any, method_name: str, value: object) -> None:
    try:
        getattr(laminar, method_name)(value)
    except BaseException:
        pass


def _safe_flush(laminar: Any) -> None:
    try:
        laminar.flush()
    except BaseException:
        pass


def _invoke_sync(
    operation: Callable[P, int],
    *args: P.args,
    **kwargs: P.kwargs,
) -> tuple[int | None, BaseException | None]:
    try:
        return _require_exit_code(operation(*args, **kwargs)), None
    except SystemExit as exc:
        return _system_exit_code(exc), exc
    except BaseException as exc:
        return None, exc


async def _invoke_async(
    operation: Callable[P, Awaitable[int]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> tuple[int | None, BaseException | None]:
    try:
        return _require_exit_code(await operation(*args, **kwargs)), None
    except SystemExit as exc:
        return _system_exit_code(exc), exc
    except BaseException as exc:
        return None, exc


def _run_sync(
    command: str,
    operation: Callable[P, int],
    *args: P.args,
    **kwargs: P.kwargs,
) -> int:
    Laminar = _load_laminar_best_effort()
    if Laminar is None:
        return _require_exit_code(operation(*args, **kwargs))

    operation_started = False
    result: int | None = None
    error: BaseException | None = None
    try:
        with Laminar.start_as_current_span(
            "toolbench.cli",
            tags=["toolbench", command],
        ):
            operation_started = True
            _safe_report(Laminar, "set_trace_metadata", {"command": command})
            result, error = _invoke_sync(operation, *args, **kwargs)
            if result is not None:
                _safe_report(Laminar, "set_span_output", {"exit_code": result})
    except BaseException:
        if not operation_started:
            return _require_exit_code(operation(*args, **kwargs))
    finally:
        _safe_flush(Laminar)
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
    Laminar = _load_laminar_best_effort()
    if Laminar is None:
        return _require_exit_code(await operation(*args, **kwargs))

    operation_started = False
    result: int | None = None
    error: BaseException | None = None
    try:
        with Laminar.start_as_current_span(
            "toolbench.cli",
            tags=["toolbench", command],
        ):
            operation_started = True
            _safe_report(Laminar, "set_trace_metadata", {"command": command})
            result, error = await _invoke_async(operation, *args, **kwargs)
            if result is not None:
                _safe_report(Laminar, "set_span_output", {"exit_code": result})
    except BaseException:
        if not operation_started:
            return _require_exit_code(await operation(*args, **kwargs))
    finally:
        _safe_flush(Laminar)
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
                if not _tracing_is_available():
                    return _require_exit_code(await async_operation(*args, **kwargs))
                return await _run_async(command, async_operation, *args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        sync_operation = cast(Callable[P, int], operation_object)

        @wraps(operation)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
            if not _tracing_is_available():
                return _require_exit_code(sync_operation(*args, **kwargs))
            return _run_sync(command, sync_operation, *args, **kwargs)

        return cast(Callable[P, R], sync_wrapper)

    return decorator
