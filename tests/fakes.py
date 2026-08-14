"""Shared test doubles for toolbench subprocess, module, and ToolCall seams."""

from __future__ import annotations

import subprocess
import types

from toolbench.transcript import ToolCall, UsageProvenance


def make_module(name: str, **attributes: object) -> types.ModuleType:
    """Build a stand-in module for patching into `sys.modules`.

    `types.ModuleType` declares no attributes, so `module.Laminar = ...` is an
    `attr-defined` error under `--strict` and every call site grew its own
    `# type: ignore`. `setattr` is typed `(object, str, Any)`, so routing the
    assignment through here is checked as written and needs no suppression --
    the escape is designed out rather than moved.

    Nest by passing an inner module as a value:

        utils = make_module("lmnr.sdk.utils", from_env=from_env)
        sdk = make_module("lmnr.sdk", utils=utils)
    """
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


def completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeRunner:
    """Scripted subprocess-runner seam (S24): argv -> CompletedProcess, in call order."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if not self._responses:
            raise AssertionError(f"FakeRunner exhausted, unexpected call: {argv}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_call(**overrides: object) -> ToolCall:
    fields: dict[str, object] = {
        "agent": "claude-code",
        "source": "raw",
        "project": "tool-benchmarks",
        "name": "Read",
        "input_chars": 40,
        "output_chars": 400,
        "session_id": "sess-1",
        "ts": "2026-07-08T00:00:00Z",
        "usage": None,
        "duration_ms": None,
        "error": None,
        "model": "claude-opus-4-8",
    }
    fields.update(overrides)
    # Mirrors ClaudeParser._provenance so existing tests keep their meaning.
    fields.setdefault(
        "usage_provenance",
        UsageProvenance.PRESENT
        if fields["usage"] is not None
        else UsageProvenance.ABSENT_UNEXPECTED,
    )
    return ToolCall(**fields)  # type: ignore[arg-type]
