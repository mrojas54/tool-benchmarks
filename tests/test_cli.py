"""The `toolbench` console entry point: a dispatcher, not a parser.

Everything after the subcommand reaches the sub-CLI verbatim. These tests pin
the pass-through, the exit-code normalization (probe.main returns None), and
the argparse-style failure codes, in the suite's in-process main(argv) style
(tests/test_passive_cli.py)."""

import io
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from toolbench.cli import main


class HelpAndErrorTests(unittest.TestCase):
    def test_no_args_prints_usage_to_stderr_and_returns_2(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main([]), 2)
        self.assertIn("usage: toolbench", err.getvalue())

    def test_help_flag_prints_both_subcommands_to_stdout_and_returns_0(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("passive", out.getvalue())
        self.assertIn("probe", out.getvalue())

    def test_unknown_subcommand_is_named_on_stderr_and_returns_2(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["blorp"]), 2)
        self.assertIn("blorp", err.getvalue())


class DispatchTests(unittest.TestCase):
    def test_passive_gets_remaining_argv_verbatim_and_its_exit_code_returns(self) -> None:
        with unittest.mock.patch("toolbench.passive.main", return_value=3) as sub:
            self.assertEqual(main(["passive", "--agent", "all", "--all"]), 3)
        sub.assert_called_once_with(["--agent", "all", "--all"])

    def test_probe_gets_remaining_argv_verbatim_and_none_normalizes_to_0(self) -> None:
        with unittest.mock.patch("toolbench.probe.main", return_value=None) as sub:
            self.assertEqual(main(["probe", "--allow-seeded"]), 0)
        sub.assert_called_once_with(["--allow-seeded"])

    def test_a_leading_option_is_never_parsed_by_the_dispatcher(self) -> None:
        # A REMAINDER-based dispatcher drops or rejects a leading option
        # (python/cpython#61252); ours must hand it through untouched.
        with unittest.mock.patch("toolbench.probe.main") as sub:
            main(["probe", "--out", "x.md"])
        sub.assert_called_once_with(["--out", "x.md"])

    def test_sub_cli_help_flows_through_to_the_subcommands_own_parser(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["passive", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("usage", out.getvalue().lower())
