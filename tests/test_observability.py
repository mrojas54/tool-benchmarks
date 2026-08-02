"""Tests for optional observability setup."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
import unittest.mock
from pathlib import Path


class SetupTracingTests(unittest.TestCase):
    def test_setup_tracing_sanitizes_sdk_inputs_and_restores_state(self) -> None:
        events: list[tuple[list[str], str | None, str | None]] = []

        class RecordingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str, instruments: set[object]
            ) -> None:
                del project_api_key, instruments
                events.append(
                    (
                        sys.argv[:],
                        os.environ.get("LMNR_TRACE_METADATA"),
                        os.environ.get("LMNR_SPAN_CONTEXT"),
                    )
                )

        fake_lmnr = types.ModuleType("lmnr")
        fake_lmnr.Laminar = RecordingLaminar  # type: ignore[attr-defined]
        original_argv = sys.argv[:]
        original_environment = {
            name: os.environ.get(name)
            for name in ("LMNR_TRACE_METADATA", "LMNR_SPAN_CONTEXT")
        }
        configured_environment = {
            "LMNR_PROJECT_API_KEY": "test-project-key",
            "LMNR_TRACE_METADATA": "private-metadata",
            "LMNR_SPAN_CONTEXT": "private-context",
        }
        configured_context_environment = {
            name: configured_environment[name]
            for name in ("LMNR_TRACE_METADATA", "LMNR_SPAN_CONTEXT")
        }

        with (
            unittest.mock.patch.dict(os.environ, configured_environment),
            unittest.mock.patch.dict(sys.modules, {"lmnr": fake_lmnr}),
        ):
            module = importlib.import_module(
                "toolbench.observability.setup_tracing"
            )
            self.assertTrue(module.setup_tracing())
            self.assertEqual(sys.argv, original_argv)
            self.assertEqual(
                {
                    name: os.environ.get(name)
                    for name in ("LMNR_TRACE_METADATA", "LMNR_SPAN_CONTEXT")
                },
                configured_context_environment,
            )

        self.assertEqual(
            events,
            [(["toolbench", *original_argv[1:]], None, None)],
        )
        self.assertEqual(
            {
                name: os.environ.get(name) for name in original_environment
            },
            original_environment,
        )

    @unittest.skipUnless(
        importlib.util.find_spec("lmnr") is not None,
        "optional tracing extra is not installed",
    )
    def test_real_sdk_export_is_sanitized(self) -> None:
        script = r"""
import json
import sys

sys.argv[0] = "/private/checkout/tool-benchmarks/toolbench"

from toolbench.observability.setup_tracing import setup_tracing

assert setup_tracing()

from lmnr import Laminar
from lmnr.opentelemetry_lib.tracing import TracerWrapper
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

processor = TracerWrapper.instance._span_processor
old_instance = processor.instance
old_instance.shutdown()
exporter = InMemorySpanExporter()
processor.exporter = exporter
processor.instance = SimpleSpanProcessor(exporter)

with Laminar.start_as_current_span("toolbench.cli", tags=["toolbench", "probe"]):
    Laminar.set_trace_metadata({"command": "probe"})
    Laminar.set_span_output({"exit_code": 0})
Laminar.flush()

finished = exporter.get_finished_spans()
assert len(finished) == 1, finished
span = finished[0]
print(json.dumps({"resource": dict(span.resource.attributes), "attributes": dict(span.attributes)}))
Laminar.shutdown()
"""
        environment = os.environ.copy()
        environment.update(
            {
                "LMNR_PROJECT_API_KEY": "test-project-key",
                "LMNR_TRACE_METADATA": json.dumps(
                    {
                        "private_path": "/private/transcripts/member-session.jsonl",
                        "user_id": "private-user",
                    }
                ),
                "LMNR_SPAN_CONTEXT": "private-session-context",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["resource"]["service.name"], "toolbench")
        attributes = payload["attributes"]
        self.assertEqual(
            attributes["lmnr.association.properties.metadata.command"],
            "probe",
        )
        observed_output = completed.stdout + completed.stderr
        self.assertNotIn("private_path", observed_output)
        self.assertNotIn("private-user", observed_output)
        self.assertNotIn("private-session-context", observed_output)

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

    def test_setup_tracing_reads_the_laminar_env_fallback(self) -> None:
        events: list[tuple[object, ...]] = []

        class RecordingLaminar:
            @classmethod
            def initialize(
                cls, *, project_api_key: str, instruments: set[object]
            ) -> None:
                events.append(("initialize", project_api_key, frozenset(instruments)))

        def from_env(name: str) -> str | None:
            if name == "LMNR_PROJECT_API_KEY":
                return "dotenv-project-key"
            return None

        fake_lmnr = types.ModuleType("lmnr")
        fake_lmnr.Laminar = RecordingLaminar  # type: ignore[attr-defined]
        fake_sdk = types.ModuleType("lmnr.sdk")
        fake_utils = types.ModuleType("lmnr.sdk.utils")
        fake_sdk.utils = fake_utils  # type: ignore[attr-defined]
        fake_utils.from_env = from_env  # type: ignore[attr-defined]

        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.dict(
                sys.modules,
                {
                    "lmnr": fake_lmnr,
                    "lmnr.sdk": fake_sdk,
                    "lmnr.sdk.utils": fake_utils,
                },
            ),
        ):
            module = importlib.import_module(
                "toolbench.observability.setup_tracing"
            )
            self.assertTrue(module.setup_tracing())

        self.assertEqual(
            events,
            [("initialize", "dotenv-project-key", frozenset())],
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
