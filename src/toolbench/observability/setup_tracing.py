"""Initialize optional, privacy-preserving tracing."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any, cast

_STABLE_SERVICE_NAME = "toolbench"
_SDK_CONTEXT_ENV_VARS = (
    "LMNR_TRACE_METADATA",
    "LMNR_SPAN_CONTEXT",
    "LMNR_DEBUG",
    "LMNR_DEBUG_SESSION_ID",
    "LMNR_DEBUG_REPLAY_TRACE_ID",
    "LMNR_DEBUG_CACHE_UNTIL",
)
_SDK_ENVIRONMENT_LOCK = RLock()


@contextmanager
def _sanitized_sdk_environment() -> Iterator[None]:
    """Keep SDK-derived resource and context data within the CLI boundary."""
    with _SDK_ENVIRONMENT_LOCK:
        original_argv = sys.argv[:]
        removed_environment = {
            name: os.environ.pop(name)
            for name in _SDK_CONTEXT_ENV_VARS
            if name in os.environ
        }
        sys.argv[:] = [_STABLE_SERVICE_NAME]
        try:
            yield
        finally:
            sys.argv[:] = original_argv
            for name in _SDK_CONTEXT_ENV_VARS:
                if name in removed_environment:
                    os.environ[name] = removed_environment[name]
                else:
                    os.environ.pop(name, None)


def _is_missing_module(exc: ModuleNotFoundError, name: str) -> bool:
    """Return whether `name` itself is missing, not something *it* imports.

    ``importlib.import_module`` raises the same exception type whether the
    requested module is absent or one of its own internal imports is, so
    every optional-SDK import site needs this check to avoid mistaking a
    broken install for a merely-not-installed optional dependency.
    """
    return exc.name is not None and (exc.name == name or name.startswith(f"{exc.name}."))


def _import_optional_lmnr_module(name: str) -> Any | None:
    """Import an lmnr submodule, or ``None`` when it genuinely isn't installed."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if not _is_missing_module(exc, name):
            raise
        return None


def load_laminar() -> Any | None:
    """Load the optional SDK's ``Laminar`` class without a static dependency.

    Shared by ``setup_tracing`` (to initialize the SDK) and
    ``toolbench.tracing`` (to obtain the class for span creation). Returns
    ``None`` when the optional ``lmnr`` package itself is not installed.
    """
    lmnr = _import_optional_lmnr_module("lmnr")
    if lmnr is None:
        return None
    return cast(Any, lmnr.Laminar)


def _project_api_key() -> str | None:
    """Read the project key from the environment or SDK lookup."""
    if project_api_key := os.getenv("LMNR_PROJECT_API_KEY"):
        return project_api_key

    utils = _import_optional_lmnr_module("lmnr.sdk.utils")
    if utils is None:
        return None

    from_env = getattr(utils, "from_env", None)
    if from_env is None:
        return None
    return cast(Callable[[str], str | None], from_env)("LMNR_PROJECT_API_KEY")


def setup_tracing() -> bool:
    """Initialize Laminar when its SDK and project key are available.

    Returns ``True`` when tracing is ready. A missing optional dependency or
    project key leaves the standard CLI untraced instead of making it fail.
    """
    with _sanitized_sdk_environment():
        project_api_key = _project_api_key()
        if not project_api_key:
            return False

        Laminar = load_laminar()
        if Laminar is None:
            return False

        try:
            Laminar.initialize(
                project_api_key=project_api_key,
                instruments=set(),
            )
        except ValueError:
            # Keep tracing optional if the SDK rejects the configured key.
            return False

    return True
