import json
import unittest
from pathlib import Path

MANIFEST = Path("corpus/manifest.json")


class CorpusManifestTests(unittest.TestCase):
    def test_manifest_pins_a_sha_and_test_gate_per_repo(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"wids", "maltese"})
        for name, entry in data.items():
            self.assertRegex(entry["sha"], r"^[0-9a-f]{7,40}$", name)
            self.assertTrue(entry["test_gate"].startswith("Bash("), name)

    def test_rust_is_declared_because_serena_autodetect_missed_it(self) -> None:
        # Serena auto-detected maltese-agent (a Cargo workspace) as typescript-ONLY
        # and refused to extract a single Rust symbol. A benchmark on the
        # auto-detected config measures a crippled serena and blames the tool.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("rust", data["maltese"]["serena_languages"])

    def test_sql_is_never_declared_because_serena_rejects_it(self) -> None:
        # activate_project with `sql` raises `Invalid language: sql`. Declaring it
        # would make the vendored corpus fail to activate at all.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name, entry in data.items():
            self.assertNotIn("sql", entry["serena_languages"], name)
