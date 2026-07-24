"""Initialize optional, privacy-preserving tracing."""

from __future__ import annotations

import os


def setup_tracing() -> bool:
    """Initialize Laminar when its SDK and project key are available.

    Returns ``True`` when tracing is ready. A missing optional dependency or
    project key leaves the standard CLI untraced instead of making it fail.
    """
    try:
        from lmnr import Laminar
    except ModuleNotFoundError as exc:
        if exc.name != "lmnr":
            raise
        return False

    try:
        Laminar.initialize(
            project_api_key=os.environ.get("LMNR_PROJECT_API_KEY"),
            instruments=set(),
        )
    except ValueError:
        # The SDK also checks a local .env file, so no explicit key here can
        # still succeed. If neither source has a key, tracing remains optional.
        return False

    return True
