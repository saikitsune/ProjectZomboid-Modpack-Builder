import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pzmodpack.project import ProjectSettings, load_project, save_project


class ProjectSettingsTests(unittest.TestCase):
    def test_project_round_trip_never_contains_steam_secrets(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_file = root / "pack.pzpack.json"
            settings = ProjectSettings(
                name="Sai Pack",
                namespace="SaiPack",
                workshop_id="123",
                description="Test pack",
                output=root / "output",
                sources=(root / "snapshot-a", root / "snapshot-b"),
                steamcmd=root / "tools" / "steamcmd.exe",
                steam_library=root / "library",
                snapshot_root=root / "snapshots",
                workshop_items=("111", "222"),
                included_mod_ids=("Waterpipes", "Neat_Building"),
                version_bump="minor",
            )

            save_project(project_file, settings)
            restored = load_project(project_file)
            serialized = project_file.read_text(encoding="utf-8")

            self.assertEqual(restored, settings)
            self.assertEqual(
                restored.included_mod_ids,
                ("Waterpipes", "Neat_Building"),
            )
            self.assertNotIn("password", serialized.lower())
            self.assertNotIn("guard", serialized.lower())
            self.assertNotIn("username", serialized.lower())

    def test_existing_project_without_bundled_selection_still_loads(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_file = root / "legacy.pzpack.json"
            project_file.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "name": "Existing Pack",
                        "namespace": "Existing",
                        "workshop_id": "0",
                        "description": "Existing project",
                        "output": str(root / "output"),
                        "sources": [],
                        "steamcmd": str(root / "steamcmd.exe"),
                        "steam_library": str(root / "library"),
                        "snapshot_root": str(root / "snapshots"),
                    }
                ),
                encoding="utf-8",
            )

            restored = load_project(project_file)

            self.assertIsNone(restored.included_mod_ids)
            self.assertEqual(restored.version_bump, "patch")


if __name__ == "__main__":
    unittest.main()
