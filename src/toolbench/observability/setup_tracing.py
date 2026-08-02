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
_DOTENV_LOADER: Callable[..., object] | None
try:
    _DOTENV_LOADER = cast(
        Callable[..., object],
        getattr(importlib.import_module("dotenv"), "load_dotenv"),
    )
except ModuleNotFoundError as exc:
    if exc.name != "dotenv":
        raise
    _DOTENV_LOADER = None


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


def _load_laminar() -> Any:
    """Load the optional SDK without making it a static dependency."""
    return cast(Any, importlib.import_module("lmnr").Laminar)


def _project_api_key() -> str | None:
    """Load local dotenv configuration, then read the project key."""
    _load_dotenv()
    if project_api_key := os.getenv("LMNR_PROJECT_API_KEY"):
        return project_api_key

    try:
        utils = importlib.import_module("lmnr.sdk.utils")
    except ModuleNotFoundError:
        return None

    from_env = cast(Callable[[str], str | None], getattr(utils, "from_env"))
    return from_env("LMNR_PROJECT_API_KEY")


def _load_dotenv() -> None:
    """Load the local ``.env`` when python-dotenv is installed."""
    if _DOTENV_LOADER is not None:
        _DOTENV_LOADER(dotenv_path=".env")


def _tracing_configured() -> bool:
    """Return whether a Laminar project key is configured without exposing it."""
    with _sanitized_sdk_environment():
        try:
            return bool(_project_api_key())
        except (Exception, SystemExit):
            return False


def setup_tracing() -> bool:
    """Initialize Laminar when its SDK and project key are available.

    Returns ``True`` when tracing is ready. A missing optional dependency or
    project key leaves the standard CLI untraced instead of making it fail.
    """
    with _sanitized_sdk_environment():
        project_api_key = _project_api_key()
        if not project_api_key:
            return False

        try:
            Laminar = _load_laminar()
        except ModuleNotFoundError as exc:
            if exc.name != "lmnr":
                raise
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
