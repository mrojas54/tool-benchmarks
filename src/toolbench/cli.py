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
from collections.abc import Sequence

_HELP = """\
usage: toolbench <command> [options]

Tool-benchmark harness.

commands:
  passive   Passive tool-usage analyzer (delegates to toolbench.passive).
  probe     Active tool-vs-Bash probe comparison (delegates to toolbench.probe).
  worktrees Linked git worktree inventory with a reclaim verdict per tree.

Run `toolbench <command> --help` for that command's options.
"""


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
        from toolbench.tracing import run_traced

        def operation() -> int:
            return passive.main(rest)

        return run_traced(command, operation) if argv is None else operation()
    if command == "probe":
        from toolbench import probe
        from toolbench.tracing import run_traced

        def run_probe() -> int:
            probe.main(rest)  # returns None; success is "did not raise"
            return 0

        return run_traced(command, run_probe) if argv is None else run_probe()
    if command == "worktrees":
        from toolbench import worktrees

        return worktrees.main(rest)
    print(f"toolbench: unknown command {command!r}\n\n{_HELP}", file=sys.stderr, end="")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
