"""Tests for the optional tracing decorator."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
import types
import unittest
import unittest.mock
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from threading import Event
from typing import Literal

from toolbench.tracing import _TracingState, run_traced


class TracingDecoratorTests(unittest.IsolatedAsyncioTestCase):
    def test_setup_is_initialized_once_under_concurrent_calls(self) -> None:
        setup_started = Event()
        release_setup = Event()

        def setup() -> bool:
            setup_started.set()
            self.assertTrue(release_setup.wait(timeout=2))
            return True

        state = _TracingState()
        with (
            unittest.mock.patch(
                "toolbench.tracing.setup_tracing", side_effect=setup
            ) as setup_tracing,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(state.is_available_best_effort)
            self.assertTrue(setup_started.wait(timeout=2))
            second = executor.submit(state.is_available_best_effort)
            release_setup.set()
            self.assertTrue(first.result(timeout=2))
            self.assertTrue(second.result(timeout=2))

        setup_tracing.assert_called_once_with()

    def test_successful_setup_is_reused_by_a_traced_operation(self) -> None:
        calls: list[str] = []

        class RecordingLaminar:
            @classmethod
            @contextmanager
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> Iterator[None]:
                del cls, name, tags
                yield

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                del cls, metadata

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                del cls, output

            @classmethod
            def flush(cls) -> None:
                del cls

        with (
            unittest.mock.patch(
                "toolbench.tracing.setup_tracing", return_value=True
            ) as setup_tracing,
            unittest.mock.patch(
                "toolbench.tracing._load_laminar", return_value=RecordingLaminar
            ),
        ):

            @run_traced("probe")
            def operation() -> int:
                calls.append("operation")
                return 7

            self.assertEqual(operation(), 7)
            self.assertEqual(operation(), 7)

        setup_tracing.assert_called_once_with()
        self.assertEqual(calls, ["operation", "operation"])

    def test_failed_setup_is_retried_until_tracing_is_available(self) -> None:
        with (
            unittest.mock.patch(
                "toolbench.tracing.setup_tracing", side_effect=[False, True]
            ) as setup_tracing,
        ):

            @run_traced("probe")
            def operation() -> int:
                return 11

            self.assertEqual(operation(), 11)
            self.assertEqual(operation(), 11)

        self.assertEqual(setup_tracing.call_count, 2)

    async def test_async_operation_stays_inside_the_trace_until_it_finishes(
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
                yield
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

        fake_lmnr = types.ModuleType("lmnr")
        fake_lmnr.Laminar = RecordingLaminar  # type: ignore[attr-defined]

        with (
            unittest.mock.patch.dict(
                os.environ, {"LMNR_PROJECT_API_KEY": "test-project-key"}
            ),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
        ):

            @run_traced("probe")
            async def operation() -> int:
                events.append(("operation",))
                return 7

            self.assertEqual(await operation(), 7)

        self.assertEqual(
            events,
            [
                ("initialize", "test-project-key", frozenset()),
                ("start", "toolbench.cli", ("toolbench", "probe")),
                ("metadata", {"command": "probe"}),
                ("operation",),
                ("output", {"exit_code": 7}),
                ("end",),
                ("flush",),
            ],
        )

    def test_operation_failure_closes_the_span_before_reraising(self) -> None:
        events: list[tuple[object, ...]] = []
        private_path = "/private/transcripts/member-session.jsonl"

        class RecordingSpan:
            def __enter__(self) -> None:
                events.append(("start",))

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: TracebackType | None,
            ) -> Literal[False]:
                events.append(("end", exc_type))
                return False

        class RecordingLaminar:
            @classmethod
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> RecordingSpan:
                events.append(("span", name, tuple(tags)))
                return RecordingSpan()

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                events.append(("metadata", metadata))

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                events.append(("output", output))

            @classmethod
            def flush(cls) -> None:
                events.append(("flush",))

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar", return_value=RecordingLaminar
            ),
        ):

            @run_traced("probe")
            def operation() -> int:
                raise FileNotFoundError(private_path)

            with self.assertRaisesRegex(FileNotFoundError, private_path):
                operation()

        self.assertEqual(
            events,
            [
                ("span", "toolbench.cli", ("toolbench", "probe")),
                ("start",),
                ("metadata", {"command": "probe"}),
                ("end", None),
                ("flush",),
            ],
        )
        self.assertNotIn(private_path, repr(events))

    def test_span_setup_failure_does_not_block_the_operation(self) -> None:
        events: list[str] = []

        class BrokenLaminar:
            @classmethod
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> object:
                del name, tags
                raise RuntimeError("span setup failed")

            @classmethod
            def flush(cls) -> None:
                raise RuntimeError("flush failed")

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar", return_value=BrokenLaminar
            ),
        ):

            @run_traced("probe")
            def operation() -> int:
                events.append("operation")
                return 11

            self.assertEqual(operation(), 11)

        self.assertEqual(events, ["operation"])

    def test_reporting_failures_preserve_result_and_operation_error(self) -> None:
        events: list[str] = []

        class ReportingLaminar:
            @classmethod
            @contextmanager
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> Iterator[None]:
                del name, tags
                yield

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                del metadata
                raise RuntimeError("metadata failed")

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                del output
                raise RuntimeError("output failed")

            @classmethod
            def flush(cls) -> None:
                raise RuntimeError("flush failed")

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar", return_value=ReportingLaminar
            ),
        ):

            @run_traced("probe")
            def successful_operation() -> int:
                events.append("success")
                return 13

            @run_traced("probe")
            def failing_operation() -> int:
                events.append("failure")
                raise RuntimeError("operation failed")

            self.assertEqual(successful_operation(), 13)
            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                failing_operation()

        self.assertEqual(events, ["success", "failure"])

    def test_keyboard_interrupts_from_tracing_are_not_swallowed(self) -> None:
        events: list[str] = []

        class InterruptingSpanLaminar:
            @classmethod
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> object:
                del name, tags
                raise KeyboardInterrupt()

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar",
                return_value=InterruptingSpanLaminar,
            ),
        ):

            @run_traced("probe")
            def operation_before_start() -> int:
                events.append("before")
                return 17

            with self.assertRaises(KeyboardInterrupt):
                operation_before_start()

        class InterruptingFlushLaminar:
            @classmethod
            @contextmanager
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> Iterator[None]:
                del name, tags
                yield

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                del metadata

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                del output

            @classmethod
            def flush(cls) -> None:
                raise KeyboardInterrupt()

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar",
                return_value=InterruptingFlushLaminar,
            ),
        ):

            @run_traced("probe")
            def operation_before_flush() -> int:
                events.append("after")
                return 19

            with self.assertRaises(KeyboardInterrupt):
                operation_before_flush()

        self.assertEqual(events, ["after"])

    def test_system_exit_from_tracing_does_not_block_the_operation(self) -> None:
        events: list[str] = []

        class ExitFromSpanLaminar:
            @classmethod
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> object:
                del name, tags
                raise SystemExit(23)

            @classmethod
            def flush(cls) -> None:
                raise SystemExit(29)

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar",
                return_value=ExitFromSpanLaminar,
            ),
        ):

            @run_traced("probe")
            def operation_after_span_failure() -> int:
                events.append("span")
                return 31

            self.assertEqual(operation_after_span_failure(), 31)

        class ExitFromFlushLaminar:
            @classmethod
            @contextmanager
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> Iterator[None]:
                del name, tags
                yield

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                del metadata

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                del output

            @classmethod
            def flush(cls) -> None:
                raise SystemExit(37)

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar",
                return_value=ExitFromFlushLaminar,
            ),
        ):

            @run_traced("probe")
            def operation_before_flush_exit() -> int:
                events.append("flush")
                return 41

            self.assertEqual(operation_before_flush_exit(), 41)

        self.assertEqual(events, ["span", "flush"])

    def test_system_exit_records_a_safe_exit_code_before_reraising(self) -> None:
        events: list[tuple[object, ...]] = []

        class RecordingSpan:
            def __enter__(self) -> None:
                pass

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: TracebackType | None,
            ) -> Literal[False]:
                events.append(("end", exc_type))
                return False

        class RecordingLaminar:
            @classmethod
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> RecordingSpan:
                events.append(("span", name, tuple(tags)))
                return RecordingSpan()

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                events.append(("metadata", metadata))

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                events.append(("output", output))

            @classmethod
            def flush(cls) -> None:
                events.append(("flush",))

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar", return_value=RecordingLaminar
            ),
        ):
            for code in (0, 2):
                with self.subTest(code=code):

                    @run_traced("probe")
                    def operation() -> int:
                        raise SystemExit(code)

                    with self.assertRaises(SystemExit) as raised:
                        operation()
                    self.assertEqual(raised.exception.code, code)

        self.assertEqual(
            events,
            [
                ("span", "toolbench.cli", ("toolbench", "probe")),
                ("metadata", {"command": "probe"}),
                ("output", {"exit_code": 0}),
                ("end", None),
                ("flush",),
                ("span", "toolbench.cli", ("toolbench", "probe")),
                ("metadata", {"command": "probe"}),
                ("output", {"exit_code": 2}),
                ("end", None),
                ("flush",),
            ],
        )

    async def test_async_callable_object_stays_inside_the_trace(self) -> None:
        events: list[str] = []

        class RecordingSpan:
            def __enter__(self) -> None:
                events.append("start")

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc_value: BaseException | None,
                traceback: TracebackType | None,
            ) -> Literal[False]:
                events.append("end")
                return False

        class RecordingLaminar:
            @classmethod
            def start_as_current_span(
                cls, name: str, *, tags: list[str]
            ) -> RecordingSpan:
                return RecordingSpan()

            @classmethod
            def set_trace_metadata(cls, metadata: dict[str, str]) -> None:
                events.append("metadata")

            @classmethod
            def set_span_output(cls, output: dict[str, int]) -> None:
                events.append("output")

            @classmethod
            def flush(cls) -> None:
                events.append("flush")

        class AsyncOperation:
            async def __call__(self) -> int:
                events.append("operation")
                return 3

        with (
            unittest.mock.patch("toolbench.tracing.setup_tracing", return_value=True),
            unittest.mock.patch(
                "toolbench.tracing._load_laminar", return_value=RecordingLaminar
            ),
        ):
            operation = run_traced("probe")(AsyncOperation())
            self.assertEqual(await operation(), 3)

        self.assertEqual(events, ["start", "metadata", "operation", "output", "end", "flush"])
