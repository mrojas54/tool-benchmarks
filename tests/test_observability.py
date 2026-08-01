"""Tests for optional observability setup."""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
import unittest.mock
from pathlib import Path


class SetupTracingTests(unittest.TestCase):
    def test_setup_tracing_stays_disabled_without_a_project_key(self) -> None:
        events: list[tuple[object, ...]] = []

        class RecordingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str | None, instruments: set[object]
            ) -> None:
                events.append(("initialize", project_api_key, frozenset(instruments)))

        fake_lmnr = types.ModuleType("lmnr")
        fake_lmnr.Laminar = RecordingLaminar  # type: ignore[attr-defined]

        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
        ):
            module = importlib.import_module(
                "toolbench.observability.setup_tracing"
            )
            self.assertFalse(module.setup_tracing())

        self.assertEqual(events, [])

    def test_setup_tracing_initializes_laminar_without_auto_instruments(self) -> None:
        module_path = (
            Path(__file__).parents[1]
            / "src"
            / "toolbench"
            / "observability"
            / "setup_tracing.py"
        )
        self.assertTrue(module_path.is_file(), "setup_tracing.py must exist")

        events: list[tuple[object, ...]] = []

        class RecordingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str, instruments: set[object]
            ) -> None:
                events.append(("initialize", project_api_key, frozenset(instruments)))

        fake_lmnr = types.ModuleType("lmnr")
        fake_lmnr.Laminar = RecordingLaminar  # type: ignore[attr-defined]

        with (
            unittest.mock.patch.dict(
                os.environ, {"LMNR_PROJECT_API_KEY": "test-project-key"}
            ),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
        ):
            module = importlib.import_module(
                "toolbench.observability.setup_tracing"
            )
            self.assertTrue(module.setup_tracing())

        self.assertEqual(
            events,
            [("initialize", "test-project-key", frozenset())],
        )

    def test_setup_tracing_stays_disabled_without_the_optional_sdk(self) -> None:
        fake_missing_sdk: types.ModuleType | None = None

        with (
            unittest.mock.patch.dict(
                os.environ, {"LMNR_PROJECT_API_KEY": "test-project-key"}, clear=True
            ),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_missing_sdk}),
        ):
            module = importlib.import_module(
                "toolbench.observability.setup_tracing"
            )
            self.assertFalse(module.setup_tracing())

    def test_setup_tracing_stays_disabled_when_laminar_rejects_the_key(self) -> None:
        events: list[tuple[object, ...]] = []

        class RejectingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str, instruments: set[object]
            ) -> None:
                events.append(("initialize", project_api_key, frozenset(instruments)))
                raise ValueError("invalid project key")

        fake_lmnr = types.ModuleType("lmnr")
        fake_lmnr.Laminar = RejectingLaminar  # type: ignore[attr-defined]

        with (
            unittest.mock.patch.dict(
                os.environ, {"LMNR_PROJECT_API_KEY": "test-project-key"}, clear=True
            ),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
        ):
            module = importlib.import_module(
                "toolbench.observability.setup_tracing"
            )
            self.assertFalse(module.setup_tracing())

        self.assertEqual(
            events,
            [("initialize", "test-project-key", frozenset())],
        )
