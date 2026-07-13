import json
import unittest
from pathlib import Path

MANIFEST = Path("corpus/manifest.json")


# Keys the manifest must NEVER carry again. The gate is derived per DEFECT from
# that defect's own oracle_cmd (see toolbench.complex.derive_test_gate); a
# per-REPO gate cannot be right, because a repo's defects do not share one test
# command -- maltese's said `Bash(cargo test:*)` while three of its four defects
# are verified by vitest. Nothing reads these keys any more, so a stale one is
# pure misinformation: it survives precisely because no test can contradict it.
_RETIRED_KEYS = ("test_gate", "test_cmd", "test_cwd")


class CorpusManifestTests(unittest.TestCase):
    def test_manifest_pins_a_sha_per_repo(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"wids", "maltese", "rich"})
        for name, entry in data.items():
            self.assertRegex(entry["sha"], r"^[0-9a-f]{7,40}$", name)

    def test_the_manifest_declares_no_per_repo_test_command_or_gate(self) -> None:
        # A per-repo gate is not merely dead, it is WRONG, and a future reader
        # would have no way to know: `test_cwd` likewise, since the directory an
        # oracle runs from is per-defect too (oracle.json's `cwd`).
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name, entry in data.items():
            for key in _RETIRED_KEYS:
                self.assertNotIn(
                    key,
                    entry,
                    f"{name}: manifest declares {key!r}, which nothing reads. The gate "
                    f"is derived per-defect from oracle.json; a per-repo one disagrees "
                    f"with its own defects.",
                )

    def test_rich_exists_solely_to_host_the_name_collision_defect(self) -> None:
        # Neither wids nor maltese contains a real name collision (measured: the
        # worst is 3 definitions, which is duplication, not a wall). A collision is
        # the ONE condition serena's reference-resolution needs to beat grep, so
        # without this corpus the benchmark could not test serena at its best.
        # It is also the one cell where repo is confounded with defect class --
        # `hosts_only` is what makes that confound explicit rather than forgotten.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["rich"]["hosts_only"], ["D1"])

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
