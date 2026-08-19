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
            )

            save_project(project_file, settings)
            restored = load_project(project_file)
            serialized = project_file.read_text(encoding="utf-8")

            self.assertEqual(restored, settings)
            self.assertNotIn("password", serialized.lower())
            self.assertNotIn("guard", serialized.lower())
            self.assertNotIn("username", serialized.lower())


if __name__ == "__main__":
    unittest.main()
