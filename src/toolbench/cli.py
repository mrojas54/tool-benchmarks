"""Unified `toolbench` console entry point.

`toolbench passive ...` and `toolbench probe ...` hand their remaining argv to
the existing sub-CLIs (`toolbench.passive.main`, `toolbench.probe.main`)
VERBATIM. The dispatcher parses nothing beyond the subcommand name: the
sub-CLIs own their flags, including `--help`. argparse's REMAINDER is avoided
on purpose -- it mishandles a leading option (python/cpython#61252), which is
the shape of every real invocation (`toolbench passive --agent all`).

Imports are lazy per subcommand: `toolbench.probe` imports `toolbench.complex`,
which loads the DEFECTS fixtures at import time -- a broken fixture must fail
`toolbench probe`, never `toolbench passive` or `--help`.
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

        return passive.main(rest)
    if command == "probe":
        from toolbench import probe

        probe.main(rest)  # returns None; success is "did not raise"
        return 0
    print(f"toolbench: unknown command {command!r}\n\n{_HELP}", file=sys.stderr, end="")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
