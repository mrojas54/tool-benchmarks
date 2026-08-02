"""Fail only on cyclomatic-complexity debt introduced by a change."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

DEFAULT_THRESHOLD = 10
DEFAULT_WARNING_DELTA = 2
_COMPLEXITY_MESSAGE = re.compile(
    r"^`(?P<name>[^`]+)` is too complex \((?P<score>\d+) > 0\)$"
)


@dataclass(frozen=True, order=True)
class Symbol:
    path: str
    qualname: str


@dataclass(frozen=True)
class FunctionComplexity:
    symbol: Symbol
    complexity: int
    line: int


@dataclass(frozen=True)
class ComplexityChange:
    current: FunctionComplexity
    previous: int | None
    reason: str


@dataclass(frozen=True)
class GateResult:
    errors: tuple[ComplexityChange, ...]
    warnings: tuple[ComplexityChange, ...]


def compare_complexity(
    previous: Mapping[Symbol, FunctionComplexity],
    current: Mapping[Symbol, FunctionComplexity],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    warning_delta: int = DEFAULT_WARNING_DELTA,
) -> GateResult:
    errors: list[ComplexityChange] = []
    warnings: list[ComplexityChange] = []
    for symbol, measured in sorted(current.items()):
        old = previous.get(symbol)
        if old is None and measured.complexity > threshold:
            errors.append(
                ComplexityChange(measured, None, f"new function exceeds {threshold}")
            )
        elif old is not None and old.complexity <= threshold < measured.complexity:
            errors.append(
                ComplexityChange(
                    measured,
                    old.complexity,
                    f"complexity crossed {threshold}",
                )
            )
        elif (
            old is not None
            and old.complexity > threshold
            and measured.complexity > old.complexity
        ):
            errors.append(
                ComplexityChange(
                    measured,
                    old.complexity,
                    "legacy hotspot increased",
                )
            )
        elif (
            old is not None
            and measured.complexity <= threshold
            and measured.complexity - old.complexity >= warning_delta
        ):
            warnings.append(
                ComplexityChange(
                    measured,
                    old.complexity,
                    f"complexity increased by {measured.complexity - old.complexity}",
                )
            )
    return GateResult(tuple(errors), tuple(warnings))


def _qualified_function_names(source: str) -> dict[tuple[int, str], str]:
    names: dict[tuple[int, str], str] = {}

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        next_scope = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names[(node.lineno, node.name)] = ".".join((*scope, node.name))
            next_scope = (*scope, node.name)
        elif isinstance(node, ast.ClassDef):
            next_scope = (*scope, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, next_scope)

    visit(ast.parse(source), ())
    return names


def collect_complexities(
    root: Path,
    paths: Sequence[str],
    *,
    ruff_executable: str = "ruff",
) -> dict[Symbol, FunctionComplexity]:
    if not paths:
        return {}
    command = (
        ruff_executable,
        "check",
        "--isolated",
        "--no-cache",
        "--ignore-noqa",
        "--select",
        "C901",
        "--config",
        "lint.mccabe.max-complexity=0",
        "--output-format",
        "json",
        "--",
        *paths,
    )
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"Ruff complexity scan failed:\n{completed.stderr.strip()}")
    payload = cast(list[dict[str, object]], json.loads(completed.stdout or "[]"))
    qualified = {
        path: _qualified_function_names((root / path).read_text(encoding="utf-8"))
        for path in paths
    }
    report: dict[Symbol, FunctionComplexity] = {}
    resolved_root = root.resolve()
    for diagnostic in payload:
        if diagnostic.get("code") != "C901":
            continue
        filename = diagnostic.get("filename")
        location = diagnostic.get("location")
        message = diagnostic.get("message")
        if (
            not isinstance(filename, str)
            or not isinstance(location, dict)
            or not isinstance(message, str)
        ):
            raise RuntimeError("Ruff returned a malformed C901 diagnostic")
        line = location.get("row")
        match = _COMPLEXITY_MESSAGE.fullmatch(message)
        if not isinstance(line, int) or match is None:
            raise RuntimeError("Ruff returned an unrecognized C901 diagnostic")
        path = Path(filename).resolve().relative_to(resolved_root).as_posix()
        name = match.group("name")
        qualname = qualified[path].get((line, name))
        if qualname is None:
            raise RuntimeError(f"Could not qualify {path}:{line} {name}")
        symbol = Symbol(path, qualname)
        report[symbol] = FunctionComplexity(
            symbol=symbol,
            complexity=int(match.group("score")),
            line=line,
        )
    return report


def _git(
    root: Path,
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
    )


def _changed_python_paths(root: Path, base: str) -> tuple[str, ...]:
    _git(root, ("rev-parse", "--verify", f"{base}^{{commit}}"))
    changed = _git(
        root,
        ("diff", "--name-only", "--diff-filter=ACMR", "-z", base, "--", "*.py"),
    ).stdout.split("\0")
    untracked = _git(
        root,
        ("ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"),
    ).stdout.split("\0")
    paths = {
        path
        for path in (*changed, *untracked)
        if path.endswith(".py") and path.startswith(("src/", "tests/"))
    }
    return tuple(sorted(paths))


def _write_base_snapshot(
    root: Path,
    snapshot: Path,
    base: str,
    paths: Sequence[str],
) -> tuple[str, ...]:
    written: list[str] = []
    for path in paths:
        source = _git(root, ("show", f"{base}:{path}"), check=False)
        if source.returncode != 0:
            continue
        destination = snapshot / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.stdout, encoding="utf-8")
        written.append(path)
    return tuple(written)


def evaluate_repository(
    root: Path,
    base: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    warning_delta: int = DEFAULT_WARNING_DELTA,
    ruff_executable: str = "ruff",
) -> GateResult:
    paths = _changed_python_paths(root, base)
    current_paths = tuple(path for path in paths if (root / path).is_file())
    with tempfile.TemporaryDirectory(prefix="toolbench-complexity-base-") as temp:
        snapshot = Path(temp)
        base_paths = _write_base_snapshot(root, snapshot, base, paths)
        previous = collect_complexities(
            snapshot,
            base_paths,
            ruff_executable=ruff_executable,
        )
    current = collect_complexities(
        root,
        current_paths,
        ruff_executable=ruff_executable,
    )
    return compare_complexity(
        previous,
        current,
        threshold=threshold,
        warning_delta=warning_delta,
    )


def format_annotations(result: GateResult) -> tuple[str, ...]:
    annotations: list[str] = []
    for level, changes in (("error", result.errors), ("warning", result.warnings)):
        for change in changes:
            measured = change.current
            before = "new" if change.previous is None else str(change.previous)
            annotations.append(
                f"::{level} file={measured.symbol.path},line={measured.line}::"
                f"{measured.symbol.qualname} complexity {before} -> "
                f"{measured.complexity}; {change.reason}"
            )
    return tuple(annotations)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject cyclomatic-complexity regressions relative to a Git base."
    )
    parser.add_argument("--base", required=True, help="Git commit used as the baseline")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--warning-delta", type=int, default=DEFAULT_WARNING_DELTA)
    parser.add_argument("--ruff", default="ruff", help="Path to the Ruff executable")
    args = parser.parse_args(argv)
    result = evaluate_repository(
        args.root,
        args.base,
        threshold=args.threshold,
        warning_delta=args.warning_delta,
        ruff_executable=args.ruff,
    )
    for annotation in format_annotations(result):
        print(annotation)
    print(
        f"Complexity regression gate: {len(result.errors)} error(s), "
        f"{len(result.warnings)} warning(s)."
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
