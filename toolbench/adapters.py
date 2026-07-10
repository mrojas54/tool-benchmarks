"""Schema dispatch (TB-13). Stdlib only.

`parse_session` used to be the unnamed default for every transcript that was not
hermes. A codex session matched nothing inside it and returned
`ParseResult(calls=[], malformed=0)` -- a healthy-looking zero (TB-12). A parser
that cannot recognize a schema must not be the fallback for schemas it has never
seen, so detection is explicit and failure is loud.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from itertools import chain

from toolbench.parsers import ClaudeParser, CodexParser, HermesTraceParser, TranscriptParser
from toolbench.sources import (
    AgentsViewLoader,
    RawFileLoader,
    Runner,
    SessionLoader,
    SessionRef,
)
from toolbench.transcript import ParseResult

# Ordered by nothing in particular: detection asserts exactly one parser claims a
# line, so order cannot silently decide a tie. ClaudeParser and HermesTraceParser
# partition on `version`; CodexParser partitions from both on top-level `type`.
PARSERS: tuple[type[TranscriptParser], ...] = (ClaudeParser, HermesTraceParser, CodexParser)

# Transcripts open with control/metadata preamble, so the discriminating record is
# not guaranteed to be the first decodable line. Measured across all 2142
# claude/cowork sessions in the 2026-07-09 archive, every one claims at decodable
# line 1; zero were never claimed. The window is insurance against unseen preamble,
# and it bounds the read on a blob that no parser will ever claim.
DETECT_WINDOW = 100


class UnknownSchema(RuntimeError):
    """No registered parser claimed any line in the detection window.

    Subclasses RuntimeError so `passive.main` demotes the session to
    `skipped_roots` via its existing per-session guard. The agent is then named
    in the Summary rather than reported as an agent that did no tool work.
    """


class AmbiguousSchema(RuntimeError):
    """Two parsers claimed the same line. A programming error, not a data error."""


def detect_parser(
    lines: Iterator[str], *, window: int = DETECT_WINDOW
) -> tuple[TranscriptParser, Iterator[str]]:
    """Sniff up to `window` non-empty lines; return (parser, all lines replayed).

    Consumed lines are chained back onto the iterator, so the transcript is read
    exactly once even though detection looks at its head. Undecodable lines inside
    the window are skipped and NOT counted -- malformed accounting is the parser's
    job (S5), and counting here would charge a session twice.
    """
    buffered: list[str] = []
    seen = 0
    for raw_line in lines:
        buffered.append(raw_line)
        line = raw_line.strip()
        if not line:
            continue
        if seen >= window:
            break
        seen += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        claimed = [p for p in PARSERS if p.claims_line(entry)]
        if len(claimed) > 1:
            tags = ", ".join(p.schema_tag for p in claimed)
            raise AmbiguousSchema(f"line claimed by multiple parsers: {tags}")
        if claimed:
            return claimed[0](), chain(buffered, lines)

    raise UnknownSchema(
        f"no registered parser claimed any of the first {seen} decodable lines"
    )


class SessionAdapter(ABC):
    """The single seam `passive.py` sees: a SessionRef becomes a ParseResult."""

    @abstractmethod
    def claims(self, ref: SessionRef) -> bool:
        """True if this adapter is responsible for `ref`."""

    @abstractmethod
    def parse(self, ref: SessionRef) -> ParseResult: ...


class ComposedAdapter(SessionAdapter):
    """A loader and a content-detected parser, composed. The terminal fallback.

    `claims` is unconditionally True: this adapter is last in the registry, and a
    ref it cannot handle surfaces as `UnknownSchema` from `detect_parser` rather
    than as a silent zero.
    """

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def claims(self, ref: SessionRef) -> bool:
        return True

    def _loader(self, ref: SessionRef) -> SessionLoader:
        if ref.path is not None:
            return RawFileLoader()
        return AgentsViewLoader(self._runner) if self._runner else AgentsViewLoader()

    def parse(self, ref: SessionRef) -> ParseResult:
        lines = self._loader(ref).lines(ref)
        parser, replayed = detect_parser(lines)
        return parser.parse(replayed, agent=ref.agent, source=ref.source, project=ref.project)
