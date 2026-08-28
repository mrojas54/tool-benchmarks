"""The `toolbench` console entry point: a dispatcher, not a parser.

Everything after the subcommand reaches the sub-CLI verbatim. These tests pin
the pass-through, the exit-code normalization (probe.main returns None), and
the argparse-style failure codes, in the suite's in-process main(argv) style
(tests/test_passive.py)."""

import builtins
import importlib
import io
import os
import sys
import types
import unittest
import unittest.mock
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout

from tests.fakes import make_module
from toolbench.cli import main


class HelpAndErrorTests(unittest.TestCase):
    def test_no_args_prints_usage_to_stderr_and_returns_2(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main([]), 2)
        self.assertIn("usage: toolbench", err.getvalue())

    def test_help_flag_prints_every_subcommand_to_stdout_and_returns_0(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("passive", out.getvalue())
        self.assertIn("probe", out.getvalue())
        self.assertIn("worktrees", out.getvalue())

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

    def test_worktrees_gets_remaining_argv_verbatim_and_its_exit_code_returns(
        self,
    ) -> None:
        with unittest.mock.patch("toolbench.worktrees.main", return_value=0) as sub:
            self.assertEqual(main(["worktrees"]), 0)
        sub.assert_called_once_with([])

    def test_worktrees_is_imported_lazily_so_a_broken_probe_fixture_cannot_break_it(
        self,
    ) -> None:
        # The dispatcher's documented convention: `toolbench.probe` loads the
        # DEFECTS fixtures at import time, and that must never be on the path to
        # a worktree report.
        with (
            unittest.mock.patch.dict("sys.modules", {"toolbench.probe": None}),
            unittest.mock.patch("toolbench.worktrees.main", return_value=0) as sub,
        ):
            self.assertEqual(main(["worktrees", "--help"]), 0)
        sub.assert_called_once_with(["--help"])

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

    def test_probe_dispatch_emits_a_private_laminar_trace_and_flushes(self) -> None:
        events: list[tuple[object, ...]] = []

        class RecordingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str, instruments: set[object]
            ) -> None:
                events.append(("initialize", project_api_key, frozenset(instruments)))

            @classmethod
            @contextmanager
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> Iterator[None]:
                events.append(("start", name, tuple(tags)))
                try:
                    yield
                finally:
                    events.append(("end",))

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                events.append(("metadata", metadata))

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                events.append(("output", output))

            @classmethod
            def flush(cls) -> None:
                events.append(("flush",))

        fake_lmnr = make_module("lmnr", Laminar=RecordingLaminar)
        private_session = "/private/archive/member-session.jsonl"

        with (
            unittest.mock.patch.dict(
                os.environ,
                {
                    "LMNR_PROJECT_API_KEY": "test-project-key",
                    "TOOLBENCH_TRACING": "1",
                },
            ),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
            unittest.mock.patch.object(
                sys,
                "argv",
                ["toolbench", "probe", "--session", private_session],
            ),
            unittest.mock.patch("toolbench.probe.main", return_value=None),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(
            events,
            [
                ("initialize", "test-project-key", frozenset()),
                ("start", "toolbench.cli", ("toolbench", "probe")),
                ("metadata", {"command": "probe"}),
                ("output", {"exit_code": 0}),
                ("end",),
                ("flush",),
            ],
        )
        self.assertNotIn(private_session, repr(events))

    def test_probe_dispatch_without_optional_laminar_still_runs(self) -> None:
        stderr = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.dict(sys.modules, {"lmnr": None}),
            unittest.mock.patch.object(sys, "argv", ["toolbench", "probe"]),
            unittest.mock.patch("toolbench.probe.main", return_value=None),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(stderr.getvalue(), "")

    def test_probe_failure_flushes_the_closed_span_and_preserves_the_error(
        self,
    ) -> None:
        events: list[tuple[object, ...]] = []

        class RecordingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str, instruments: set[object]
            ) -> None:
                events.append(("initialize", project_api_key, frozenset(instruments)))

            @classmethod
            @contextmanager
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> Iterator[None]:
                events.append(("start", name, tuple(tags)))
                try:
                    yield
                finally:
                    events.append(("end",))

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                events.append(("metadata", metadata))

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                events.append(("output", output))

            @classmethod
            def flush(cls) -> None:
                events.append(("flush",))

        fake_lmnr = make_module("lmnr", Laminar=RecordingLaminar)
        private_session = "/private/archive/member-session.jsonl"

        with (
            unittest.mock.patch.dict(
                os.environ,
                {
                    "LMNR_PROJECT_API_KEY": "test-project-key",
                    "TOOLBENCH_TRACING": "1",
                },
            ),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
            unittest.mock.patch.object(
                sys,
                "argv",
                ["toolbench", "probe", "--session", private_session],
            ),
            unittest.mock.patch(
                "toolbench.probe.main",
                side_effect=FileNotFoundError(private_session),
            ),
            self.assertRaisesRegex(FileNotFoundError, private_session),
        ):
            main()

        self.assertEqual(
            events,
            [
                ("initialize", "test-project-key", frozenset()),
                ("start", "toolbench.cli", ("toolbench", "probe")),
                ("metadata", {"command": "probe"}),
                ("end",),
                ("flush",),
            ],
        )
        self.assertNotIn(private_session, repr(events))

    def test_programmatic_dispatch_does_not_emit_a_laminar_trace(self) -> None:
        with (
            unittest.mock.patch(
                "toolbench.tracing.run_traced",
            ) as trace,
            unittest.mock.patch("toolbench.passive.main", return_value=0),
            unittest.mock.patch("toolbench.probe.main", return_value=None),
            unittest.mock.patch("toolbench.worktrees.main", return_value=0),
        ):
            for command in ("passive", "probe", "worktrees"):
                with self.subTest(command=command):
                    self.assertEqual(main([command]), 0)

        trace.assert_not_called()

    def test_real_cli_tracing_requires_explicit_opt_in(self) -> None:
        with (
            unittest.mock.patch.dict(
                os.environ,
                {"LMNR_PROJECT_API_KEY": "configured-but-not-enabled"},
                clear=True,
            ),
            unittest.mock.patch.object(sys, "argv", ["toolbench", "probe"]),
            unittest.mock.patch("toolbench.probe.main", return_value=None),
            unittest.mock.patch(
                "toolbench.tracing.run_traced",
                side_effect=lambda _command: lambda operation: operation,
            ) as trace,
        ):
            self.assertEqual(main(), 0)

        trace.assert_not_called()

    def test_normal_passive_and_probe_do_not_import_optional_tracers(self) -> None:
        attempted: list[str] = []
        real_import = builtins.__import__
        real_import_module = importlib.import_module

        def guarded_import(
            name: str,
            globals_: Mapping[str, object] | None = None,
            locals_: Mapping[str, object] | None = None,
            fromlist: Sequence[str] | None = (),
            level: int = 0,
        ) -> types.ModuleType:
            if name.partition(".")[0] in {"lmnr", "logfire"}:
                attempted.append(name)
                raise ModuleNotFoundError(name=name)
            return real_import(name, globals_, locals_, fromlist, level)

        def guarded_import_module(
            name: str, package: str | None = None
        ) -> types.ModuleType:
            if name.partition(".")[0] in {"lmnr", "logfire"}:
                attempted.append(name)
                raise ModuleNotFoundError(name=name)
            return real_import_module(name, package)

        for command, result in (("passive", 0), ("probe", None)):
            with (
                unittest.mock.patch.dict(
                    os.environ,
                    {"LMNR_PROJECT_API_KEY": "configured-but-not-enabled"},
                    clear=True,
                ),
                unittest.mock.patch.object(sys, "argv", ["toolbench", command]),
                unittest.mock.patch(
                    f"toolbench.{command}.main", return_value=result
                ),
                unittest.mock.patch.object(
                    builtins, "__import__", side_effect=guarded_import
                ),
                unittest.mock.patch.object(
                    importlib, "import_module", side_effect=guarded_import_module
                ),
            ):
                self.assertEqual(main(), 0)

        self.assertEqual(attempted, [])

    def test_real_worktree_hook_dispatch_does_not_emit_a_laminar_trace(self) -> None:
        with (
            unittest.mock.patch(
                "toolbench.tracing.run_traced",
            ) as trace,
            unittest.mock.patch.object(
                sys,
                "argv",
                ["toolbench", "worktrees", "--hook"],
            ),
            unittest.mock.patch("toolbench.worktrees.main", return_value=0),
        ):
            self.assertEqual(main(), 0)

        trace.assert_not_called()
