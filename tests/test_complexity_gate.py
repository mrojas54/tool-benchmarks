"""Regression tests for the PR-only cyclomatic-complexity gate."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

from toolbench.complexity_gate import (
    DEFAULT_THRESHOLD,
    FunctionComplexity,
    Symbol,
    collect_complexities,
    compare_complexity,
    evaluate_repository,
    format_annotations,
)


def _function(
    qualname: str,
    complexity: int,
    *,
    path: str = "src/toolbench/example.py",
    line: int = 1,
) -> FunctionComplexity:
    return FunctionComplexity(
        symbol=Symbol(path=path, qualname=qualname),
        complexity=complexity,
        line=line,
    )


def test_new_function_above_threshold_is_an_error() -> None:
    current = _function("new_router", 11)

    result = compare_complexity({}, {current.symbol: current})

    assert [change.reason for change in result.errors] == [
        "new function exceeds 10"
    ]
    assert result.warnings == ()


def test_existing_function_crossing_threshold_is_an_error() -> None:
    previous = _function("router", 10)
    current = _function("router", 11)

    result = compare_complexity(
        {previous.symbol: previous},
        {current.symbol: current},
    )

    assert [change.reason for change in result.errors] == [
        "complexity crossed 10"
    ]


def test_legacy_hotspot_only_fails_when_it_gets_worse() -> None:
    previous = _function("legacy_router", 14)
    unchanged = _function("legacy_router", 14)
    reduced = _function("legacy_router", 12)
    increased = _function("legacy_router", 15)

    assert not compare_complexity(
        {previous.symbol: previous},
        {unchanged.symbol: unchanged},
    ).errors
    assert not compare_complexity(
        {previous.symbol: previous},
        {reduced.symbol: reduced},
    ).errors
    assert [
        change.reason
        for change in compare_complexity(
            {previous.symbol: previous},
            {increased.symbol: increased},
        ).errors
    ] == ["legacy hotspot increased"]


def test_material_increase_below_threshold_is_a_warning() -> None:
    previous = _function("parser", 5)
    immaterial = _function("parser", 6)
    material = _function("parser", 7)

    assert not compare_complexity(
        {previous.symbol: previous},
        {immaterial.symbol: immaterial},
    ).warnings
    assert [
        change.reason
        for change in compare_complexity(
            {previous.symbol: previous},
            {material.symbol: material},
        ).warnings
    ] == ["complexity increased by 2"]


def test_collect_complexities_uses_qualified_names_and_ignores_noqa(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text(
        """
class First:
    def route(self, left, right):  # noqa: C901
        if left:
            return 1
        if right:
            return 2
        return 0

class Second:
    def route(self, enabled):
        if enabled:
            return 1
        return 0
""".lstrip(),
        encoding="utf-8",
    )
    ruff = shutil.which("ruff")
    assert ruff is not None

    report = collect_complexities(
        tmp_path,
        ("src/sample.py",),
        ruff_executable=ruff,
    )

    assert report[Symbol("src/sample.py", "First.route")].complexity == 3
    assert report[Symbol("src/sample.py", "Second.route")].complexity == 2


def test_repository_evaluation_compares_worktree_to_base_commit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text("def route(value):\n    return value\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "complexity-gate@example.invalid"),
        ("config", "user.name", "Complexity Gate Test"),
        ("add", "src/sample.py"),
        ("commit", "-qm", "base"),
    ):
        subprocess.run(
            ("git", *args),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    base = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_text(
        """
def route(value):
    if value == 1:
        return 1
    if value == 2:
        return 2
    if value == 3:
        return 3
    if value == 4:
        return 4
    if value == 5:
        return 5
    if value == 6:
        return 6
    if value == 7:
        return 7
    if value == 8:
        return 8
    if value == 9:
        return 9
    if value == 10:
        return 10
    return 0
""".lstrip(),
        encoding="utf-8",
    )
    ruff = shutil.which("ruff")
    assert ruff is not None

    result = evaluate_repository(
        tmp_path,
        base,
        ruff_executable=ruff,
    )

    assert [change.reason for change in result.errors] == [
        "complexity crossed 10"
    ]
    assert format_annotations(result) == (
        "::error file=src/sample.py,line=1::route complexity 1 -> 11; "
        "complexity crossed 10",
    )


def test_project_config_and_ci_enforce_the_regression_gate() -> None:
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert config["tool"]["ruff"]["target-version"] == "py313"

    # The budget must be declared exactly once, in a place that runs. Ruff only
    # evaluates `[tool.ruff.lint.mccabe] max-complexity` when C901 is selected,
    # and this project keeps Ruff's default rule set (E4, E7, E9, F), which
    # excludes it -- so a budget declared there without selecting the rule reads
    # as authoritative while doing nothing. This assertion permits either design
    # (enforce through Ruff, or leave the gate as sole owner) and forbids only
    # the misleading middle state.
    ruff_lint = config["tool"]["ruff"].get("lint", {})
    selected = [*ruff_lint.get("select", []), *ruff_lint.get("extend-select", [])]
    c901_enforced = any(rule.startswith("C9") or rule == "ALL" for rule in selected)
    assert ("mccabe" in ruff_lint) == c901_enforced, (
        "`[tool.ruff.lint.mccabe] max-complexity` is inert unless C901 is "
        "selected; the live budget is complexity_gate.DEFAULT_THRESHOLD"
    )
    assert DEFAULT_THRESHOLD == 10

    assert "uv run python -m toolbench.complexity_gate" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.before" in workflow
    assert "fetch-depth: 0" in workflow
