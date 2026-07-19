#!/usr/bin/env bash
# corpus/vendor.sh -- clone all three repos at their pinned SHA, write serena
# config, and provision any repo whose oracle needs an installed environment.
#
# .serena/project.yml is written EXPLICITLY and never left to auto-detection:
# serena detected maltese-agent (a Cargo workspace) as typescript-only and could
# not extract a single Rust symbol. `sql` is deliberately absent -- serena rejects
# it as an invalid language, so declaring it would break activation outright.
#
# `provision` exists because a clone is not a runnable corpus. rich's oracle is
# pytest against an importable `rich`, and a bare clone has neither -- so D1 would
# have run only on the one machine that happened to have a venv lying around, and
# nowhere else. The venv is gitignored: it costs the repo nothing, and costs a
# clean checkout one `python3 -m venv` plus two `pip install`s (rich's manifest
# `provision` steps below) -- stdlib venv + pip, NOT `uv`, so provisioning the
# corpus needs nothing installed beyond a python3.
set -euo pipefail
cd "$(dirname "$0")"

# The manifest's source of truth ships inside the package
# (src/toolbench/corpus/manifest.json). Copy it here so the vendored tree is
# self-describing: complex_runner and the TOOLBENCH_CORPUS_TESTS=1 suite read
# `<corpus_root>/manifest.json`, and the heredoc below reads this local copy.
cp ../src/toolbench/corpus/manifest.json manifest.json

python3 - <<'PY'
import json, pathlib, subprocess, sys

manifest = json.loads(pathlib.Path("manifest.json").read_text())
for name, entry in manifest.items():
    dest = pathlib.Path(name)
    if dest.exists():
        print(f"{name}: present, skipping clone")
    else:
        subprocess.run(["git", "clone", "-q", entry["origin"], name], check=True)
    subprocess.run(["git", "-C", name, "checkout", "-q", entry["sha"]], check=True)

    serena = dest / ".serena"
    serena.mkdir(exist_ok=True)
    langs = "\n".join(f"- {lang}" for lang in entry["serena_languages"])
    (serena / "project.yml").write_text(
        f'project_name: "{name}"\nlanguages:\n{langs}\n'
        'encoding: "utf-8"\nignore_all_files_in_gitignore: true\n'
        'ls_workspace_folders: ["."]\nread_only: false\nexcluded_tools: []\n'
    )
    print(f"{name}: pinned at {entry['sha']}, languages={entry['serena_languages']}")

    for step in entry.get("provision", []):
        print(f"{name}: provisioning -- {' '.join(step)}")
        subprocess.run(step, cwd=dest, check=True)

    # A provisioning step that exits 0 but leaves the oracle unrunnable is exactly
    # the failure this block exists to prevent. Prove the environment, never assume it.
    check = entry.get("provision_check")
    if check:
        proc = subprocess.run(check, cwd=dest, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(
                f"{name}: provisioned, but `{' '.join(check)}` failed "
                f"({proc.returncode}): {proc.stderr.strip()[:200]}"
            )
        print(f"{name}: oracle environment OK -- {proc.stdout.strip().splitlines()[0]}")
PY
