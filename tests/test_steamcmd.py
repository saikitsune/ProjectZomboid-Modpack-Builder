import io
import json
import os
import subprocess
import threading
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
    build_workshop_description,
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
    def test_workshop_description_uses_version_recorded_at_build_time(self) -> None:
        manifest: dict[str, object] = {
            "description": "Pack",
            "builder_version": "1.2.3",
            "mods": [],
        }

        description = build_workshop_description(manifest, "9.8.7")

        self.assertIn("Version: 1.2.3", description)
        self.assertNotIn("Version: 9.8.7", description)

    def test_builds_attributed_description_with_every_bundled_workshop_mod(self) -> None:
        manifest: dict[str, object] = {
            "description": 'Curated "server" pack',
            "mods": [
                {
                    "display_name": "Second Mod",
                    "source_folder": "Second",
                    "source_workshop_id": "222",
                },
                {
                    "display_name": "First [Build 42] Mod",
                    "source_folder": "First",
                    "source_workshop_id": "111",
                },
                {
                    "display_name": "Second Add-on",
                    "source_folder": "SecondAddon",
                    "source_workshop_id": "222",
                },
                {
                    "display_name": 'Anthro Survivors (the "Furry Mod")',
                    "source_folder": "Anthro",
                    "source_workshop_id": "333",
                },
                {
                    "display_name": "Local Mod",
                    "source_folder": "Local",
                    "source_workshop_id": None,
                },
            ],
        }

        description = build_workshop_description(manifest, "9.8.7")

        self.assertTrue(description.startswith("Curated “server” pack\n\n"))
        self.assertIn(
            "Modpack made with "
            "[url=https://github.com/saikitsune/ProjectZomboid-Modpack-Builder]"
            "ProjectZomboid Modpack Builder[/url] Version: 9.8.7",
            description,
        )
        self.assertIn("Bundled Workshop mods:", description)
        self.assertIn(
            "[url=https://steamcommunity.com/sharedfiles/filedetails/?id=111]"
            "First (Build 42) Mod (Workshop ID: 111)[/url]",
            description,
        )
        self.assertIn(
            "[url=https://steamcommunity.com/sharedfiles/filedetails/?id=333]"
            "Anthro Survivors (the “Furry Mod”) (Workshop ID: 333)[/url]",
            description,
        )
        self.assertIn(
            "[url=https://steamcommunity.com/sharedfiles/filedetails/?id=222]"
            "Second Mod (Workshop ID: 222)[/url]",
            description,
        )
        self.assertIn(
            "[url=https://steamcommunity.com/sharedfiles/filedetails/?id=222]"
            "Second Add-on (Workshop ID: 222)[/url]",
            description,
        )
        self.assertNotIn("Local Mod", description)
        self.assertEqual(build_workshop_description(manifest, "9.8.7"), description)
        self.assertEqual(manifest["description"], 'Curated "server" pack')

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
                description='Anthro Survivors (the "Furry Mod")\nPack description',
                change_note='Updated "quoted" mods',
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
            self.assertIn('"title"\t\t"Sai “Test” Pack"', text)
            self.assertIn(
                '"description"\t\t"Anthro Survivors (the “Furry Mod”)'
                '\\nPack description"',
                text,
            )
            self.assertIn(
                '"changenote"\t\t"Updated “quoted” mods"',
                text,
            )
            self.assertNotIn('\\"', text)
            value_lines = [line for line in text.splitlines() if line.startswith("\t")]
            self.assertTrue(value_lines)
            self.assertTrue(all(line.count('"') == 4 for line in value_lines))

    def test_vdf_quotes_preserve_quoted_names_and_measurements(self) -> None:
        self.assertEqual(
            _vdf_escape('A 12" shelf named "Furry Mod"'),
            "A 12” shelf named “Furry Mod”",
        )
        self.assertEqual(_vdf_escape('"Unclosed'), "“Unclosed")
        self.assertEqual(_vdf_escape("one\r\ntwo\rthree"), "one\\ntwo\\nthree")

        with self.assertRaisesRegex(ValueError, "NUL"):
            _vdf_escape("bad\0value")

    def test_rejects_quoted_workshop_paths_instead_of_rewriting_them(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = WorkshopUploadConfig(
                published_file_id="123",
                content_folder=root / 'bad"content',
                preview_file=root / "preview.png",
                visibility=2,
                title="Pack",
                description="Description",
                change_note="Changes",
            )

            with self.assertRaisesRegex(ValueError, "content folder.*double quotes"):
                write_upload_vdf(root / "upload.vdf", config)

    def test_rejects_generated_description_over_steam_byte_limit(self) -> None:
        manifest: dict[str, object] = {"description": "x" * 8000, "mods": []}

        with self.assertRaisesRegex(ValueError, "8000-byte limit"):
            build_workshop_description(manifest, "9.8.7")

    def test_description_limit_counts_typographic_quote_bytes(self) -> None:
        manifest: dict[str, object] = {"description": '"' * 2700, "mods": []}

        with self.assertRaisesRegex(ValueError, "8000-byte limit"):
            build_workshop_description(manifest, "9.8.7")

    def test_upload_new_item_updates_generated_pack_with_published_id(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "build"
            (output / "Contents" / "mods" / "Example").mkdir(parents=True)
            (output / "preview.png").write_bytes(b"png")
            (output / ".pzmodpack-output").write_text("generated\n", encoding="utf-8")
            manifest_payload: dict[str, object] = {
                "name": "Test Pack",
                "description": "Description",
                "workshop_id": "0",
                "visibility": 2,
                "generated_change_note": "Test Pack v1.0.0\n\nInitial build.",
                "mods": [
                    {
                        "display_name": "Example Mod",
                        "source_folder": "Example",
                        "source_workshop_id": "123456",
                    }
                ],
            }
            (output / "manifest.json").write_text(
                json.dumps(manifest_payload),
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
                    "   ",
                )

            self.assertTrue(result.command_result.success)
            self.assertEqual(result.published_file_id, "555")
            upload_vdf = result.vdf_path.read_text(encoding="utf-8")
            self.assertIn(
                f'"contentfolder"\t\t"{_vdf_escape(str((output / "Contents").resolve()))}"',
                upload_vdf,
            )
            self.assertNotIn("stale", upload_vdf)
            self.assertIn(
                '"changenote"\t\t"Test Pack v1.0.0\\n\\nInitial build."',
                upload_vdf,
            )
            expected_description = build_workshop_description(manifest_payload)
            self.assertIn(
                f'"description"\t\t"{_vdf_escape(expected_description)}"',
                upload_vdf,
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workshop_id"], "555")
            self.assertIn("WorkshopItems=555;", (output / "amp-config.txt").read_text())
            self.assertIn("id=555", (output / "workshop.txt").read_text())


class SteamCmdClientTests(unittest.TestCase):
    def test_download_streams_redacted_steamcmd_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, root / "library")
            credentials = SteamCredentials(username="sai", password="secret")
            streamed: list[str] = []

            class FakeProcess:
                def __init__(self) -> None:
                    self.stdout = io.StringIO(
                        "Logging in with secret\n"
                        "workshop_download_item 108600 111 validate\n"
                        "Update state (0x5) validating, progress: 42.50\n"
                        'Success. Downloaded item 111 to "cache"\n'
                    )
                    self.returncode = 0

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return self.returncode

                def kill(self) -> None:
                    self.returncode = 1

                def poll(self) -> int | None:
                    return self.returncode

            with patch("pzmodpack.steamcmd.subprocess.Popen", return_value=FakeProcess()):
                result = client.download(
                    ("111",),
                    credentials,
                    output_callback=streamed.append,
                )

            self.assertTrue(result.success)
            self.assertTrue(any("progress: 42.50" in line for line in streamed))
            self.assertTrue(any("Downloaded item 111" in line for line in streamed))
            self.assertNotIn("secret", "\n".join(streamed))
            self.assertNotIn("secret", result.output)

    def test_streaming_download_still_honors_timeout(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd"
            executable.write_text("", encoding="utf-8")
            client = SteamCmdClient(executable, root / "library")
            stopped = threading.Event()

            class BlockingOutput:
                def __iter__(self) -> "BlockingOutput":
                    return self

                def __next__(self) -> str:
                    stopped.wait(timeout=2)
                    raise StopIteration

            class FakeProcess:
                def __init__(self) -> None:
                    self.stdout = BlockingOutput()
                    self.returncode: int | None = None

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return self.returncode or 1

                def kill(self) -> None:
                    self.returncode = 1
                    stopped.set()

                def poll(self) -> int | None:
                    return self.returncode

            with patch("pzmodpack.steamcmd.subprocess.Popen", return_value=FakeProcess()):
                result = client.download(
                    ("111",),
                    SteamCredentials.anonymous(),
                    timeout=0,
                    output_callback=lambda _line: None,
                )

            self.assertFalse(result.success)
            self.assertEqual(result.return_code, 124)
            self.assertIn("timed out after 0 seconds", result.output)

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
            metadata = json.loads(
                (first.path / "snapshot.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["format_version"], 2)
            self.assertEqual(metadata["workshop_id"], "2921417999")
            self.assertEqual(metadata["sha256"], first.sha256)
            self.assertEqual(
                metadata["snapshot_created_at_utc"],
                first.snapshot_created_at_utc,
            )

    def test_existing_format_one_snapshot_is_enriched_without_changing_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            downloaded = root / "downloads" / "2921417999"
            mod_info = downloaded / "mods" / "Scalies" / "mod.info"
            mod_info.parent.mkdir(parents=True)
            mod_info.write_text("id=Scalies\n", encoding="utf-8")
            first = create_snapshot(downloaded, root / "snapshots", "2921417999")
            snapshot_mod_info = first.path / "mods" / "Scalies" / "mod.info"
            original_payload = snapshot_mod_info.read_bytes()
            original_payload_mtime = snapshot_mod_info.stat().st_mtime_ns
            metadata_path = first.path / "snapshot.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "workshop_id": "2921417999",
                        "sha256": first.sha256,
                    }
                ),
                encoding="utf-8",
            )
            os.utime(metadata_path, (1_700_000_000, 1_700_000_000))

            enriched = create_snapshot(
                downloaded,
                root / "snapshots",
                "2921417999",
                workshop_updated_at_utc="2023-11-15T01:00:00+00:00",
                workshop_manifest_id="987654321",
            )

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["format_version"], 2)
            self.assertEqual(
                metadata["snapshot_created_at_utc"],
                "2023-11-14T22:13:20+00:00",
            )
            self.assertEqual(
                metadata["workshop_updated_at_utc"],
                "2023-11-15T01:00:00+00:00",
            )
            self.assertEqual(metadata["workshop_manifest_id"], "987654321")
            self.assertEqual(enriched.snapshot_created_at_utc, metadata["snapshot_created_at_utc"])
            self.assertEqual(snapshot_mod_info.read_bytes(), original_payload)
            self.assertEqual(snapshot_mod_info.stat().st_mtime_ns, original_payload_mtime)

    def test_download_records_preferred_acf_workshop_revision_metadata(self) -> None:
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
                (mod / "mod.info").write_text(
                    f"id=Mod{workshop_id}\n",
                    encoding="utf-8",
                )
            acf = library / "steamapps" / "workshop" / "appworkshop_108600.acf"
            acf.parent.mkdir(parents=True, exist_ok=True)
            acf.write_text(
                '"AppWorkshop"\n'
                "{\n"
                '\t"WorkshopItemsInstalled"\n'
                "\t{\n"
                '\t\t"111"\n'
                '\t\t{\n\t\t\t"timeupdated" "1700000000"\n'
                '\t\t\t"manifest" "1110"\n\t\t}\n'
                '\t\t"222"\n'
                '\t\t{\n\t\t\t"timeupdated" "1700000100"\n'
                '\t\t\t"manifest" "2220"\n\t\t}\n'
                "\t}\n"
                '\t"WorkshopItemDetails"\n'
                "\t{\n"
                '\t\t"111"\n'
                '\t\t{\n\t\t\t"latest_timeupdated" "1700000200"\n'
                '\t\t\t"latest_manifest" "1111"\n\t\t}\n'
                '\t\t"222"\n'
                '\t\t{\n\t\t\t"latest_timeupdated" "invalid"\n'
                '\t\t\t"latest_manifest" "0"\n\t\t}\n'
                "\t}\n"
                "}\n",
                encoding="utf-8",
            )
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

            first, second = batch.snapshots
            self.assertEqual(first.workshop_updated_at_utc, "2023-11-14T22:16:40+00:00")
            self.assertEqual(first.workshop_manifest_id, "1111")
            self.assertEqual(second.workshop_updated_at_utc, "2023-11-14T22:15:00+00:00")
            self.assertEqual(second.workshop_manifest_id, "2220")
            first_metadata = json.loads(
                (first.path / "snapshot.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                first_metadata["workshop_updated_at_utc"],
                first.workshop_updated_at_utc,
            )
            self.assertEqual(
                first_metadata["workshop_manifest_id"],
                first.workshop_manifest_id,
            )

    def test_malformed_acf_does_not_block_snapshot_creation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "steamcmd" / "steamcmd"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            library = root / "library"
            mod = (
                library
                / "steamapps"
                / "workshop"
                / "content"
                / "108600"
                / "111"
                / "mods"
                / "Mod111"
            )
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("id=Mod111\n", encoding="utf-8")
            acf = library / "steamapps" / "workshop" / "appworkshop_108600.acf"
            acf.parent.mkdir(parents=True, exist_ok=True)
            acf.write_text('"AppWorkshop" { broken', encoding="utf-8")
            client = SteamCmdClient(executable, library)

            with patch.object(
                client,
                "download",
                return_value=SteamCmdResult(True, 0, "Success"),
            ):
                batch = download_and_snapshot(
                    client,
                    ("111",),
                    SteamCredentials.anonymous(),
                    root / "snapshots",
                )

            snapshot = batch.snapshots[0]
            metadata = json.loads(
                (snapshot.path / "snapshot.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["format_version"], 2)
            self.assertIn("snapshot_created_at_utc", metadata)
            self.assertIsNone(snapshot.workshop_updated_at_utc)
            self.assertIsNone(snapshot.workshop_manifest_id)
            self.assertNotIn("workshop_updated_at_utc", metadata)
            self.assertNotIn("workshop_manifest_id", metadata)

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

    def test_download_batch_reports_item_percentages_and_snapshot_progress(self) -> None:
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
                (mod / "mod.info").write_text(
                    f"id=Mod{workshop_id}\n",
                    encoding="utf-8",
                )
            client = SteamCmdClient(executable, library)
            events: list[tuple[int, int, str]] = []

            def fake_download(
                _workshop_ids: tuple[str, ...],
                _credentials: SteamCredentials,
                _timeout: int,
                output_callback: object = None,
            ) -> SteamCmdResult:
                assert callable(output_callback)
                output_callback("workshop_download_item 108600 111 validate")
                output_callback("Update state (0x5), progress: 50.00")
                output_callback("Success. Downloaded item 111 to cache")
                output_callback("workshop_download_item 108600 222 validate")
                output_callback("Success. Downloaded item 222 to cache")
                return SteamCmdResult(True, 0, "Success")

            with patch.object(client, "download", side_effect=fake_download):
                batch = download_and_snapshot(
                    client,
                    ("111", "222"),
                    SteamCredentials.anonymous(),
                    root / "snapshots",
                    progress=lambda current, total, message: events.append(
                        (current, total, message)
                    ),
                )

            self.assertTrue(batch.command_result.success)
            self.assertEqual(events[0][0], 0)
            self.assertEqual(events[-1][0], 100)
            self.assertTrue(any("111): 50%" in event[2] for event in events))
            self.assertTrue(any("Downloaded 2/2" in event[2] for event in events))
            self.assertTrue(any("Snapshot complete 2/2" in event[2] for event in events))
            self.assertEqual([event[0] for event in events], sorted(event[0] for event in events))


if __name__ == "__main__":
    unittest.main()
