"""Contract tests for the first complex-probe Harbor task."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
FIXTURE = (
    REPO_ROOT
    / "src"
    / "toolbench"
    / "probes"
    / "complex"
    / "wids-D2-string-keyed-dispatch"
)
TASK = (
    REPO_ROOT
    / "benchmarks"
    / "harbor"
    / "toolbench-complex"
    / "wids-D2-string-keyed-dispatch"
)


def test_wids_d2_task_has_harbor_required_files() -> None:
    expected = {
        "instruction.md",
        "task.toml",
        "environment/Dockerfile",
        "environment/docker-compose.yaml",
        "environment/defect.patch",
        "tests/test.sh",
        "tests/truth.json",
    }

    assert {str(path.relative_to(TASK)) for path in TASK.rglob("*") if path.is_file()} == expected


def test_wids_d2_instruction_matches_probe_prompt() -> None:
    assert (TASK / "instruction.md").read_bytes() == (FIXTURE / "prompt.md").read_bytes()


def test_wids_d2_environment_matches_pinned_defect() -> None:
    assert (TASK / "environment" / "defect.patch").read_bytes() == (
        FIXTURE / "defect.patch"
    ).read_bytes()

    dockerfile = (TASK / "environment" / "Dockerfile").read_text(encoding="utf-8")
    assert "https://github.com/mrojas54/wids-nyc-reading-group-assistant" in dockerfile
    assert "ARG REPO_SHA=a39cdd0" in dockerfile
    assert "git apply /tmp/defect.patch" in dockerfile
    assert "npm ci" in dockerfile
    assert "rm -rf .git" in dockerfile


def test_wids_d2_task_is_runtime_isolated() -> None:
    task = tomllib.loads((TASK / "task.toml").read_text(encoding="utf-8"))

    assert task["schema_version"] == "1.3"
    assert task["task"]["name"] == "toolbench/wids-D2-string-keyed-dispatch"
    assert task["metadata"]["repo_sha"] == "a39cdd0"
    assert task["environment"]["network_mode"] == "public"
    assert task["agent"]["user"] == "agent"
    assert task["verifier"]["user"] == "root"

    compose = (TASK / "environment" / "docker-compose.yaml").read_text(
        encoding="utf-8"
    )
    assert "main:" in compose
    assert "network_mode: none" in compose


def test_wids_d2_verifier_matches_probe_oracle() -> None:
    assert json.loads((TASK / "tests" / "truth.json").read_text(encoding="utf-8")) == json.loads(
        (FIXTURE / "truth.json").read_text(encoding="utf-8")
    )

    oracle = json.loads((FIXTURE / "oracle.json").read_text(encoding="utf-8"))
    test_script = (TASK / "tests" / "test.sh").read_text(encoding="utf-8")
    assert "cd /app/web" in test_script
    assert " ".join(oracle["cmd"]) in test_script
    assert "/logs/verifier/reward.json" in test_script
