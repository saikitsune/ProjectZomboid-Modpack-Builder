import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit

from pzmodpack.backend import BuildReport
from pzmodpack.gui import ModpackWindow
from pzmodpack.project import load_project
from pzmodpack.steamcmd import (
    DownloadBatchResult,
    SteamCmdResult,
    WorkshopSnapshot,
    WorkshopUploadResult,
)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_window_builds_using_the_shared_backend(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            mod = source / "mods" / "Example"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("name=Example\nid=ExampleId\n", encoding="utf-8")
            destination = root / "built"
            window = ModpackWindow(run_async=False, persist_session=False)
            self.assertIn("v0.4.2", window.windowTitle())
            window.name_edit.setText("GUI Pack")
            window.namespace_edit.setText("GuiPack")
            window.workshop_edit.setText("123")
            window.output_edit.setText(str(destination))
            window.add_source_path(source)

            window.build_pack()

            self.assertTrue((destination / "manifest.json").is_file())
            self.assertIn("Built 1 mod", window.log.toPlainText())
            window.close()

    def test_async_build_keeps_gui_responsive_and_shows_progress(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            started = threading.Event()
            release = threading.Event()
            window = ModpackWindow(run_async=True, persist_session=False)
            window.show()
            self.application.processEvents()
            window.add_source_path(source)
            window.output_edit.setText(str(root / "built"))

            def slow_build(_config: object, progress: object = None) -> BuildReport:
                started.set()
                if callable(progress):
                    progress(35, 100, "Copying test mod")
                release.wait(timeout=3)
                return BuildReport(root / "built", 1, {}, (), ())

            with patch("pzmodpack.gui.build_modpack", side_effect=slow_build):
                window.build_pack()
                deadline = time.monotonic() + 2
                while not started.is_set() and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

                self.assertTrue(started.is_set())
                self.application.processEvents()
                self.assertFalse(window.build_button.isEnabled())
                self.assertTrue(window.build_progress.isVisible())
                self.assertIn("Copying test mod", window.build_status.text())

                release.set()
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

            self.assertFalse(window._workers)
            self.assertTrue(window.build_button.isEnabled())
            self.assertFalse(window.build_progress.isVisible())
            self.assertIn("Built 1 mod", window.log.toPlainText())
            window.close()

    def test_account_login_uses_password_field_without_logging_secrets(self) -> None:
        window = ModpackWindow(run_async=False, persist_session=False)
        window.anonymous_check.setChecked(False)
        window.username_edit.setText("sai")
        window.password_edit.setText("super-secret")
        window.guard_edit.setText("ABCDE")

        credentials = window.steam_credentials()

        self.assertEqual(credentials.username, "sai")
        self.assertEqual(credentials.password, "super-secret")
        self.assertEqual(credentials.guard_code, "ABCDE")
        self.assertEqual(window.password_edit.echoMode(), QLineEdit.EchoMode.Password)
        with patch("pzmodpack.gui.SteamCmdClient.test_login") as login:
            login.return_value = SteamCmdResult(True, 0, "Logged in OK")
            window.test_steam_login()
        log = window.log.toPlainText()
        self.assertIn("Steam login succeeded", log)
        self.assertIn("PZ Modpack Builder v0.4.2", log)
        self.assertNotIn("super-secret", log)
        self.assertNotIn("ABCDE", log)
        self.assertEqual(window.password_edit.text(), "")
        self.assertEqual(window.guard_edit.text(), "")
        window.close()

    def test_account_login_persists_cached_session_without_password(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            session_file = Path(temporary_directory) / "steam-session.json"
            window = ModpackWindow(run_async=False, session_file=session_file)
            window.anonymous_check.setChecked(False)
            window.username_edit.setText("sai")
            window.password_edit.setText("secret")

            with patch("pzmodpack.gui.SteamCmdClient.test_login") as login:
                login.return_value = SteamCmdResult(True, 0, "Logged in OK")
                window.test_steam_login()
            serialized = session_file.read_text(encoding="utf-8")
            window.close()

            restored = ModpackWindow(run_async=False, session_file=session_file)
            self.assertFalse(restored.anonymous_check.isChecked())
            self.assertEqual(restored.username_edit.text(), "sai")
            self.assertEqual(restored.password_edit.text(), "")
            self.assertIn("Cached SteamCMD account: sai", restored.login_status_label.text())
            self.assertNotIn("secret", serialized)
            self.assertNotIn("password", serialized.lower())
            restored.close()

    def test_downloaded_snapshots_are_added_to_pack_sources(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            snapshot_path = Path(temporary_directory) / "snapshots" / "111" / "abc"
            snapshot_path.mkdir(parents=True)
            window = ModpackWindow(run_async=False, persist_session=False)
            window.workshop_input.setPlainText(
                "https://steamcommunity.com/sharedfiles/filedetails/?id=111"
            )
            batch = DownloadBatchResult(
                command_result=SteamCmdResult(True, 0, "Downloaded"),
                snapshots=(WorkshopSnapshot("111", "abcdef", snapshot_path),),
            )

            with patch("pzmodpack.gui.download_and_snapshot", return_value=batch) as download:
                window.download_workshop_items()

            self.assertEqual(window.source_paths(), (snapshot_path.resolve(),))
            self.assertEqual(download.call_args.args[1], ("111",))
            self.assertIn("Locked Workshop 111", window.log.toPlainText())
            window.close()

    def test_authenticated_user_can_upload_built_pack_and_capture_new_id(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "build"
            output.mkdir()
            window = ModpackWindow(run_async=False, persist_session=False)
            window.output_edit.setText(str(output))
            window.workshop_edit.setText("0")
            self.assertIn("Create new Workshop item", window.upload_destination_label.text())
            window.anonymous_check.setChecked(False)
            window.username_edit.setText("sai")
            window.password_edit.setText("secret")
            window.upload_change_edit.setPlainText("Initial release")
            window.upload_permission_check.setChecked(True)
            result = WorkshopUploadResult(
                SteamCmdResult(True, 0, "Success"),
                "555",
                output / "workshop_upload.vdf",
            )

            with patch("pzmodpack.gui.upload_modpack", return_value=result) as upload:
                window.upload_built_modpack()

            self.assertEqual(upload.call_args.args[1], output.resolve())
            self.assertEqual(upload.call_args.args[3], "Initial release")
            self.assertEqual(window.workshop_edit.text(), "555")
            self.assertIn("Update Workshop item 555", window.upload_destination_label.text())
            self.assertIn("Workshop upload succeeded", window.log.toPlainText())
            self.assertEqual(window.password_edit.text(), "")
            window.close()

    def test_project_settings_can_be_saved_and_loaded_without_credentials(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_file = root / "pack.pzpack.json"
            source = root / "snapshot"
            source.mkdir()
            window = ModpackWindow(run_async=False, persist_session=False)
            window.name_edit.setText("Saved Pack")
            window.namespace_edit.setText("Saved")
            window.workshop_edit.setText("999")
            window.output_edit.setText(str(root / "output"))
            window.steamcmd_edit.setText(str(root / "steamcmd.exe"))
            window.library_edit.setText(str(root / "library"))
            window.snapshot_edit.setText(str(root / "snapshots"))
            preview = root / "preview.png"
            preview.write_bytes(b"png")
            window.preview_edit.setText(str(preview))
            window.visibility_combo.setCurrentIndex(3)
            window.active_ids_edit.setPlainText("Example=ExampleB41")
            window.workshop_input.setPlainText("111\n222")
            window.add_source_path(source)
            window.username_edit.setText("should-not-save")
            window.password_edit.setText("secret")

            window.save_project_to(project_file)
            saved = load_project(project_file)
            restored = ModpackWindow(run_async=False, persist_session=False)
            restored.load_project_from(project_file)

            self.assertEqual(saved.name, "Saved Pack")
            self.assertEqual(restored.name_edit.text(), "Saved Pack")
            self.assertEqual(restored.source_paths(), (source.resolve(),))
            self.assertEqual(restored.preview_edit.text(), str(preview))
            self.assertEqual(restored.visibility_combo.currentData(), 3)
            self.assertEqual(restored.active_ids_edit.toPlainText(), "Example=ExampleB41")
            self.assertEqual(restored.username_edit.text(), "")
            self.assertEqual(restored.password_edit.text(), "")
            window.close()
            restored.close()


if __name__ == "__main__":
    unittest.main()
