import io
import json
import os
import subprocess
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pzmodpack.steamcmd import (
    SteamCmdClient,
    SteamCmdResult,
    SteamCredentials,
    WorkshopUploadConfig,
    _vdf_escape,
    build_command_script,
    build_upload_script,
    create_snapshot,
    download_and_snapshot,
    install_steamcmd,
    parse_workshop_ids,
    redact_secrets,
    upload_modpack,
    write_upload_vdf,
)


class WorkshopIdTests(unittest.TestCase):
    def test_parses_ids_and_workshop_urls_without_duplicates(self) -> None:
        values = [
            "2921417999",
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3778832646",
            "2921417999",
            "https://steamcommunity.com/workshop/filedetails/?id=3781771367&searchtext=fence",
        ]

        self.assertEqual(
            parse_workshop_ids(values),
            ("2921417999", "3778832646", "3781771367"),
        )


class LoginScriptTests(unittest.TestCase):
    def test_account_password_is_sent_in_script_and_can_be_redacted(self) -> None:
        credentials = SteamCredentials(
            username="sai",
            password="correct horse battery staple",
            guard_code="ABC12",
        )

        with TemporaryDirectory() as temporary_directory:
            library = Path(temporary_directory) / "steam library"
            script = build_command_script(
                workshop_ids=("2921417999",),
                credentials=credentials,
                library_root=library,
            )

            self.assertIn(f'force_install_dir "{library}"', script)
            self.assertIn('login "sai" "correct horse battery staple" "ABC12"', script)
            self.assertIn("workshop_download_item 108600 2921417999 validate", script)
            self.assertNotIn(credentials.password, redact_secrets(script, credentials))
            self.assertNotIn(credentials.guard_code, redact_secrets(script, credentials))

    def test_anonymous_login_never_requires_a_password(self) -> None:
        script = build_command_script(
            workshop_ids=(),
            credentials=SteamCredentials.anonymous(),
            library_root=Path("/tmp/steamcmd"),
        )

        self.assertIn("login anonymous", script)
        self.assertNotIn("None", script)

    def test_cached_account_login_can_upload_without_reentering_password(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            upload = root / "upload.vdf"
            library = root / "steam-library"
            script = build_upload_script(
                upload,
                SteamCredentials(username="sai"),
                library,
            )

            self.assertIn('login "sai"', script)
            self.assertIn(f'workshop_build_item "{upload.resolve()}"', script)


class UploadConfigTests(unittest.TestCase):
    def test_writes_project_zomboid_workshop_upload_vdf(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = root / "Contents" / "mods"
            content.mkdir(parents=True)
            preview = root / "preview.png"
            preview.write_bytes(b"png")
            vdf = root / "upload.vdf"
            config = WorkshopUploadConfig(
                published_file_id="123456789",
                content_folder=content,
                preview_file=preview,
                visibility=3,
                title='Sai "Test" Pack',
                description="Pack description",
                change_note="Updated mods",
            )

            write_upload_vdf(vdf, config)
            text = vdf.read_text(encoding="utf-8")

            self.assertIn('"appid"\t\t"108600"', text)
            self.assertIn('"publishedfileid"\t\t"123456789"', text)
            self.assertIn(
                f'"contentfolder"\t\t"{_vdf_escape(str(content.resolve()))}"',
                text,
            )
            self.assertIn('"visibility"\t\t"3"', text)
            self.assertIn('"title"\t\t"Sai \\"Test\\" Pack"', text)
            self.assertIn('"changenote"\t\t"Updated mods"', text)

    def test_upload_new_item_updates_generated_pack_with_published_id(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "build"
            (output / "Contents" / "mods" / "Example").mkdir(parents=True)
            (output / "preview.png").write_bytes(b"png")
            (output / ".pzmodpack-output").write_text("generated\n", encoding="utf-8")
            (output / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "Test Pack",
                        "description": "Description",
                        "workshop_id": "0",
                        "visibility": 2,
                    }
                ),
                encoding="utf-8",
            )
            (output / "workshop.txt").write_text("id=0\n", encoding="utf-8")
            (output / "amp-config.txt").write_text("WorkshopItems=0;\nMods=Test;\n", encoding="utf-8")
            (output / "workshop_upload.vdf").write_text(
                '"contentfolder" "C:\\\\stale\\\\build"\n',
                encoding="utf-8",
            )
            executable = Path(temporary_directory) / "steamcmd"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, Path(temporary_directory) / "library")

            def fake_upload(
                vdf_path: Path,
                _credentials: SteamCredentials,
                _timeout: int,
            ) -> SteamCmdResult:
                text = vdf_path.read_text(encoding="utf-8")
                vdf_path.write_text(
                    text.replace('"publishedfileid"\t\t"0"', '"publishedfileid"\t\t"555"'),
                    encoding="utf-8",
                )
                return SteamCmdResult(True, 0, "Success")

            with patch.object(client, "upload", side_effect=fake_upload):
                result = upload_modpack(
                    client,
                    output,
                    SteamCredentials(username="sai"),
                    "Initial upload",
                )

            self.assertTrue(result.command_result.success)
            self.assertEqual(result.published_file_id, "555")
            upload_vdf = result.vdf_path.read_text(encoding="utf-8")
            self.assertIn(
                f'"contentfolder"\t\t"{_vdf_escape(str((output / "Contents").resolve()))}"',
                upload_vdf,
            )
            self.assertNotIn("stale", upload_vdf)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workshop_id"], "555")
            self.assertIn("WorkshopItems=555;", (output / "amp-config.txt").read_text())
            self.assertIn("id=555", (output / "workshop.txt").read_text())


