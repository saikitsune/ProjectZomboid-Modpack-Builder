import json
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QMessageBox

from pzmodpack.backend import BuildReport, discover_mods
from pzmodpack.gui import (
    BundledModSelectionDialog,
    ModpackWindow,
    WorkshopSnapshotSelectionDialog,
)
from pzmodpack.project import load_project
from pzmodpack.steamcmd import (
    DownloadBatchResult,
    SteamCmdResult,
    WorkshopSnapshot,
    WorkshopUploadResult,
)


def _stored_snapshot_fixture(
    snapshot_root: Path,
    workshop_id: str,
    sha256: str,
    captured_at: str,
    *,
    folder: str = "Example",
    updated_at: str | None = None,
) -> Path:
    snapshot = snapshot_root / workshop_id / sha256[:16]
    mod = snapshot / "mods" / folder / "42"
    mod.mkdir(parents=True)
    (mod / "mod.info").write_text(
        f"name={folder}\nid={folder}Id\n",
        encoding="utf-8",
    )
    metadata: dict[str, object] = {
        "format_version": 2,
        "workshop_id": workshop_id,
        "sha256": sha256,
        "snapshot_created_at_utc": captured_at,
    }
    if updated_at is not None:
        metadata["workshop_updated_at_utc"] = updated_at
    (snapshot / "snapshot.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    return snapshot


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
            self.assertIn("v0.7.0", window.windowTitle())
            window.name_edit.setText("GUI Pack")
            window.namespace_edit.setText("GuiPack")
            window.workshop_edit.setText("123")
            window.output_edit.setText(str(destination))
            window.add_source_path(source)

            window.build_pack()

            self.assertTrue((destination / "manifest.json").is_file())
            self.assertIn("Built 1 mod", window.log.toPlainText())
            self.assertIn("GUI Pack v1.0.0", window.upload_change_edit.toPlainText())
            self.assertIn("Current: v1.0.0", window.version_status_label.text())
            window.close()

    def test_async_build_keeps_gui_responsive_and_shows_progress(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            mod = source / "mods" / "Example" / "42"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text(
                "name=Example\nid=ExampleId\n",
                encoding="utf-8",
            )
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
                self.assertFalse(window.output_edit.isEnabled())
                self.assertFalse(window.namespace_edit.isEnabled())
                self.assertFalse(window.browse_output_button.isEnabled())

                release.set()
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

            self.assertFalse(window._workers)
            self.assertTrue(window.build_button.isEnabled())
            self.assertFalse(window.build_progress.isVisible())
            self.assertTrue(window.output_edit.isEnabled())
            self.assertTrue(window.namespace_edit.isEnabled())
            self.assertTrue(window.browse_output_button.isEnabled())
            self.assertIn("Built 1 mod", window.log.toPlainText())
            window.close()

    def test_window_refuses_to_close_while_operation_is_active(self) -> None:
        window = ModpackWindow(run_async=False, persist_session=False)
        window.show()
        self.application.processEvents()
        window._set_download_running(True)

        with patch(
            "pzmodpack.gui.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Ok,
        ) as warning:
            closed = window.close()

        self.assertFalse(closed)
        self.assertTrue(window.isVisible())
        warning.assert_called_once()
        window._set_download_running(False)
        self.assertTrue(window.close())

    def test_bundled_mod_dialog_prevents_conflicting_selections(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            main = root / "mods" / "WaterPipes" / "42"
            removed = root / "mods" / "WaterPipesRemoved" / "42"
            main.mkdir(parents=True)
            removed.mkdir(parents=True)
            (main / "mod.info").write_text(
                "name=Waterpipes\nid=Waterpipes\n"
                "description=The main water-pipe mod.\n",
                encoding="utf-8",
            )
            (removed / "mod.info").write_text(
                "name=Waterpipes Removed\nid=WaterpipesRemoved\n"
                "incompatible=Waterpipes\n",
                encoding="utf-8",
            )
            dialog = BundledModSelectionDialog(discover_mods([root]), None)
            ok_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
            first = dialog.tree.topLevelItem(0)
            second = dialog.tree.topLevelItem(1)

            self.assertFalse(ok_button.isEnabled())
            second.setCheckState(0, Qt.CheckState.Unchecked)
            self.assertTrue(ok_button.isEnabled())
            self.assertEqual(dialog.selected_mod_ids(), ("Waterpipes",))

            second.setCheckState(0, Qt.CheckState.Checked)
            self.assertEqual(first.checkState(0), Qt.CheckState.Unchecked)
            self.assertEqual(dialog.selected_mod_ids(), ("WaterpipesRemoved",))
            self.assertIn("Deselected incompatible", dialog.status.text())
            dialog.close()

    def test_snapshot_dialog_groups_revisions_and_marks_latest_with_dates(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshots = []
            for name, sha256, updated in (
                ("old", "a" * 64, "2026-08-18T12:00:00+00:00"),
                ("latest", "b" * 64, "2026-08-19T12:00:00+00:00"),
            ):
                snapshot = root / "snapshots" / "111" / name
                mod = snapshot / "mods" / "Example" / "42"
                mod.mkdir(parents=True)
                (mod / "mod.info").write_text(
                    "name=Example Mod\nid=ExampleId\n",
                    encoding="utf-8",
                )
                (snapshot / "snapshot.json").write_text(
                    json.dumps(
                        {
                            "format_version": 2,
                            "workshop_id": "111",
                            "sha256": sha256,
                            "snapshot_created_at_utc": updated,
                            "workshop_updated_at_utc": updated,
                        }
                    ),
                    encoding="utf-8",
                )
                snapshots.append(snapshot)

            dialog = WorkshopSnapshotSelectionDialog(
                discover_mods(snapshots),
            )
            item = dialog.tree.topLevelItem(0)
            combo = dialog._combos["111"]

            self.assertEqual(dialog.tree.topLevelItemCount(), 1)
            self.assertEqual(combo.count(), 2)
            self.assertEqual(combo.currentData(), "b" * 64)
            self.assertIn("LATEST", combo.currentText())
            self.assertIn("2026-08-19", item.text(2))
            self.assertEqual(item.text(4), "Latest downloaded")

            combo.setCurrentIndex(1)

            self.assertEqual(dialog.selected_revisions(), {"111": "a" * 64})
            self.assertIn("2026-08-18", item.text(2))
            self.assertEqual(item.text(4), "Pinned older snapshot")
            dialog.close()

    def test_snapshot_dialog_labels_legacy_workshop_date_unknown(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            snapshot = Path(temporary_directory) / "snapshots" / "111" / "legacy"
            mod = snapshot / "mods" / "Example" / "42"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text("id=ExampleId\n", encoding="utf-8")
            (snapshot / "snapshot.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "workshop_id": "111",
                        "sha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )

            dialog = WorkshopSnapshotSelectionDialog(discover_mods((snapshot,)))
            item = dialog.tree.topLevelItem(0)

            self.assertEqual(item.text(2), "Unknown")
            self.assertNotEqual(item.text(3), "Unknown")
            dialog.close()

    def test_build_prompts_once_and_bundles_only_latest_snapshot_revision(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshots = []
            for name, sha256, updated, content in (
                ("old", "a" * 64, "2026-08-18T12:00:00+00:00", "old"),
                ("latest", "b" * 64, "2026-08-19T12:00:00+00:00", "latest"),
            ):
                snapshot = root / "snapshots" / "111" / name
                mod = snapshot / "mods" / "Example" / "42"
                mod.mkdir(parents=True)
                (mod / "mod.info").write_text(
                    "name=Example Mod\nid=ExampleId\n",
                    encoding="utf-8",
                )
                (mod / "content.txt").write_text(content, encoding="utf-8")
                (snapshot / "snapshot.json").write_text(
                    json.dumps(
                        {
                            "format_version": 2,
                            "workshop_id": "111",
                            "sha256": sha256,
                            "snapshot_created_at_utc": updated,
                            "workshop_updated_at_utc": updated,
                        }
                    ),
                    encoding="utf-8",
                )
                snapshots.append(snapshot)
            window = ModpackWindow(run_async=False, persist_session=False)
            window.output_edit.setText(str(root / "output"))
            for snapshot in snapshots:
                window.add_source_path(snapshot)

            with patch.object(
                WorkshopSnapshotSelectionDialog,
                "exec",
                return_value=WorkshopSnapshotSelectionDialog.DialogCode.Accepted,
            ) as execute:
                window.build_pack()

            execute.assert_called_once()
            self.assertEqual(window.snapshot_selections, {"111": "b" * 64})
            self.assertEqual(
                (
                    root
                    / "output"
                    / "Contents"
                    / "mods"
                    / "MyPack_Example"
                    / "42"
                    / "content.txt"
                ).read_text(encoding="utf-8"),
                "latest",
            )
            self.assertNotIn("duplicate", window.log.toPlainText().lower())
            window.close()

    def test_bundled_mod_dialog_auto_selects_and_locks_required_mods(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dependent = root / "mods" / "Dependent" / "42"
            framework = root / "mods" / "Framework" / "42"
            dependent.mkdir(parents=True)
            framework.mkdir(parents=True)
            (dependent / "mod.info").write_text(
                "name=Dependent\nid=DependentId\nrequire=FrameworkId\n",
                encoding="utf-8",
            )
            (framework / "mod.info").write_text(
                "name=Framework\nid=FrameworkId\n",
                encoding="utf-8",
            )

            dialog = BundledModSelectionDialog(
                discover_mods([root]),
                ("DependentId",),
            )
            ok_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
            dependent_item = next(
                dialog.tree.topLevelItem(index)
                for index in range(dialog.tree.topLevelItemCount())
                if dialog.tree.topLevelItem(index).text(2) == "DependentId"
            )
            framework_item = next(
                dialog.tree.topLevelItem(index)
                for index in range(dialog.tree.topLevelItemCount())
                if dialog.tree.topLevelItem(index).text(2) == "FrameworkId"
            )

            self.assertEqual(
                dialog.selected_mod_ids(),
                ("DependentId", "FrameworkId"),
            )
            self.assertEqual(dependent_item.text(3), "FrameworkId")
            self.assertTrue(ok_button.isEnabled())

            framework_item.setCheckState(0, Qt.CheckState.Unchecked)

            self.assertEqual(framework_item.checkState(0), Qt.CheckState.Checked)
            self.assertIn("required by Dependent", dialog.status.text())
            dialog.close()

    def test_bundled_mod_dialog_identifies_missing_requirements(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dependent = root / "mods" / "Dependent" / "42"
            dependent.mkdir(parents=True)
            (dependent / "mod.info").write_text(
                "name=Dependent\nid=DependentId\nrequire=MissingFramework\n",
                encoding="utf-8",
            )

            dialog = BundledModSelectionDialog(discover_mods([root]), None)
            ok_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

            self.assertEqual(
                dialog.tree.topLevelItem(0).text(3),
                "MissingFramework [MISSING]",
            )
            self.assertFalse(ok_button.isEnabled())
            self.assertIn("MissingFramework", dialog.status.text())
            self.assertIn("Workshop items or source folders", dialog.status.text())
            dialog.close()

    def test_build_opens_selection_dialog_for_bundled_conflicts(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "source" / "mods" / "First" / "42"
            second = root / "source" / "mods" / "Second" / "42"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "mod.info").write_text(
                "name=First\nid=FirstId\nincompatible=SecondId\n",
                encoding="utf-8",
            )
            (second / "mod.info").write_text(
                "name=Second\nid=SecondId\n",
                encoding="utf-8",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.add_source_path(root / "source")
            window.output_edit.setText(str(root / "output"))

            with patch.object(
                BundledModSelectionDialog,
                "exec",
                return_value=BundledModSelectionDialog.DialogCode.Rejected,
            ) as execute:
                window.build_pack()

            execute.assert_called_once()
            self.assertFalse((root / "output").exists())
            self.assertIn("Build cancelled", window.log.toPlainText())
            window.close()

    def test_build_opens_selection_dialog_for_excluded_requirement(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dependent = root / "source" / "mods" / "Dependent" / "42"
            framework = root / "source" / "mods" / "Framework" / "42"
            dependent.mkdir(parents=True)
            framework.mkdir(parents=True)
            (dependent / "mod.info").write_text(
                "id=DependentId\nrequire=FrameworkId\n",
                encoding="utf-8",
            )
            (framework / "mod.info").write_text(
                "id=FrameworkId\n",
                encoding="utf-8",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.add_source_path(root / "source")
            window.output_edit.setText(str(root / "output"))
            window.included_mod_ids = ("DependentId",)

            with patch.object(
                BundledModSelectionDialog,
                "exec",
                return_value=BundledModSelectionDialog.DialogCode.Rejected,
            ) as execute:
                window.build_pack()

            execute.assert_called_once()
            self.assertFalse((root / "output").exists())
            self.assertIn("required-mod issue", window.log.toPlainText())
            window.close()

    def test_build_forces_bundled_selection_after_snapshot_change(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mod = root / "source" / "mods" / "Example" / "42"
            mod.mkdir(parents=True)
            (mod / "mod.info").write_text(
                "name=Example\nid=ExampleId\n",
                encoding="utf-8",
            )
            destination = root / "built"
            window = ModpackWindow(run_async=False, persist_session=False)
            window.add_source_path(root / "source")
            window.output_edit.setText(str(destination))
            window.included_mod_ids = ("ExampleId",)
            window._mod_selection_needs_review = True

            with patch.object(
                window,
                "_show_bundled_mod_selection_dialog",
                return_value=False,
            ) as selection:
                window.build_pack()

            selection.assert_called_once()
            self.assertFalse(destination.exists())
            self.assertIn("snapshot changed", window.log.toPlainText())
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
        self.assertIn("PZ Modpack Builder v0.7.0", log)
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

    def test_new_download_follows_latest_unless_older_snapshot_is_pinned(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            def make_snapshot(_name: str, sha256: str, updated: str) -> Path:
                snapshot = root / "snapshots" / "111" / sha256[:16]
                mod = snapshot / "mods" / "Example" / "42"
                mod.mkdir(parents=True)
                (mod / "mod.info").write_text(
                    "name=Example\nid=ExampleId\n",
                    encoding="utf-8",
                )
                (snapshot / "snapshot.json").write_text(
                    json.dumps(
                        {
                            "format_version": 2,
                            "workshop_id": "111",
                            "sha256": sha256,
                            "snapshot_created_at_utc": updated,
                            "workshop_updated_at_utc": updated,
                        }
                    ),
                    encoding="utf-8",
                )
                return snapshot

            old_hash = "a" * 64
            new_hash = "b" * 64
            newest_hash = "c" * 64
            old = make_snapshot("old", old_hash, "2026-08-17T12:00:00+00:00")
            new = make_snapshot("new", new_hash, "2026-08-18T12:00:00+00:00")
            newest = make_snapshot(
                "newest",
                newest_hash,
                "2026-08-19T12:00:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.workshop_input.setPlainText("111")
            window.add_source_path(old)
            window.snapshot_selections = {"111": old_hash}
            window.included_mod_ids = ("ExampleId",)

            first_batch = DownloadBatchResult(
                SteamCmdResult(True, 0, "Downloaded"),
                (
                    WorkshopSnapshot(
                        "111",
                        new_hash,
                        new,
                        "2026-08-18T12:05:00+00:00",
                        "2026-08-18T12:00:00+00:00",
                    ),
                ),
            )
            with patch(
                "pzmodpack.gui.download_and_snapshot",
                return_value=first_batch,
            ):
                window.download_workshop_items()

            self.assertEqual(window.snapshot_selections, {"111": new_hash})
            self.assertEqual(window.included_mod_ids, ("ExampleId",))
            self.assertTrue(window._mod_selection_needs_review)

            window.snapshot_selections = {"111": old_hash}
            window._mod_selection_needs_review = False
            second_batch = DownloadBatchResult(
                SteamCmdResult(True, 0, "Downloaded"),
                (
                    WorkshopSnapshot(
                        "111",
                        newest_hash,
                        newest,
                        "2026-08-19T12:05:00+00:00",
                        "2026-08-19T12:00:00+00:00",
                    ),
                ),
            )
            with patch(
                "pzmodpack.gui.download_and_snapshot",
                return_value=second_batch,
            ):
                window.download_workshop_items()

            self.assertEqual(window.snapshot_selections, {"111": old_hash})
            self.assertFalse(window._mod_selection_needs_review)
            window.close()

    def test_async_workshop_download_shows_live_progress(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_path = root / "snapshots" / "111" / "abc"
            snapshot_path.mkdir(parents=True)
            started = threading.Event()
            release = threading.Event()
            window = ModpackWindow(run_async=True, persist_session=False)
            window.show()
            self.application.processEvents()
            window.workshop_input.setPlainText("111")
            batch = DownloadBatchResult(
                command_result=SteamCmdResult(True, 0, "Downloaded"),
                snapshots=(WorkshopSnapshot("111", "abcdef", snapshot_path),),
            )

            def slow_download(
                _client: object,
                _workshop_ids: object,
                _credentials: object,
                _snapshot_root: object,
                progress: object = None,
            ) -> DownloadBatchResult:
                started.set()
                if callable(progress):
                    progress(42, 100, "Downloading Workshop item 1/1 (111): 50%")
                release.wait(timeout=3)
                return batch

            with patch("pzmodpack.gui.download_and_snapshot", side_effect=slow_download):
                window.download_workshop_items()
                deadline = time.monotonic() + 2
                while not started.is_set() and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

                self.assertTrue(started.is_set())
                self.application.processEvents()
                self.assertFalse(window.download_button.isEnabled())
                self.assertFalse(window.download_progress.isHidden())
                self.assertEqual(window.download_progress.value(), 42)
                self.assertIn("Workshop item 1/1", window.download_status.text())

                release.set()
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

            self.assertFalse(window._workers)
            self.assertTrue(window.download_button.isEnabled())
            self.assertFalse(window.download_progress.isVisible())
            self.assertIn("Locked Workshop 111", window.log.toPlainText())
            window.close()

    def test_manager_tab_groups_stored_snapshots_and_marks_project_state(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            old_hash = "a" * 64
            new_hash = "b" * 64
            old = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                old_hash,
                "2026-08-18T12:05:00+00:00",
                updated_at="2026-08-18T12:00:00+00:00",
            )
            _stored_snapshot_fixture(
                snapshot_root,
                "111",
                new_hash,
                "2026-08-19T12:05:00+00:00",
                updated_at="2026-08-19T12:00:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(old)
            window.snapshot_selections = {"111": old_hash}

            window.refresh_managed_downloads()

            self.assertIn(
                "Manage downloads",
                [window.tabs.tabText(index) for index in range(window.tabs.count())],
            )
            self.assertEqual(window.manager_tree.topLevelItemCount(), 1)
            parent = window.manager_tree.topLevelItem(0)
            self.assertIn("Workshop 111", parent.text(0))
            self.assertEqual(parent.childCount(), 2)
            self.assertEqual(parent.child(0).text(0), new_hash[:16])
            self.assertIn("Latest stored", parent.child(0).text(5))
            self.assertIn("In current pack", parent.child(1).text(5))
            self.assertIn("Pinned older", parent.child(1).text(5))
            window.manager_tree.setCurrentItem(parent.child(1))
            self.assertTrue(window.manager_update_button.isEnabled())
            self.assertTrue(window.manager_add_button.isEnabled())
            self.assertIn(old_hash, window.manager_details.toPlainText())
            window.close()

    def test_manager_update_of_global_item_does_not_add_it_to_current_pack(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            _stored_snapshot_fixture(
                snapshot_root,
                "111",
                "a" * 64,
                "2026-08-18T12:05:00+00:00",
            )
            new_path = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                "b" * 64,
                "2026-08-19T12:05:00+00:00",
            )
            batch = DownloadBatchResult(
                SteamCmdResult(True, 0, "Downloaded"),
                (
                    WorkshopSnapshot(
                        "111",
                        "b" * 64,
                        new_path,
                        "2026-08-19T12:05:00+00:00",
                        created=True,
                    ),
                ),
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.workshop_input.setPlainText("999")
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(window.manager_tree.topLevelItem(0))

            with patch(
                "pzmodpack.gui.download_and_snapshot",
                return_value=batch,
            ) as download:
                window.update_managed_workshop_item()

            self.assertEqual(download.call_args.args[1], ("111",))
            self.assertEqual(window.workshop_input.toPlainText(), "999")
            self.assertEqual(window.source_paths(), ())
            self.assertIn("new immutable snapshot", window.log.toPlainText())
            window.close()

    def test_manager_update_of_attached_item_selects_the_new_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            old_hash = "a" * 64
            new_hash = "b" * 64
            old = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                old_hash,
                "2026-08-18T12:05:00+00:00",
            )
            new = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                new_hash,
                "2026-08-19T12:05:00+00:00",
            )
            batch = DownloadBatchResult(
                SteamCmdResult(True, 0, "Downloaded"),
                (
                    WorkshopSnapshot(
                        "111",
                        new_hash,
                        new,
                        "2026-08-19T12:05:00+00:00",
                        created=True,
                    ),
                ),
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(old)
            window.snapshot_selections = {"111": old_hash}
            window.included_mod_ids = ("ExampleId",)
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(window.manager_tree.topLevelItem(0))

            with patch(
                "pzmodpack.gui.download_and_snapshot",
                return_value=batch,
            ):
                window.update_managed_workshop_item()

            self.assertEqual(window.source_paths(), (old.resolve(), new.resolve()))
            self.assertEqual(window.snapshot_selections, {"111": new_hash})
            self.assertEqual(window.included_mod_ids, ("ExampleId",))
            self.assertTrue(window._mod_selection_needs_review)
            window.close()

    def test_manager_lists_cache_only_item_as_updateable(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            library = root / "steam-library"
            cached_mod = (
                library
                / "steamapps"
                / "workshop"
                / "content"
                / "108600"
                / "111"
                / "mods"
                / "Cached Example"
            )
            cached_mod.mkdir(parents=True)
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(library))
            window.snapshot_edit.setText(str(root / "snapshots"))

            window.refresh_managed_downloads()
            parent = window.manager_tree.topLevelItem(0)
            window.manager_tree.setCurrentItem(parent)

            self.assertEqual(window.manager_tree.topLevelItemCount(), 1)
            self.assertEqual(parent.childCount(), 0)
            self.assertIn("Cached Example", parent.text(1))
            self.assertIn("SteamCMD cache present", parent.text(5))
            self.assertIn("1 SteamCMD cache item(s)", window.manager_summary_label.text())
            self.assertTrue(window.manager_update_button.isEnabled())
            self.assertFalse(window.manager_delete_snapshot_button.isEnabled())
            self.assertFalse(window.manager_delete_item_button.isEnabled())
            window.close()

    def test_async_manager_update_shows_progress_and_locks_mutations(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            sha256 = "a" * 64
            snapshot = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                sha256,
                "2026-08-19T12:05:00+00:00",
            )
            batch = DownloadBatchResult(
                SteamCmdResult(True, 0, "Downloaded"),
                (
                    WorkshopSnapshot(
                        "111",
                        sha256,
                        snapshot,
                        "2026-08-19T12:05:00+00:00",
                    ),
                ),
            )
            started = threading.Event()
            release = threading.Event()
            window = ModpackWindow(run_async=True, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(window.manager_tree.topLevelItem(0))
            window.tabs.setCurrentIndex(window.manager_tab_index)
            window.show()
            self.application.processEvents()

            def slow_download(
                _client: object,
                _ids: object,
                _credentials: object,
                _snapshot_root: object,
                *,
                progress: object = None,
            ) -> DownloadBatchResult:
                started.set()
                if callable(progress):
                    progress(45, 100, "Workshop item 1/1: downloading")
                release.wait(timeout=3)
                return batch

            with patch(
                "pzmodpack.gui.download_and_snapshot",
                side_effect=slow_download,
            ):
                window.update_managed_workshop_item()
                deadline = time.monotonic() + 2
                while not started.is_set() and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

                self.assertTrue(started.is_set())
                self.application.processEvents()
                self.assertTrue(window.manager_progress.isVisible())
                self.assertFalse(window.download_progress.isVisible())
                self.assertFalse(window.manager_update_button.isEnabled())
                self.assertFalse(window.open_project_button.isEnabled())
                self.assertFalse(window.snapshot_edit.isEnabled())
                self.assertIn("Workshop item 1/1", window.manager_operation_status.text())

                release.set()
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

            self.assertFalse(window._workers)
            self.assertFalse(window.manager_progress.isVisible())
            self.assertTrue(window.manager_update_button.isEnabled())
            self.assertTrue(window.open_project_button.isEnabled())
            self.assertTrue(window.snapshot_edit.isEnabled())
            window.close()

    def test_async_manager_update_failure_restores_controls(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            _stored_snapshot_fixture(
                snapshot_root,
                "111",
                "a" * 64,
                "2026-08-19T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=True, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(window.manager_tree.topLevelItem(0))
            window.show()
            self.application.processEvents()

            def failed_download(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("simulated SteamCMD failure")

            with patch(
                "pzmodpack.gui.download_and_snapshot",
                side_effect=failed_download,
            ):
                window.update_managed_workshop_item()
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)

            self.assertFalse(window._workers)
            self.assertFalse(window.manager_progress.isVisible())
            self.assertTrue(window.manager_update_button.isEnabled())
            self.assertTrue(window.open_project_button.isEnabled())
            self.assertIn("simulated SteamCMD failure", window.log.toPlainText())
            window.close()

    def test_manager_can_attach_a_specific_stored_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory) / "snapshots"
            sha256 = "a" * 64
            snapshot = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                sha256,
                "2026-08-18T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(
                str(Path(temporary_directory) / "steam-library")
            )
            window.snapshot_edit.setText(str(snapshot_root))
            window.refresh_managed_downloads()
            child = window.manager_tree.topLevelItem(0).child(0)
            window.manager_tree.setCurrentItem(child)

            window.add_managed_snapshot_to_pack()

            self.assertEqual(window.source_paths(), (snapshot.resolve(),))
            self.assertEqual(window.snapshot_selections, {"111": sha256})
            refreshed_child = window.manager_tree.topLevelItem(0).child(0)
            self.assertIn("Selected latest", refreshed_child.text(5))
            window.close()

    def test_manager_use_of_already_effective_snapshot_does_not_require_review(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            sha256 = "a" * 64
            snapshot = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                sha256,
                "2026-08-19T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(snapshot)
            window.included_mod_ids = ("ExampleId",)
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(
                window.manager_tree.topLevelItem(0).child(0)
            )

            window.add_managed_snapshot_to_pack()

            self.assertEqual(window.snapshot_selections, {"111": sha256})
            self.assertFalse(window._mod_selection_needs_review)
            window.close()

    def test_manager_delete_selected_snapshot_falls_back_without_resetting_mods(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory) / "snapshots"
            old_hash = "a" * 64
            new_hash = "b" * 64
            old = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                old_hash,
                "2026-08-18T12:05:00+00:00",
            )
            new = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                new_hash,
                "2026-08-19T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(
                str(Path(temporary_directory) / "steam-library")
            )
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(old)
            window.add_source_path(new)
            window.snapshot_selections = {"111": new_hash}
            window.included_mod_ids = ("ExampleId",)
            window.refresh_managed_downloads()
            newest = window.manager_tree.topLevelItem(0).child(0)
            window.manager_tree.setCurrentItem(newest)

            with patch(
                "pzmodpack.gui.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.delete_managed_snapshot()

            self.assertFalse(new.exists())
            self.assertTrue(old.exists())
            self.assertEqual(window.source_paths(), (old.resolve(),))
            self.assertEqual(window.snapshot_selections, {"111": old_hash})
            self.assertEqual(window.included_mod_ids, ("ExampleId",))
            self.assertIn("review required", window.mod_selection_label.text())
            window.close()

    def test_manager_delete_confirmation_cancel_is_a_no_op(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            sha256 = "a" * 64
            snapshot = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                sha256,
                "2026-08-19T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(snapshot)
            window.snapshot_selections = {"111": sha256}
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(
                window.manager_tree.topLevelItem(0).child(0)
            )

            with patch(
                "pzmodpack.gui.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ):
                window.delete_managed_snapshot()

            self.assertTrue(snapshot.exists())
            self.assertEqual(window.source_paths(), (snapshot.resolve(),))
            self.assertEqual(window.snapshot_selections, {"111": sha256})
            self.assertFalse(window._manager_delete_running)
            window.close()

    def test_manager_delete_failure_restores_controls_and_project_state(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            sha256 = "a" * 64
            snapshot = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                sha256,
                "2026-08-19T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(snapshot)
            window.snapshot_selections = {"111": sha256}
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(
                window.manager_tree.topLevelItem(0).child(0)
            )

            with (
                patch(
                    "pzmodpack.gui.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch(
                    "pzmodpack.gui.delete_stored_workshop_snapshot",
                    side_effect=PermissionError("snapshot is in use"),
                ),
            ):
                window.delete_managed_snapshot()

            self.assertTrue(snapshot.exists())
            self.assertEqual(window.source_paths(), (snapshot.resolve(),))
            self.assertEqual(window.snapshot_selections, {"111": sha256})
            self.assertFalse(window._manager_delete_running)
            self.assertTrue(window.build_button.isEnabled())
            self.assertTrue(window.download_button.isEnabled())
            self.assertIn("snapshot is in use", window.log.toPlainText())
            window.close()

    def test_deleting_unattached_stale_selection_keeps_attached_revision(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            old_hash = "a" * 64
            new_hash = "b" * 64
            old = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                old_hash,
                "2026-08-18T12:05:00+00:00",
            )
            new = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                new_hash,
                "2026-08-19T12:05:00+00:00",
            )
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(old)
            window.snapshot_selections = {"111": new_hash}
            window.included_mod_ids = ("ExampleId",)
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(
                window.manager_tree.topLevelItem(0).child(0)
            )

            with patch(
                "pzmodpack.gui.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.delete_managed_snapshot()

            self.assertFalse(new.exists())
            self.assertTrue(old.exists())
            self.assertEqual(window.source_paths(), (old.resolve(),))
            self.assertEqual(window.snapshot_selections, {})
            self.assertFalse(window._mod_selection_needs_review)
            self.assertIn("attached snapshot did not change", window.log.toPlainText())
            window.close()

    def test_manager_delete_all_prunes_project_but_keeps_steam_cache(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            library = root / "steam-library"
            old = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                "a" * 64,
                "2026-08-18T12:05:00+00:00",
            )
            new = _stored_snapshot_fixture(
                snapshot_root,
                "111",
                "b" * 64,
                "2026-08-19T12:05:00+00:00",
            )
            cache = library / "steamapps" / "workshop" / "content" / "108600" / "111"
            cache.mkdir(parents=True)
            cache_sentinel = cache / "cached.txt"
            cache_sentinel.write_text("mutable Steam cache", encoding="utf-8")
            local_source = root / "local-mods"
            local_source.mkdir()

            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(library))
            window.snapshot_edit.setText(str(snapshot_root))
            window.add_source_path(old)
            window.add_source_path(new)
            window.add_source_path(local_source)
            window.snapshot_selections = {"111": "b" * 64}
            window.refresh_managed_downloads()
            window.manager_tree.setCurrentItem(window.manager_tree.topLevelItem(0))

            with patch(
                "pzmodpack.gui.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window.delete_managed_workshop_item()

            self.assertFalse((snapshot_root / "111").exists())
            self.assertTrue(cache_sentinel.is_file())
            self.assertEqual(window.source_paths(), (local_source.resolve(),))
            self.assertEqual(window.snapshot_selections, {})
            self.assertFalse(window._mod_selection_needs_review)
            self.assertEqual(window.manager_tree.topLevelItemCount(), 1)
            self.assertEqual(window.manager_tree.topLevelItem(0).childCount(), 0)
            window.close()

    def test_manager_surfaces_malformed_snapshot_without_allowing_attach(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshots"
            malformed = snapshot_root / "111" / ("a" * 16)
            malformed.mkdir(parents=True)
            (malformed / "snapshot.json").write_text("not json", encoding="utf-8")
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(root / "steam-library"))
            window.snapshot_edit.setText(str(snapshot_root))

            window.refresh_managed_downloads()
            child = window.manager_tree.topLevelItem(0).child(0)
            window.manager_tree.setCurrentItem(child)

            self.assertIn("storage warning", window.manager_summary_label.text())
            self.assertIn("Metadata warning", child.text(5))
            self.assertFalse(window.manager_add_button.isEnabled())
            self.assertTrue(window.manager_delete_snapshot_button.isEnabled())
            self.assertIn("malformed", window.manager_details.toPlainText())
            window.close()

    def test_manager_refuses_snapshot_root_overlapping_steam_cache(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            library = root / "steam-library"
            cache_root = (
                library / "steamapps" / "workshop" / "content" / "108600"
            )
            cache_mods = cache_root / "111" / "mods"
            cache_mods.mkdir(parents=True)
            sentinel = cache_mods / "keep.txt"
            sentinel.write_text("cache payload", encoding="utf-8")
            window = ModpackWindow(run_async=False, persist_session=False)
            window.library_edit.setText(str(library))
            window.snapshot_edit.setText(str(cache_root))

            window.refresh_managed_downloads()

            self.assertEqual(window.manager_tree.topLevelItemCount(), 0)
            self.assertIn("Unsafe snapshot library", window.manager_summary_label.text())
            self.assertFalse(window.manager_delete_snapshot_button.isEnabled())
            self.assertFalse(window.manager_delete_item_button.isEnabled())
            window.workshop_input.setPlainText("111")
            with patch("pzmodpack.gui.download_and_snapshot") as download:
                window.download_workshop_items()
            download.assert_not_called()
            self.assertIn("must be separate", window.log.toPlainText())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "cache payload")
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
            window.version_bump_combo.setCurrentIndex(1)
            window.active_ids_edit.setPlainText("Example=ExampleB41")
            window.workshop_input.setPlainText("111\n222")
            window.add_source_path(source)
            window.included_mod_ids = ("ExampleB41",)
            window._update_mod_selection_label()
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
            self.assertEqual(saved.version_bump, "minor")
            self.assertEqual(restored.version_bump_combo.currentData(), "minor")
            self.assertEqual(restored.visibility_combo.currentData(), 3)
            self.assertEqual(restored.active_ids_edit.toPlainText(), "Example=ExampleB41")
            self.assertEqual(restored.included_mod_ids, ("ExampleB41",))
            self.assertEqual(restored.username_edit.text(), "")
            self.assertEqual(restored.password_edit.text(), "")
            window.close()
            restored.close()


if __name__ == "__main__":
    unittest.main()
