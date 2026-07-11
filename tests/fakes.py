"""Shared test doubles for toolbench subprocess and ToolCall seams."""

from __future__ import annotations

import subprocess

from toolbench.transcript import ToolCall, UsageProvenance


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