class SteamCmdClientTests(unittest.TestCase):
    def test_credentials_use_a_temporary_runscript_and_are_deleted_afterward(self) -> None:
        credentials = SteamCredentials(username="sai", password="secret", guard_code="GUARD")
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable=executable, library_root=root / "library")
            captured: dict[str, object] = {}

            def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                runscript = Path(argv[2])
                captured["path"] = runscript
                captured["contents"] = runscript.read_text(encoding="utf-8")
                captured["mode"] = runscript.stat().st_mode & 0o777
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout="login sai secret GUARD\nLogged in OK\n",
                )

            with patch("pzmodpack.steamcmd.subprocess.run", side_effect=fake_run) as run:
                result = client.test_login(credentials)

            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], [str(executable), "+runscript"])
            self.assertNotIn("secret", " ".join(argv))
            self.assertIn("secret", str(captured["contents"]))
            if os.name != "nt":
                self.assertEqual(captured["mode"], 0o600)
            self.assertFalse(Path(captured["path"]).exists())
            self.assertNotIn("secret", result.output)
            self.assertNotIn("GUARD", result.output)
            self.assertTrue(result.success)

    def test_login_timeout_returns_failure_instead_of_hanging_forever(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, root / "library")
            timeout = subprocess.TimeoutExpired(
                cmd=[str(executable)],
                timeout=5,
                output="Waiting for Steam Guard code...",
            )

            with patch("pzmodpack.steamcmd.subprocess.run", side_effect=timeout):
                result = client.test_login(
                    SteamCredentials(username="sai", password="secret"),
                    timeout=5,
                )

            self.assertFalse(result.success)
            self.assertEqual(result.return_code, 124)
            self.assertIn("timed out after 5 seconds", result.output)
            self.assertIn("Steam Guard", result.output)
            self.assertNotIn("secret", result.output)

    def test_login_requires_explicit_logged_in_confirmation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, root / "library")
            completed = subprocess.CompletedProcess(
                args=[str(executable)],
                returncode=0,
                stdout="SteamCMD update complete\n",
            )

            with patch("pzmodpack.steamcmd.subprocess.run", return_value=completed):
                result = client.test_login(SteamCredentials(username="sai"))

            self.assertFalse(result.success)
            self.assertIn("did not confirm a successful login", result.output)

    def test_windows_steam_public_ok_output_confirms_login(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd.exe"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, root / "library")
            output = (
                "@ShutdownOnFailedCommand 1\n"
                '"@ShutdownOnFailedCommand" = "1"\n'
                "@NoPromptForPassword 1\n"
                "Logging in using username/password.\n"
                "Steam Guard code provided.\n"
                "Logging in user 'saikitsune' [U:1:80152035] to Steam Public...OK\n"
                "Waiting for client config...OK\n"
                "Waiting for user info...OK\n"
                "Unloading Steam API...OK\n"
            )
            completed = subprocess.CompletedProcess(
                args=[str(executable)],
                returncode=0,
                stdout=output,
            )

            with patch("pzmodpack.steamcmd.subprocess.run", return_value=completed):
                result = client.test_login(
                    SteamCredentials(
                        username="saikitsune",
                        password="secret",
                        guard_code="ABCDE",
                    )
                )

            self.assertTrue(result.success)
            self.assertEqual(result.return_code, 0)

    def test_login_confirmation_ignores_terminal_control_sequences(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd.exe"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, root / "library")
            completed = subprocess.CompletedProcess(
                args=[str(executable)],
                returncode=0,
                stdout=(
                    "Logging in user 'sai' to Steam Public...\x1b[32mOK\x1b[0m\r\n"
                    "Waiting for user info...\x1b[32mOK\x1b[0m\r\n"
                ),
            )

            with patch("pzmodpack.steamcmd.subprocess.run", return_value=completed):
                result = client.test_login(SteamCredentials(username="sai"))

            self.assertTrue(result.success)


class SteamCmdInstallerTests(unittest.TestCase):
    def test_installs_the_official_windows_archive_into_managed_tools(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("steamcmd.exe", b"binary")

        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "steamcmd"
            executable = install_steamcmd(
                destination,
                platform_name="win32",
                fetcher=lambda _url: archive.getvalue(),
            )

            self.assertEqual(executable, destination / "steamcmd.exe")
            self.assertEqual(executable.read_bytes(), b"binary")


class SnapshotTests(unittest.TestCase):
    def test_snapshots_are_immutable_and_content_addressed(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            downloaded = root / "downloads" / "2921417999"
            mod = downloaded / "mods" / "Scalies"
            mod.mkdir(parents=True)
            source_file = mod / "mod.info"
            source_file.write_text("id=Scalies\n", encoding="utf-8")
            snapshots = root / "snapshots"

            first = create_snapshot(downloaded, snapshots, "2921417999")
            source_file.write_text("id=ScaliesUpdated\n", encoding="utf-8")
            second = create_snapshot(downloaded, snapshots, "2921417999")

            self.assertNotEqual(first.sha256, second.sha256)
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(
                (first.path / "mods" / "Scalies" / "mod.info").read_text(encoding="utf-8"),
                "id=Scalies\n",
            )
            self.assertTrue((first.path / "snapshot.json").is_file())

    def test_download_batch_creates_snapshots_for_each_item(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd" / "steamcmd"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            library = root / "library"
            for workshop_id in ("111", "222"):
                mod = (
                    library
                    / "steamapps"
                    / "workshop"
                    / "content"
                    / "108600"
                    / workshop_id
                    / "mods"
                    / f"Mod{workshop_id}"
                )
                mod.mkdir(parents=True)
                (mod / "mod.info").write_text(f"id=Mod{workshop_id}\n", encoding="utf-8")
            client = SteamCmdClient(executable, library)

            with patch.object(
                client,
                "download",
                return_value=SteamCmdResult(True, 0, "Success"),
            ):
                batch = download_and_snapshot(
                    client,
                    ("111", "222"),
                    SteamCredentials.anonymous(),
                    root / "snapshots",
                )

            self.assertTrue(batch.command_result.success)
            self.assertEqual([item.workshop_id for item in batch.snapshots], ["111", "222"])
            self.assertTrue(all(item.path.is_dir() for item in batch.snapshots))


if __name__ == "__main__":
    unittest.main()
