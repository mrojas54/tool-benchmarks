"""Unified `toolbench` console entry point.

`toolbench passive ...`, `toolbench probe ...` and `toolbench worktrees ...`
hand their remaining argv to the sub-CLIs (`toolbench.passive.main`,
`toolbench.probe.main`, `toolbench.worktrees.main`) VERBATIM. The dispatcher
parses nothing beyond the subcommand name: the sub-CLIs own their flags,
including `--help`. argparse's REMAINDER is avoided on purpose -- it mishandles
a leading option (python/cpython#61252), which is the shape of every real
invocation (`toolbench passive --agent all`).

Imports are lazy per subcommand: `toolbench.probe` imports `toolbench.complex`,
which loads the DEFECTS fixtures at import time -- a broken fixture must fail
`toolbench probe`, never `toolbench passive`, `toolbench worktrees` or `--help`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

_HELP = """\
usage: toolbench <command> [options]

Tool-benchmark harness.

commands:
  passive   Passive tool-usage analyzer (delegates to toolbench.passive).
  probe     Active tool-vs-Bash probe comparison (delegates to toolbench.probe).
  worktrees Linked git worktree inventory with a reclaim verdict per tree.

Run `toolbench <command> --help` for that command's options.
"""


def _run_command(
    command: str,
    operation: Callable[[], int],
    *,
    trace: bool,
) -> int:
    if trace:
        from toolbench.tracing import run_traced

        operation = run_traced(command)(operation)
    return operation()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(_HELP, file=sys.stderr, end="")
        return 2
    if args[0] in ("-h", "--help"):
        print(_HELP, end="")
        return 0
    command, rest = args[0], args[1:]
    if command == "passive":
        from toolbench import passive

        def operation() -> int:
            return passive.main(rest)

        return _run_command(command, operation, trace=argv is None)
    if command == "probe":
        from toolbench import probe

        def run_probe() -> int:
            probe.main(rest)  # returns None; success is "did not raise"
            return 0

        return _run_command(command, run_probe, trace=argv is None)
    if command == "worktrees":
        from toolbench import worktrees

        def run_worktrees() -> int:
            return worktrees.main(rest)

        return _run_command(command, run_worktrees, trace=argv is None)
    print(f"toolbench: unknown command {command!r}\n\n{_HELP}", file=sys.stderr, end="")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
