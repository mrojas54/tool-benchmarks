"""Adapter registry (TB-13). Stdlib only.

Exists to break an import cycle: `hermes.py` imports `SessionAdapter` from
`adapters.py`, so `adapters.py` cannot import `HermesAdapter`. This module imports
both and is imported by neither.

Order is significant. Source-keyed adapters get first refusal; `ComposedAdapter`
is the terminal fallback and claims everything. Adding an agent means adding an
entry here, never editing a dispatcher.
"""

from __future__ import annotations

from toolbench.adapters import ComposedAdapter, SessionAdapter
from toolbench.hermes import HermesAdapter
from toolbench.sources import Runner, SessionRef


def pick_adapter(ref: SessionRef, runner: Runner | None = None) -> SessionAdapter:
    """Return the first adapter that claims `ref`. Never returns None."""
    adapters: tuple[SessionAdapter, ...] = (HermesAdapter(), ComposedAdapter(runner))
    for adapter in adapters:
        if adapter.claims(ref):
            return adapter
    raise AssertionError("ComposedAdapter claims everything; this is unreachable")
