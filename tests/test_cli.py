import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pzmodpack.cli import main
from pzmodpack.steamcmd import (
    DownloadBatchResult,
    SteamCmdResult,
    WorkshopSnapshot,
    WorkshopUploadResult,
)


class CliTests(unittest.TestCase):
    def test_scan_outputs_machine_readable_mod_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "mods" / "Example"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("name=Example\nid=ExampleId\n", encoding="utf-8")
            output = StringIO()

            with redirect_stdout(output):
                exit_code = main(["scan", str(root), "--json"])

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload[0]["folder_name"], "Example")
            self.assertEqual(payload[0]["mod_ids"], ["ExampleId"])

    def test_build_command_creates_a_pack(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            mod = source / "mods" / "Example"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("name=Example\nid=ExampleId\n", encoding="utf-8")
            destination = root / "built"
            preview = root / "preview.png"
            preview.write_bytes(b"png")
            output = StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        "build",
                        "--name",
                        "Example Pack",
                        "--namespace",
                        "Pack",
                        "--source",
                        str(source),
                        "--output",
                        str(destination),
                        "--workshop-id",
                        "987654321",
                        "--preview",
                        str(preview),
                        "--visibility",
                        "3",
                        "--active-id",
                        "Example=ExampleId",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((destination / "manifest.json").is_file())
            self.assertTrue((destination / "preview.png").is_file())
            self.assertIn("visibility=3", (destination / "workshop.txt").read_text())
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["active_mod_id_overrides"], {"Example": "ExampleId"})
            self.assertIn("Built 1 mod", output.getvalue())

    def test_steam_install_command_uses_the_managed_installer(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "steamcmd"
            executable = destination / "steamcmd.exe"
            output = StringIO()

            with (
                patch("pzmodpack.cli.install_steamcmd", return_value=executable) as install,
                redirect_stdout(output),
            ):
                exit_code = main(["steam-install", "--destination", str(destination)])

            self.assertEqual(exit_code, 0)
            install.assert_called_once_with(destination)
            self.assertIn(str(executable), output.getvalue())

    def test_anonymous_steam_download_command_reports_snapshots(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot = WorkshopSnapshot("111", "abcdef", root / "snapshots" / "111" / "abcdef")
            batch = DownloadBatchResult(SteamCmdResult(True, 0, "Downloaded"), (snapshot,))
            output = StringIO()

            with (
                patch("pzmodpack.cli.download_and_snapshot", return_value=batch) as download,
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "steam-download",
                        "111",
                        "--steamcmd",
                        str(root / "steamcmd"),
                        "--library",
                        str(root / "library"),
                        "--snapshots",
                        str(root / "snapshots"),
                        "--anonymous",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(download.call_args.args[1], ("111",))
            self.assertTrue(download.call_args.args[2].is_anonymous)
            self.assertIn("Snapshot 111", output.getvalue())

    def test_cached_account_can_upload_generated_pack(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result = WorkshopUploadResult(
                SteamCmdResult(True, 0, "Success"),
                "555",
                root / "build" / "workshop_upload.vdf",
            )
            output = StringIO()

            with (
                patch("pzmodpack.cli.upload_modpack", return_value=result) as upload,
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "steam-upload",
                        "--steamcmd",
                        str(root / "steamcmd"),
                        "--library",
                        str(root / "library"),
                        "--output",
                        str(root / "build"),
                        "--username",
                        "sai",
                        "--cached-login",
                        "--change-note",
                        "Initial release",
                        "--confirm-permissions",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(upload.call_args.args[1], (root / "build").resolve())
            self.assertEqual(upload.call_args.args[2].username, "sai")
            self.assertIsNone(upload.call_args.args[2].password)
            self.assertIn("Published file ID: 555", output.getvalue())


if __name__ == "__main__":
    unittest.main()
