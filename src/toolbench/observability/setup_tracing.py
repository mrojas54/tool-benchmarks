"""Initialize optional, privacy-preserving tracing."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any, cast


def _load_laminar() -> Any:
    """Load the optional SDK without making it a static dependency."""
    return cast(Any, importlib.import_module("lmnr").Laminar)


def _project_api_key() -> str | None:
    """Read the project key from the environment or the SDK's ``.env`` lookup."""
    if project_api_key := os.environ.get("LMNR_PROJECT_API_KEY"):
        return project_api_key

    try:
        utils = importlib.import_module("lmnr.sdk.utils")
    except ModuleNotFoundError:
        return None

    from_env = cast(Callable[[str], str | None], getattr(utils, "from_env"))
    return from_env("LMNR_PROJECT_API_KEY")


def setup_tracing() -> bool:
    """Initialize Laminar when its SDK and project key are available.

    Returns ``True`` when tracing is ready. A missing optional dependency or
    project key leaves the standard CLI untraced instead of making it fail.
    """
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
