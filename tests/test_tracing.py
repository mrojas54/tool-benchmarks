"""Tests for the optional tracing decorator."""

from __future__ import annotations

import os
import sys
import types
import unittest
import unittest.mock
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Literal

from toolbench.tracing import run_traced


class TracingDecoratorTests(unittest.IsolatedAsyncioTestCase):
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
