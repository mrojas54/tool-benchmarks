# TB-36: _probe_agentsview hand-assembles a session-list argv outside _list_argv(), the sole-builder invariant TB-33 established

WHERE: toolbench/sources.py:555-563 (_probe_agentsview) vs toolbench/sources.py:211-235 (_list_argv).

WHAT: TB-33 made _list_argv the ONE place a agentsview session list argv is built, by design -- its docstring states the invariant: the census denominators and the discovery numerators must carry identical filters or they describe different populations. Routing both through here makes that invariant structural instead of a comment two functions apart.

_probe_agentsview is the one remaining call site that ignores it, hand-rolling:

    [agentsview, session, list, --json, --limit, 1]

HARMLESS TODAY -- and the ticket should say so plainly. It is an availability probe only (S10 fallback detection): it asks whether the binary runs and exits 0, discards the payload entirely, and carries no agent / project / since / includes filters. It cannot desync a denominator from a numerator because it never feeds either.

WHY FILE IT: the invariant is only as strong as its exceptions. A future edit that reaches for the probe as a template, or that adds a filter to it, silently reintroduces the TB-30 and TB-31 class of bug (probe listing and census describing different populations). The cost of closing it is a few lines.

FIX SKETCH: route the probe through _list_argv with limit=1 and an explicit includes tuple, OR leave it hand-built and add a comment naming it as the deliberate, filter-free exception. Either closes the gap; the first is structural, the second is cheap. Note sources.py:313 already calls _list_argv with limit=1, so the shape exists.

TEST NOTE: tests/test_passive_cli.py:291 asserts call [0] is the _probe_agentsview availability probe -- any argv change must keep that ordering assumption intact.
