#!/usr/bin/env bash
# corpus/vendor.sh -- clone both repos at their pinned SHA and write serena config.
#
# .serena/project.yml is written EXPLICITLY and never left to auto-detection:
# serena detected maltese-agent (a Cargo workspace) as typescript-only and could
# not extract a single Rust symbol. `sql` is deliberately absent -- serena rejects
# it as an invalid language, so declaring it would break activation outright.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import json, pathlib, subprocess

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
PY
