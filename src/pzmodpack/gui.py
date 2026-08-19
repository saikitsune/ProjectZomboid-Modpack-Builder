from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .version import __version__
from .backend import (
    BuildConfig,
    BuildError,
    BuildReport,
    BundledModConflict,
    BundledModRequirement,
    DiscoveredMod,
    build_modpack,
    discover_mods,
    find_bundled_conflicts,
    find_mod_requirements,
    select_mods,
    validate_mods,
    validate_mod_selection,
)
from .project import ProjectSettings, load_project, save_project
from .session import (
    SavedSteamSession,
    clear_steam_session,
    load_steam_session,
    save_steam_session,
)
from .steamcmd import (
    SteamCmdClient,
    SteamCredentials,
    download_and_snapshot,
    install_steamcmd,
    parse_workshop_ids,
    upload_modpack,
)


class OperationWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation

    def run(self) -> None:
        try:
            self.succeeded.emit(self.operation())
        except Exception as error:  # noqa: BLE001 - Qt thread boundary must report all failures.
            self.failed.emit(str(error))


class WorkshopDownloadWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(
        self,
        client: SteamCmdClient,
        workshop_ids: tuple[str, ...],
        credentials: SteamCredentials,
        snapshot_root: Path,
    ) -> None:
        super().__init__()
        self.client = client
        self.workshop_ids = workshop_ids
        self.credentials = credentials
        self.snapshot_root = snapshot_root

    def run(self) -> None:
        try:
            result = download_and_snapshot(
                self.client,
                self.workshop_ids,
                self.credentials,
                self.snapshot_root,
                progress=self.progress.emit,
            )
            self.succeeded.emit(result)
        except Exception as error:  # noqa: BLE001 - Qt thread boundary reports failures.
            self.failed.emit(str(error))


class BuildWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, config: BuildConfig) -> None:
        super().__init__()
        self.config = config

    def run(self) -> None:
        try:
            report = build_modpack(self.config, progress=self.progress.emit)
            self.succeeded.emit(report)
        except Exception as error:  # noqa: BLE001 - Qt thread boundary must report all failures.
            self.failed.emit(str(error))


class BundledModSelectionDialog(QDialog):
    def __init__(
        self,
        mods: list[DiscoveredMod],
        included_mod_ids: tuple[str, ...] | None,
        parent: QWidget | None = None,
        active_mod_ids: dict[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select bundled mods")
        self.resize(1120, 690)
        self._mods = mods
        self._conflicts = find_bundled_conflicts(mods)
        self._requirements = find_mod_requirements(mods)
        self._active_mod_ids = active_mod_ids or {}
        self._updating = False
        selected_ids = (
            {mod_id for mod in mods for mod_id in mod.mod_ids}
            if included_mod_ids is None
            else set(included_mod_ids)
        )
        initially_selected = {
            mod for mod in mods if selected_ids.intersection(mod.mod_ids)
        }
        while True:
            additions = {
                requirement.providers[0]
                for requirement in self._requirements
                if requirement.declaring_mod in initially_selected
                and requirement.declaring_mod_id
                == self._active_mod_id(requirement.declaring_mod)
                and requirement.providers
                and not any(
                    provider in initially_selected
                    for provider in requirement.providers
                )
            }
            if not additions - initially_selected:
                break
            initially_selected.update(additions)
        selected_ids.update(
            mod_id for mod in initially_selected for mod_id in mod.mod_ids
        )

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Each row is one bundled mod folder. Select the variants to copy into the "
            "pack. Game-version directories inside a selected folder stay together. "
            "Required bundled mods are selected automatically. Missing requirements and "
            "incompatible combinations must be resolved before building."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            ("Include", "Bundled mod", "Mod ID(s)", "Requires", "Conflicts with")
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self._items: dict[DiscoveredMod, QTreeWidgetItem] = {}
        for mod in mods:
            item = QTreeWidgetItem(self.tree)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0,
                Qt.CheckState.Checked
                if selected_ids.intersection(mod.mod_ids)
                else Qt.CheckState.Unchecked,
            )
            item.setText(1, mod.display_name or mod.folder_name)
            item.setText(2, "; ".join(mod.mod_ids))
            item.setText(3, self._requirement_summary(mod))
            item.setText(4, self._conflict_summary(mod))
            item.setToolTip(1, f"Source folder: {mod.folder_name}")
            if mod.workshop_id:
                item.setToolTip(2, f"Workshop item {mod.workshop_id}")
            item.setToolTip(3, self._requirement_details(mod))
            self._items[mod] = item
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(170)
        self.details.setPlaceholderText("Select a row to view its source and description.")
        layout.addWidget(self.details)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(self._show_details)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._refresh_status()

    def _active_mod_id(self, mod: DiscoveredMod) -> str:
        selected = self._active_mod_ids.get(mod.folder_name, mod.mod_ids[-1])
        return selected if selected in mod.mod_ids else mod.mod_ids[-1]

    def _requirements_for(
        self,
        mod: DiscoveredMod,
    ) -> list[BundledModRequirement]:
        active_id = self._active_mod_id(mod)
        return [
            requirement
            for requirement in self._requirements
            if requirement.declaring_mod == mod
            and requirement.declaring_mod_id == active_id
        ]

    @staticmethod
    def _provider_names(requirement: BundledModRequirement) -> str:
        return " or ".join(
            provider.display_name or provider.folder_name
            for provider in requirement.providers
        )

    def _requirement_summary(self, mod: DiscoveredMod) -> str:
        summaries = []
        for requirement in self._requirements_for(mod):
            if requirement.is_missing:
                summaries.append(f"{requirement.required_mod_id} [MISSING]")
            else:
                summaries.append(requirement.required_mod_id)
        return "; ".join(summaries) or "None"

    def _requirement_details(self, mod: DiscoveredMod) -> str:
        requirements = self._requirements_for(mod)
        if not requirements:
            return "No required bundled Mod IDs declared by the active mod.info."
        lines = []
        for requirement in requirements:
            if requirement.is_missing:
                lines.append(
                    f"{requirement.required_mod_id}: missing; add its Workshop item or source"
                )
            else:
                lines.append(
                    f"{requirement.required_mod_id}: provided by "
                    f"{self._provider_names(requirement)}"
                )
        return "\n".join(lines)

    def _conflicts_for(self, mod: DiscoveredMod) -> list[BundledModConflict]:
        return [
            conflict
            for conflict in self._conflicts
            if conflict.declaring_mod == mod or conflict.incompatible_mod == mod
        ]

    def _other_mod(
        self,
        conflict: BundledModConflict,
        mod: DiscoveredMod,
    ) -> DiscoveredMod:
        return (
            conflict.incompatible_mod
            if conflict.declaring_mod == mod
            else conflict.declaring_mod
        )

    def _conflict_summary(self, mod: DiscoveredMod) -> str:
        ids = {
            conflict.incompatible_mod_id
            if conflict.declaring_mod == mod
            else conflict.declaring_mod_id
            for conflict in self._conflicts_for(mod)
        }
        return "; ".join(sorted(ids)) or "None"

    def _is_checked(self, mod: DiscoveredMod) -> bool:
        return self._items[mod].checkState(0) == Qt.CheckState.Checked

    def _mod_for_item(self, item: QTreeWidgetItem) -> DiscoveredMod | None:
        return next(
            (mod for mod, candidate in self._items.items() if candidate is item),
            None,
        )

    def _requirement_closure(
        self,
        mod: DiscoveredMod,
        selected: set[DiscoveredMod],
    ) -> set[DiscoveredMod]:
        closure = {mod}
        pending = [mod]
        while pending:
            current = pending.pop()
            for requirement in self._requirements_for(current):
                if not requirement.providers:
                    continue
                provider = next(
                    (
                        candidate
                        for candidate in requirement.providers
                        if candidate in selected or candidate in closure
                    ),
                    requirement.providers[0],
                )
                if provider not in closure:
                    closure.add(provider)
                    pending.append(provider)
        return closure

    def _required_by_selection(
        self,
        mod: DiscoveredMod,
        selected: set[DiscoveredMod],
    ) -> list[DiscoveredMod]:
        dependents = []
        for declaring_mod in selected:
            for requirement in self._requirements_for(declaring_mod):
                if mod not in requirement.providers:
                    continue
                if any(
                    provider != mod and provider in selected
                    for provider in requirement.providers
                ):
                    continue
                dependents.append(declaring_mod)
                break
        return dependents

    def _set_checked_mods(self, selected: set[DiscoveredMod]) -> None:
        self._updating = True
        try:
            for mod, item in self._items.items():
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if mod in selected
                    else Qt.CheckState.Unchecked,
                )
        finally:
            self._updating = False

    def _item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._updating:
            return
        selected_mod = self._mod_for_item(item)
        if selected_mod is None:
            return
        checked = {mod for mod in self._mods if self._is_checked(mod)}
        if item.checkState(0) != Qt.CheckState.Checked:
            dependents = self._required_by_selection(selected_mod, checked)
            if dependents:
                checked.add(selected_mod)
                self._set_checked_mods(checked)
                names = ", ".join(
                    dependent.display_name or dependent.folder_name
                    for dependent in dependents
                )
                self._refresh_status(
                    f"Cannot exclude {selected_mod.display_name or selected_mod.folder_name}; "
                    f"it is required by {names}."
                )
            else:
                self._refresh_status()
            return

        selected_before = set(checked)
        selected_before.discard(selected_mod)
        closure = self._requirement_closure(selected_mod, selected_before)
        desired = selected_before | closure
        deselected: set[DiscoveredMod] = set()
        for conflict in self._conflicts:
            first = conflict.declaring_mod
            second = conflict.incompatible_mod
            if first not in desired or second not in desired:
                continue
            if first in closure and second not in closure:
                candidate = second
            elif second in closure and first not in closure:
                candidate = first
            else:
                continue
            if self._required_by_selection(candidate, desired - {candidate}):
                continue
            desired.remove(candidate)
            deselected.add(candidate)
        self._set_checked_mods(desired)

        auto_selected = closure - {selected_mod} - selected_before
        actions = []
        if auto_selected:
            actions.append(
                "Selected required mod(s): "
                + ", ".join(
                    mod.display_name or mod.folder_name
                    for mod in sorted(auto_selected, key=lambda entry: entry.folder_name)
                )
            )
        if deselected:
            actions.append(
                "Deselected incompatible mod(s): "
                + ", ".join(
                    mod.display_name or mod.folder_name
                    for mod in sorted(deselected, key=lambda entry: entry.folder_name)
                )
            )
        self._refresh_status(". ".join(actions) + "." if actions else None)

    def _selected_requirement_problems(
        self,
    ) -> tuple[list[BundledModRequirement], list[BundledModRequirement]]:
        missing: list[BundledModRequirement] = []
        excluded: list[BundledModRequirement] = []
        for mod in self._mods:
            if not self._is_checked(mod):
                continue
            for requirement in self._requirements_for(mod):
                if requirement.is_missing:
                    missing.append(requirement)
                elif not any(self._is_checked(item) for item in requirement.providers):
                    excluded.append(requirement)
        return missing, excluded

    def _selected_conflicts(self) -> list[BundledModConflict]:
        return [
            conflict
            for conflict in self._conflicts
            if self._is_checked(conflict.declaring_mod)
            and self._is_checked(conflict.incompatible_mod)
        ]

    def _refresh_status(self, action: str | None = None) -> None:
        selected_count = sum(self._is_checked(mod) for mod in self._mods)
        conflicts = self._selected_conflicts()
        missing_requirements, excluded_requirements = (
            self._selected_requirement_problems()
        )
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setEnabled(
            selected_count > 0
            and not conflicts
            and not missing_requirements
            and not excluded_requirements
        )
        if not selected_count:
            self.status.setText("Select at least one bundled mod folder.")
        elif missing_requirements:
            labels = ", ".join(
                f"{item.required_mod_id} (needed by "
                f"{item.declaring_mod.display_name or item.declaring_mod.folder_name})"
                for item in missing_requirements[:4]
            )
            remainder = (
                f" (+{len(missing_requirements) - 4} more)"
                if len(missing_requirements) > 4
                else ""
            )
            self.status.setText(
                f"Missing required Mod ID(s): {labels}{remainder}. Add their Workshop "
                "items or source folders before building."
            )
        elif excluded_requirements:
            labels = ", ".join(
                f"{item.required_mod_id} (needed by "
                f"{item.declaring_mod.display_name or item.declaring_mod.folder_name})"
                for item in excluded_requirements[:4]
            )
            self.status.setText(f"Include the required bundled mod(s): {labels}.")
        elif conflicts:
            pairs = ", ".join(
                f"{item.declaring_mod_id} ↔ {item.incompatible_mod_id}"
                for item in conflicts[:4]
            )
            remainder = f" (+{len(conflicts) - 4} more)" if len(conflicts) > 4 else ""
            self.status.setText(
                f"Resolve {len(conflicts)} incompatible selection(s): {pairs}{remainder}"
            )
        elif action:
            self.status.setText(action)
        else:
            self.status.setText(
                f"{selected_count} of {len(self._mods)} bundled mod folders will be included."
            )

    def _show_details(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        mod = next(
            (candidate for candidate, item in self._items.items() if item is current),
            None,
        )
        if mod is None:
            self.details.clear()
            return
        lines = [
            f"Name: {mod.display_name or mod.folder_name}",
            f"Folder: {mod.folder_name}",
            f"Mod ID(s): {'; '.join(mod.mod_ids)}",
            f"Active Mod ID: {self._active_mod_id(mod)}",
            f"Workshop item: {mod.workshop_id or 'local source'}",
            f"Requires: {self._requirement_summary(mod)}",
            f"Conflicts with: {self._conflict_summary(mod)}",
        ]
        requirements = self._requirements_for(mod)
        if requirements:
            lines.extend(("", self._requirement_details(mod)))
        if mod.description:
            lines.extend(("", mod.description))
        self.details.setPlainText("\n".join(lines))

    def selected_mod_ids(self) -> tuple[str, ...]:
        return tuple(
            mod_id
            for mod in self._mods
            if self._is_checked(mod)
            for mod_id in mod.mod_ids
        )


class ModpackWindow(QMainWindow):
    def __init__(
        self,
        run_async: bool = True,
        session_file: Path | None = None,
        persist_session: bool = True,
    ) -> None:
        super().__init__()
        self.run_async = run_async
        self.persist_session = persist_session
        self._workers: set[QThread] = set()
        self.setWindowTitle(f"PZ Modpack Builder v{__version__}")
        self.resize(1040, 780)
        workspace = Path.home() / ".pzmodpack-builder"
        self.session_file = (
            Path(session_file).resolve()
            if session_file is not None
            else workspace / "steam-session.json"
        )
        saved_session = (
            load_steam_session(self.session_file)
            if self.persist_session
            else None
        )
        executable_name = "steamcmd.exe" if sys.platform.startswith("win") else "steamcmd.sh"

        self.name_edit = QLineEdit("My Project Zomboid Modpack")
        self.namespace_edit = QLineEdit("MyPack")
        self.workshop_edit = QLineEdit("0")
        self.output_edit = QLineEdit(str(Path.cwd() / "build" / "modpack"))
        self.description_edit = QLineEdit("A curated Project Zomboid multiplayer modpack")
        self.preview_edit = QLineEdit()
        self.preview_edit.setPlaceholderText("Optional preview.png for Workshop upload")
        self.visibility_combo = QComboBox()
        self.visibility_combo.addItem("Public", 0)
        self.visibility_combo.addItem("Friends only", 1)
        self.visibility_combo.addItem("Private", 2)
        self.visibility_combo.addItem("Unlisted", 3)
        self.visibility_combo.setCurrentIndex(2)
        self.version_bump_combo = QComboBox()
        self.version_bump_combo.addItem("Patch (1.0.0 -> 1.0.1)", "patch")
        self.version_bump_combo.addItem("Minor (1.0.0 -> 1.1.0)", "minor")
        self.version_bump_combo.addItem("Major (1.0.0 -> 2.0.0)", "major")
        self.version_status_label = QLabel()
        self.version_status_label.setWordWrap(True)
        self.output_edit.textChanged.connect(self._update_version_summary)
        self.version_bump_combo.currentIndexChanged.connect(
            self._update_version_summary
        )
        self._update_version_summary()
        self.active_ids_edit = QPlainTextEdit()
        self.active_ids_edit.setMaximumHeight(70)
        self.active_ids_edit.setPlaceholderText(
            "Optional active Mod ID overrides, one per line: FolderName=ModId"
        )
        self.included_mod_ids: tuple[str, ...] | None = None
        self.source_list = QListWidget()
        self.source_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        default_steamcmd = workspace / "tools" / "steamcmd" / executable_name
        default_library = workspace / "steam-library"
        self.steamcmd_edit = QLineEdit(
            str(saved_session.steamcmd if saved_session else default_steamcmd)
        )
        self.library_edit = QLineEdit(
            str(saved_session.steam_library if saved_session else default_library)
        )
        self.snapshot_edit = QLineEdit(str(workspace / "snapshots"))
        self.anonymous_check = QCheckBox("Use anonymous Steam login")
        self.anonymous_check.setChecked(saved_session is None)
        self.username_edit = QLineEdit(saved_session.username if saved_session else "")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.guard_edit = QLineEdit()
        self.guard_edit.setPlaceholderText("Optional Steam Guard code")
        self.workshop_input = QPlainTextEdit()
        self.workshop_input.setPlaceholderText(
            "Paste Workshop URLs or IDs, one per line\n"
            "https://steamcommunity.com/sharedfiles/filedetails/?id=2921417999"
        )
        self.upload_change_edit = QPlainTextEdit()
        self.upload_change_edit.setMaximumHeight(100)
        self.upload_change_edit.setPlaceholderText(
            "Describe this release or update for the Workshop change notes"
        )
        self.anonymous_check.toggled.connect(self._update_login_fields)
        self.username_edit.textChanged.connect(lambda _text: self._update_login_status())
        self._update_login_fields(self.anonymous_check.isChecked())

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Scan, SteamCMD, and build results appear here.")

        tabs = QTabWidget()
        tabs.addTab(self._build_tab(), "Build pack")
        tabs.addTab(self._steam_tab(), "Workshop downloads")
        tabs.addTab(self._upload_tab(), "Workshop upload")
        self._update_login_fields(self.anonymous_check.isChecked())

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(tabs)
        splitter.addWidget(self.log)
        splitter.setSizes([550, 190])

        container = QWidget()
        container_layout = QVBoxLayout(container)
        title = QLabel("Project Zomboid Modpack Builder")
        title.setObjectName("title")
        subtitle = QLabel(
            "Download controlled Workshop snapshots, namespace Mod IDs, validate dependencies, "
            "and generate Workshop and AMP configuration files."
        )
        subtitle.setWordWrap(True)
        container_layout.addWidget(title)
        container_layout.addWidget(subtitle)
        container_layout.addWidget(splitter, 1)
        self.setCentralWidget(container)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #171a21; color: #d6d7d8; }
            QLineEdit, QListWidget, QPlainTextEdit, QComboBox, QTabWidget::pane {
                background: #202530; border: 1px solid #3a4353; border-radius: 5px;
                padding: 6px; color: #f2f3f5; selection-background-color: #3b82f6;
            }
            QTabBar::tab { background: #252b37; padding: 8px 16px; margin-right: 2px; }
            QTabBar::tab:selected { background: #354158; }
            QPushButton {
                background: #30394a; border: 1px solid #46526a; border-radius: 5px;
                padding: 7px 12px; color: #f2f3f5;
            }
            QPushButton:hover { background: #3a465b; }
            QPushButton:disabled {
                background: #242936; border-color: #343b4b; color: #737b8c;
            }
            QPushButton#primaryButton {
                background: #2563eb; border-color: #3b82f6; font-weight: 600; padding: 10px;
            }
            QPushButton#primaryButton:disabled {
                background: #27334a; border-color: #34425d; color: #77839a;
            }
            QLabel#title { font-size: 23px; font-weight: 700; color: #ffffff; }
            QLabel#sectionTitle { font-size: 17px; font-weight: 650; color: #ffffff; }
            QLabel#uploadDestination {
                background: #202b3d; border: 1px solid #3b82f6; border-radius: 5px;
                padding: 10px; color: #dbeafe; font-weight: 600;
            }
            """
        )

    def _build_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        form.addRow("Pack name", self.name_edit)
        form.addRow("Mod ID namespace", self.namespace_edit)
        form.addRow("Workshop ID", self.workshop_edit)
        form.addRow("Description", self.description_edit)
        preview_row = QHBoxLayout()
        preview_row.addWidget(self.preview_edit, 1)
        browse_preview = QPushButton("Browse...")
        browse_preview.clicked.connect(self.choose_preview)
        preview_row.addWidget(browse_preview)
        form.addRow("Workshop preview", preview_row)
        form.addRow("Workshop visibility", self.visibility_combo)
        form.addRow("Version bump on rebuild", self.version_bump_combo)
        form.addRow("Version status", self.version_status_label)
        form.addRow("Active ID overrides", self.active_ids_edit)
        bundled_mod_row = QHBoxLayout()
        self.mod_selection_label = QLabel("All discovered bundled mods")
        bundled_mod_row.addWidget(self.mod_selection_label, 1)
        select_bundled_mods = QPushButton("Select bundled mods...")
        select_bundled_mods.clicked.connect(self.select_bundled_mods)
        bundled_mod_row.addWidget(select_bundled_mods)
        form.addRow("Bundled mod selection", bundled_mod_row)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        browse_output = QPushButton("Browse...")
        browse_output.clicked.connect(self.choose_output)
        output_row.addWidget(browse_output)
        form.addRow("Output directory", output_row)
        layout.addLayout(form)
        project_buttons = QHBoxLayout()
        open_project = QPushButton("Open project...")
        open_project.clicked.connect(self.open_project_dialog)
        save_project_button = QPushButton("Save project...")
        save_project_button.clicked.connect(self.save_project_dialog)
        project_buttons.addWidget(open_project)
        project_buttons.addWidget(save_project_button)
        project_buttons.addStretch(1)
        layout.addLayout(project_buttons)
        layout.addWidget(QLabel("Workshop snapshots or local mod source folders"))
        layout.addWidget(self.source_list, 1)
        source_buttons = QHBoxLayout()
        add_source = QPushButton("Add source folder")
        add_source.clicked.connect(self.add_source_dialog)
        remove_source = QPushButton("Remove selected")
        remove_source.clicked.connect(self.remove_selected_sources)
        scan = QPushButton("Scan")
        scan.clicked.connect(self.scan_sources)
        source_buttons.addWidget(add_source)
        source_buttons.addWidget(remove_source)
        source_buttons.addStretch(1)
        source_buttons.addWidget(scan)
        layout.addLayout(source_buttons)
        self.build_button = QPushButton("Build modpack")
        self.build_button.setObjectName("primaryButton")
        self.build_button.clicked.connect(self.build_pack)
        layout.addWidget(self.build_button)
        self.build_status = QLabel("Ready to build")
        self.build_status.setObjectName("buildStatus")
        self.build_status.hide()
        layout.addWidget(self.build_status)
        self.build_progress = QProgressBar()
        self.build_progress.setRange(0, 100)
        self.build_progress.setValue(0)
        self.build_progress.setFormat("%p%")
        self.build_progress.hide()
        layout.addWidget(self.build_progress)
        return panel

    def _steam_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        paths = QFormLayout()
        steamcmd_row = QHBoxLayout()
        steamcmd_row.addWidget(self.steamcmd_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self.choose_steamcmd)
        install = QPushButton("Install SteamCMD")
        install.clicked.connect(self.install_managed_steamcmd)
        steamcmd_row.addWidget(browse)
        steamcmd_row.addWidget(install)
        paths.addRow("SteamCMD executable", steamcmd_row)
        paths.addRow("Steam library/cache", self.library_edit)
        paths.addRow("Immutable snapshots", self.snapshot_edit)
        layout.addLayout(paths)

        layout.addWidget(self.anonymous_check)
        self.login_status_label = QLabel()
        self.login_status_label.setObjectName("loginStatus")
        self._update_login_status()
        layout.addWidget(self.login_status_label)
        login_form = QFormLayout()
        login_form.addRow("Steam username", self.username_edit)
        login_form.addRow("Steam password", self.password_edit)
        login_form.addRow("Steam Guard code", self.guard_edit)
        layout.addLayout(login_form)
        login_buttons = QHBoxLayout()
        test_login = QPushButton("Test Steam login")
        test_login.clicked.connect(self.test_steam_login)
        forget_login = QPushButton("Forget saved account")
        forget_login.clicked.connect(self.forget_saved_steam_account)
        login_buttons.addWidget(test_login)
        login_buttons.addWidget(forget_login)
        login_buttons.addStretch(1)
        layout.addLayout(login_buttons)

        layout.addWidget(QLabel("Project Zomboid Workshop URLs or IDs"))
        layout.addWidget(self.workshop_input, 1)
        self.download_button = QPushButton("Download, snapshot, and add to pack")
        self.download_button.setObjectName("primaryButton")
        self.download_button.clicked.connect(self.download_workshop_items)
        layout.addWidget(self.download_button)
        self.download_status = QLabel("Ready to download")
        self.download_status.setObjectName("downloadStatus")
        self.download_status.hide()
        layout.addWidget(self.download_status)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setFormat("%p%")
        self.download_progress.hide()
        layout.addWidget(self.download_progress)
        note = QLabel(
            "Passwords and Steam Guard codes use a restricted temporary SteamCMD runscript, "
            "are redacted from output, and are deleted immediately after the operation."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return panel

    def _upload_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        heading = QLabel("Upload the generated pack with SteamCMD")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        explanation = QLabel(
            "Build the pack first. SteamCMD will upload the generated Contents folder "
            "and preview.png. Workshop ID 0 creates a new item; an existing ID updates it."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.upload_destination_label = QLabel()
        self.upload_destination_label.setWordWrap(True)
        self.upload_destination_label.setObjectName("uploadDestination")
        layout.addWidget(self.upload_destination_label)
        self.workshop_edit.textChanged.connect(self._update_upload_summary)
        self.output_edit.textChanged.connect(self._update_upload_summary)
        self.visibility_combo.currentIndexChanged.connect(self._update_upload_summary)
        self._update_upload_summary()
        account_note = QLabel(
            "Uploading requires an account login. Test the account on the Workshop downloads "
            "tab first, or enter the password and Guard code there immediately before upload."
        )
        account_note.setWordWrap(True)
        layout.addWidget(account_note)
        layout.addWidget(QLabel("Workshop change note"))
        layout.addWidget(self.upload_change_edit)
        self.upload_permission_check = QCheckBox(
            "I confirm that I have permission to redistribute every bundled mod"
        )
        self.upload_permission_check.toggled.connect(
            lambda _checked: self._update_upload_button()
        )
        layout.addWidget(self.upload_permission_check)
        self.upload_button = QPushButton("Upload built modpack to Steam Workshop")
        self.upload_button.setObjectName("primaryButton")
        self.upload_button.clicked.connect(self.upload_built_modpack)
        layout.addWidget(self.upload_button)
        warning = QLabel(
            "Uploading can create or replace a live Workshop item. Review manifest.json, "
            "the destination Workshop ID, visibility, preview, and change note first."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        layout.addStretch(1)
        return panel

    def active_mod_id_overrides(self) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for line_number, raw_line in enumerate(self.active_ids_edit.toPlainText().splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            folder, separator, mod_id = line.partition("=")
            if not separator or not folder.strip() or not mod_id.strip():
                raise ValueError(
                    f"Invalid active Mod ID override on line {line_number}; use FolderName=ModId"
                )
            overrides[folder.strip()] = mod_id.strip()
        return overrides

    def _update_version_summary(self, *_args: object) -> None:
        output_text = self.output_edit.text().strip()
        bump = str(self.version_bump_combo.currentData() or "patch")
        if not output_text:
            self.version_status_label.setText("Choose an output directory.")
            return
        output = Path(output_text).expanduser()
        manifest_path = output / "manifest.json"
        try:
            history = output.with_name(f"{output.name}.versions")
        except ValueError:
            self.version_status_label.setText("Choose a named output directory.")
            return
        archived_count = (
            len([item for item in history.glob("v*") if item.is_dir()])
            if history.is_dir()
            else 0
        )
        if not manifest_path.is_file():
            if history.exists():
                self.version_status_label.setText(
                    "Current output is missing but version history exists; restore the "
                    "latest archived version before rebuilding."
                )
            else:
                self.version_status_label.setText("First build will be v1.0.0.")
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.version_status_label.setText("Existing build manifest is unreadable.")
            return
        if not isinstance(manifest, dict):
            self.version_status_label.setText("Existing build manifest is invalid.")
            return
        pack_version = str(manifest.get("pack_version") or "legacy/untracked")
        archive_label = (
            f"{archived_count} archived version(s)"
            if archived_count
            else "no archived versions yet"
        )
        self.version_status_label.setText(
            f"Current: v{pack_version}; next rebuild uses a {bump} bump; "
            f"{archive_label}."
        )

    def project_settings(self) -> ProjectSettings:
        workshop_items = parse_workshop_ids(self.workshop_input.toPlainText().splitlines())
        return ProjectSettings(
            name=self.name_edit.text().strip(),
            namespace=self.namespace_edit.text().strip(),
            workshop_id=self.workshop_edit.text().strip() or "0",
            description=self.description_edit.text().strip(),
            output=Path(self.output_edit.text()).expanduser(),
            sources=self.source_paths(),
            steamcmd=Path(self.steamcmd_edit.text()).expanduser(),
            steam_library=Path(self.library_edit.text()).expanduser(),
            snapshot_root=Path(self.snapshot_edit.text()).expanduser(),
            workshop_items=workshop_items,
            preview=(
                Path(self.preview_edit.text()).expanduser()
                if self.preview_edit.text().strip()
                else None
            ),
            visibility=int(self.visibility_combo.currentData()),
            active_mod_ids=self.active_mod_id_overrides(),
            included_mod_ids=self.included_mod_ids,
            version_bump=str(self.version_bump_combo.currentData()),
        )

    def save_project_to(self, path: Path) -> None:
        save_project(path, self.project_settings())
        self.log.appendPlainText(f"Project saved to {Path(path).resolve()}")

    def load_project_from(self, path: Path) -> None:
        settings = load_project(path)
        self.name_edit.setText(settings.name)
        self.namespace_edit.setText(settings.namespace)
        self.workshop_edit.setText(settings.workshop_id)
        self.description_edit.setText(settings.description)
        self.output_edit.setText(str(settings.output))
        self.steamcmd_edit.setText(str(settings.steamcmd))
        self.library_edit.setText(str(settings.steam_library))
        self.snapshot_edit.setText(str(settings.snapshot_root))
        self.preview_edit.setText(str(settings.preview) if settings.preview else "")
        visibility_index = self.visibility_combo.findData(settings.visibility)
        self.visibility_combo.setCurrentIndex(visibility_index if visibility_index >= 0 else 2)
        self.active_ids_edit.setPlainText(
            "\n".join(
                f"{folder}={mod_id}"
                for folder, mod_id in sorted(settings.active_mod_ids.items())
            )
        )
        self.source_list.clear()
        for source in settings.sources:
            self.add_source_path(source)
        self.included_mod_ids = settings.included_mod_ids
        version_bump_index = self.version_bump_combo.findData(settings.version_bump)
        self.version_bump_combo.setCurrentIndex(
            version_bump_index if version_bump_index >= 0 else 0
        )
        self._update_mod_selection_label()
        self.workshop_input.setPlainText("\n".join(settings.workshop_items))
        self._clear_transient_secrets()
        self.log.appendPlainText(f"Project loaded from {Path(path).resolve()}")

    def save_project_dialog(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Save modpack project",
            "modpack.pzpack.json",
            "PZ Modpack Project (*.pzpack.json);;JSON (*.json)",
        )
        if not selected:
            return
        try:
            self.save_project_to(Path(selected))
        except (OSError, ValueError) as error:
            self.log.appendPlainText(f"Project save failed: {error}")

    def open_project_dialog(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open modpack project",
            "",
            "PZ Modpack Project (*.pzpack.json);;JSON (*.json)",
        )
        if not selected:
            return
        try:
            self.load_project_from(Path(selected))
        except (OSError, ValueError, KeyError) as error:
            self.log.appendPlainText(f"Project load failed: {error}")

    def source_paths(self) -> tuple[Path, ...]:
        return tuple(Path(self.source_list.item(index).text()) for index in range(self.source_list.count()))

    def add_source_path(self, path: Path) -> None:
        resolved = str(Path(path).resolve())
        existing = {self.source_list.item(index).text() for index in range(self.source_list.count())}
        if resolved not in existing:
            self.source_list.addItem(resolved)
            self.included_mod_ids = None
            self._update_mod_selection_label()

    def add_source_dialog(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select Workshop item or mod source")
        if selected:
            self.add_source_path(Path(selected))

    def remove_selected_sources(self) -> None:
        removed = False
        for item in self.source_list.selectedItems():
            self.source_list.takeItem(self.source_list.row(item))
            removed = True
        if removed:
            self.included_mod_ids = None
            self._update_mod_selection_label()

    def choose_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select output parent directory")
        if selected:
            self.output_edit.setText(str(Path(selected) / "modpack"))

    def choose_preview(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select Workshop preview image",
            "",
            "PNG images (*.png);;Images (*.png *.jpg *.jpeg)",
        )
        if selected:
            self.preview_edit.setText(selected)

    def choose_steamcmd(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select SteamCMD executable")
        if selected:
            self.steamcmd_edit.setText(selected)

    def _update_upload_summary(self) -> None:
        if not hasattr(self, "upload_destination_label"):
            return
        workshop_id = self.workshop_edit.text().strip() or "0"
        action = (
            "Create new Workshop item"
            if workshop_id == "0"
            else f"Update Workshop item {workshop_id}"
        )
        visibility = self.visibility_combo.currentText()
        output = str(Path(self.output_edit.text()).expanduser())
        self.upload_destination_label.setText(
            f"{action}  |  Visibility: {visibility}  |  Build: {output}"
        )

    def _update_login_status(self, message: str | None = None) -> None:
        if not hasattr(self, "login_status_label"):
            return
        if message is not None:
            self.login_status_label.setText(message)
            return
        if self.anonymous_check.isChecked():
            self.login_status_label.setText("Using anonymous SteamCMD login")
            return
        username = self.username_edit.text().strip()
        if self.persist_session and self.session_file.is_file() and username:
            self.login_status_label.setText(f"Cached SteamCMD account: {username}")
        elif username:
            self.login_status_label.setText(f"Steam account selected: {username} (not verified yet)")
        else:
            self.login_status_label.setText("Enter a Steam account to log in")

    def _save_current_steam_session(self) -> None:
        if not self.persist_session:
            return
        username = self.username_edit.text().strip()
        if not username:
            return
        save_steam_session(
            self.session_file,
            SavedSteamSession(
                username=username,
                steamcmd=Path(self.steamcmd_edit.text()).expanduser().resolve(),
                steam_library=Path(self.library_edit.text()).expanduser().resolve(),
            ),
        )

    def forget_saved_steam_account(self) -> None:
        if self.persist_session:
            clear_steam_session(self.session_file)
        self.username_edit.clear()
        self._clear_transient_secrets()
        self.anonymous_check.setChecked(True)
        self._update_login_status("Saved SteamCMD account was forgotten")

    def _update_login_fields(self, anonymous: bool) -> None:
        for field in (self.username_edit, self.password_edit, self.guard_edit):
            field.setEnabled(not anonymous)
        self._update_login_status()
        self._update_upload_button()

    def _update_upload_button(self) -> None:
        if not hasattr(self, "upload_button"):
            return
        confirmed = (
            hasattr(self, "upload_permission_check")
            and self.upload_permission_check.isChecked()
        )
        self.upload_button.setEnabled(
            not self.anonymous_check.isChecked() and confirmed
        )

    def steam_credentials(self) -> SteamCredentials:
        if self.anonymous_check.isChecked():
            return SteamCredentials.anonymous()
        return SteamCredentials(
            username=self.username_edit.text().strip(),
            password=self.password_edit.text(),
            guard_code=self.guard_edit.text().strip() or None,
        )

    def steam_client(self) -> SteamCmdClient:
        return SteamCmdClient(
            Path(self.steamcmd_edit.text()).expanduser(),
            Path(self.library_edit.text()).expanduser(),
        )

    def _clear_transient_secrets(self) -> None:
        self.password_edit.clear()
        self.guard_edit.clear()

    def _execute(
        self,
        operation: Callable[[], object],
        on_success: Callable[[object], None],
        failure_prefix: str,
    ) -> None:
        if not self.run_async:
            try:
                on_success(operation())
            except Exception as error:  # noqa: BLE001 - synchronous test boundary mirrors worker.
                self.log.appendPlainText(f"{failure_prefix}: {error}")
            return
        worker = OperationWorker(operation)
        self._workers.add(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(lambda message: self.log.appendPlainText(f"{failure_prefix}: {message}"))
        worker.finished.connect(lambda: self._workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def install_managed_steamcmd(self) -> None:
        self.log.clear()
        destination = Path(self.steamcmd_edit.text()).expanduser().parent

        def installed(executable: object) -> None:
            path = Path(str(executable))
            self.steamcmd_edit.setText(str(path))
            self.log.appendPlainText(f"SteamCMD installed at {path}")

        self.log.appendPlainText("Installing SteamCMD from Valve...")
        self._execute(
            lambda: install_steamcmd(destination),
            installed,
            "SteamCMD installation failed",
        )

    def test_steam_login(self) -> None:
        self.log.clear()
        credentials = self.steam_credentials()
        client = self.steam_client()
        self._clear_transient_secrets()

        def completed(result: object) -> None:
            steam_result = result
            self.log.appendPlainText(steam_result.output.strip())
            if steam_result.success:
                self._save_current_steam_session()
                self._update_login_status(
                    f"Cached SteamCMD account: {self.username_edit.text().strip()}"
                )
                self.log.appendPlainText(
                    "Steam login succeeded. SteamCMD will reuse its cached account session."
                )
            else:
                self._update_login_status("SteamCMD login failed; account was not saved")
                self.log.appendPlainText(
                    f"Steam login failed with exit code {steam_result.return_code}."
                )

        self.log.appendPlainText(
            f"PZ Modpack Builder v{__version__} Steam login check"
        )
        self.log.appendPlainText(
            "Testing Steam login... SteamCMD may update itself on first launch. "
            "This check stops after 90 seconds."
        )
        self._execute(
            lambda: client.test_login(credentials, timeout=90),
            completed,
            "Steam login failed",
        )

    def download_workshop_items(self) -> None:
        self.log.clear()
        credentials = self.steam_credentials()
        client = self.steam_client()
        snapshot_root = Path(self.snapshot_edit.text()).expanduser()
        try:
            workshop_ids = parse_workshop_ids(self.workshop_input.toPlainText().splitlines())
            if not workshop_ids:
                raise ValueError("Add at least one Workshop URL or ID")
        except ValueError as error:
            self._clear_transient_secrets()
            self.log.appendPlainText(f"Workshop download failed: {error}")
            return
        self._clear_transient_secrets()

        def completed(result: object) -> None:
            self._set_download_running(False)
            batch = result
            self.log.appendPlainText(batch.command_result.output.strip())
            if not batch.command_result.success:
                self.log.appendPlainText("SteamCMD did not complete the download successfully.")
                return
            for snapshot in batch.snapshots:
                self.add_source_path(snapshot.path)
                self.log.appendPlainText(
                    f"Locked Workshop {snapshot.workshop_id} as "
                    f"{snapshot.sha256[:16]} at {snapshot.path}"
                )

        def failed(message: str) -> None:
            self._set_download_running(False)
            self.log.appendPlainText(f"Workshop download failed: {message}")

        self.log.appendPlainText(
            f"Downloading {len(workshop_ids)} Workshop item(s) through SteamCMD..."
        )
        self._set_download_running(True)
        if not self.run_async:
            try:
                result = download_and_snapshot(
                    client,
                    workshop_ids,
                    credentials,
                    snapshot_root,
                    progress=self._update_download_progress,
                )
            except Exception as error:  # noqa: BLE001 - synchronous path mirrors worker.
                failed(str(error))
                return
            completed(result)
            return

        worker = WorkshopDownloadWorker(
            client,
            workshop_ids,
            credentials,
            snapshot_root,
        )
        self._workers.add(worker)
        worker.progress.connect(self._update_download_progress)
        worker.succeeded.connect(completed)
        worker.failed.connect(failed)
        worker.finished.connect(lambda: self._workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _set_download_running(self, running: bool) -> None:
        self.download_button.setEnabled(not running)
        self.download_progress.setVisible(running)
        self.download_status.setVisible(running)
        if running:
            self.download_progress.setRange(0, 100)
            self.download_progress.setValue(0)
            self.download_status.setText("Starting Workshop download...")

    def _update_download_progress(
        self,
        current: int,
        total: int,
        message: str,
    ) -> None:
        self.download_progress.setRange(0, max(total, 1))
        self.download_progress.setValue(current)
        self.download_status.setText(message)

    def upload_built_modpack(self) -> None:
        self.log.clear()
        if self.anonymous_check.isChecked():
            self.log.appendPlainText(
                "Workshop upload failed: switch to a Steam account login first."
            )
            return
        if not self.upload_permission_check.isChecked():
            self.log.appendPlainText(
                "Workshop upload failed: confirm redistribution permission first."
            )
            return
        if not self.username_edit.text().strip():
            self.log.appendPlainText("Workshop upload failed: enter the Steam username.")
            return
        change_note = self.upload_change_edit.toPlainText().strip()
        if not change_note:
            self.log.appendPlainText("Workshop upload failed: add a Workshop change note.")
            return
        credentials = self.steam_credentials()
        client = self.steam_client()
        output = Path(self.output_edit.text()).expanduser().resolve()
        self._clear_transient_secrets()

        def completed(result: object) -> None:
            upload_result = result
            self.log.appendPlainText(upload_result.command_result.output.strip())
            if not upload_result.command_result.success:
                self.log.appendPlainText("SteamCMD did not complete the upload successfully.")
                return
            if upload_result.published_file_id != "0":
                self.workshop_edit.setText(upload_result.published_file_id)
            self.log.appendPlainText(
                f"Workshop upload succeeded. Published file ID: "
                f"{upload_result.published_file_id}"
            )
            self.upload_permission_check.setChecked(False)

        self.log.appendPlainText(f"Uploading generated pack from {output}...")
        self._execute(
            lambda: upload_modpack(
                client,
                output,
                credentials,
                change_note,
            ),
            completed,
            "Workshop upload failed",
        )

    def _update_mod_selection_label(self) -> None:
        if not hasattr(self, "mod_selection_label"):
            return
        if self.included_mod_ids is None:
            self.mod_selection_label.setText("All discovered bundled mods")
        else:
            self.mod_selection_label.setText(
                f"Custom selection ({len(self.included_mod_ids)} Mod ID(s))"
            )

    def _show_mod_selection_dialog(
        self,
        mods: list[DiscoveredMod],
        active_mod_ids: dict[str, str],
    ) -> bool:
        dialog = BundledModSelectionDialog(
            mods,
            self.included_mod_ids,
            self,
            active_mod_ids=active_mod_ids,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.included_mod_ids = dialog.selected_mod_ids()
        self._update_mod_selection_label()
        selected_count = len(select_mods(mods, self.included_mod_ids))
        self.log.appendPlainText(
            f"Selected {selected_count} of {len(mods)} bundled mod folder(s)."
        )
        return True

    def select_bundled_mods(self) -> None:
        self.log.clear()
        if not self.source_paths():
            self.log.appendPlainText("Selection failed: add at least one source folder.")
            return
        try:
            mods = discover_mods(self.source_paths())
            active_mod_ids = self.active_mod_id_overrides()
        except (OSError, ValueError) as error:
            self.log.appendPlainText(f"Selection failed: {error}")
            return
        if not mods:
            self.log.appendPlainText("Selection failed: no bundled mods were discovered.")
            return
        if not self._show_mod_selection_dialog(mods, active_mod_ids):
            self.log.appendPlainText("Bundled mod selection was cancelled.")

    def scan_sources(self) -> None:
        self.log.clear()
        try:
            mods = discover_mods(self.source_paths())
            issues = validate_mods(mods, self.active_mod_id_overrides())
        except (OSError, ValueError) as error:
            self.log.appendPlainText(f"Scan failed: {error}")
            return
        self.log.appendPlainText(f"Discovered {len(mods)} mod folder(s).")
        for mod in mods:
            self.log.appendPlainText(f"  {mod.folder_name}: {';'.join(mod.mod_ids)}")
        if not self.active_ids_edit.toPlainText().strip():
            suggestions = [
                f"{mod.folder_name}={mod.mod_ids[-1]}"
                for mod in mods
                if len(mod.mod_ids) > 1
            ]
            if suggestions:
                self.active_ids_edit.setPlainText("\n".join(suggestions))
                self.log.appendPlainText(
                    "Added active-ID defaults for versioned mod folders; review before building."
                )
        for issue in issues:
            self.log.appendPlainText(f"{issue.severity.upper()}: {issue.message}")
        if any(issue.code == "bundled_incompatibility" for issue in issues):
            self.log.appendPlainText(
                "Use 'Select bundled mods...' to choose compatible alternatives."
            )
        if any(issue.code == "missing_required_mod" for issue in issues):
            self.log.appendPlainText(
                "Add the missing required Workshop items or source folders before "
                "building."
            )

    def _set_build_running(self, running: bool) -> None:
        self.build_button.setEnabled(not running)
        self.build_progress.setVisible(running)
        self.build_status.setVisible(running)
        if running:
            self.build_progress.setRange(0, 100)
            self.build_progress.setValue(0)
            self.build_status.setText("Starting build...")

    def _update_build_progress(self, current: int, total: int, message: str) -> None:
        self.build_progress.setRange(0, max(total, 1))
        self.build_progress.setValue(current)
        self.build_status.setText(message)

    def _display_build_report(self, report: BuildReport) -> None:
        self.log.appendPlainText(
            f"Built {report.mod_count} mod(s) as v{report.pack_version} at "
            f"{report.output}"
        )
        if report.archived_output is not None:
            self.log.appendPlainText(
                f"Archived v{report.previous_pack_version} at {report.archived_output}"
            )
        self.log.appendPlainText(f"Namespaced {len(report.mapping)} Mod ID(s).")
        self.log.appendPlainText(
            "Generated Workshop change notes and loaded them into the upload tab."
        )
        if report.warnings:
            actionable_categories = {"metadata", "runtime_mod_lookup", "mod_file_access"}
            actionable = [
                detail
                for detail in report.warning_details
                if detail.get("category") in actionable_categories
            ]
            ambiguous_count = len(report.warning_details) - len(actionable)
            if actionable:
                self.log.appendPlainText("Review these actionable Mod ID warnings:")
                for detail in actionable:
                    self.log.appendPlainText(
                        f"  WARNING [{detail['category']}]: {detail['message']}"
                    )
            if ambiguous_count:
                self.log.appendPlainText(
                    f"  {ambiguous_count} ambiguous/content reference warning(s) are "
                    "recorded in manifest.json."
                )
        else:
            self.log.appendPlainText("No unresolved original Mod ID references were found.")

    def _build_succeeded(self, result: object) -> None:
        self._set_build_running(False)
        self.upload_change_edit.setPlainText(result.change_note)
        self._update_version_summary()
        self._display_build_report(result)

    def _build_failed(self, message: str) -> None:
        self._set_build_running(False)
        self.log.appendPlainText(f"Build failed: {message}")

    def build_pack(self) -> None:
        self.log.clear()
        if not self.source_paths():
            self.log.appendPlainText("Build failed: add at least one source folder.")
            return
        try:
            discovered_mods = discover_mods(self.source_paths())
            active_mod_ids = self.active_mod_id_overrides()
            selected_mods = select_mods(discovered_mods, self.included_mod_ids)
            selected_conflicts = find_bundled_conflicts(selected_mods)
            selection_issues = validate_mod_selection(
                discovered_mods,
                selected_mods,
                active_mod_ids,
            )
            requirement_issues = [
                issue
                for issue in selection_issues
                if issue.code in {"excluded_required_mod", "missing_required_mod"}
            ]
            if not selected_mods or selected_conflicts or requirement_issues:
                if selected_conflicts:
                    self.log.appendPlainText(
                        f"Choose bundled variants to resolve "
                        f"{len(selected_conflicts)} incompatibility conflict(s)."
                    )
                if requirement_issues:
                    self.log.appendPlainText(
                        f"Review {len(requirement_issues)} required-mod issue(s) before "
                        "building."
                    )
                if not self._show_mod_selection_dialog(
                    discovered_mods,
                    active_mod_ids,
                ):
                    self.log.appendPlainText("Build cancelled before changing the output.")
                    return
            config = BuildConfig(
                name=self.name_edit.text().strip(),
                namespace=self.namespace_edit.text().strip(),
                sources=self.source_paths(),
                output=Path(self.output_edit.text()).expanduser(),
                workshop_id=self.workshop_edit.text().strip() or "0",
                description=self.description_edit.text().strip(),
                preview=(
                    Path(self.preview_edit.text()).expanduser()
                    if self.preview_edit.text().strip()
                    else None
                ),
                visibility=int(self.visibility_combo.currentData()),
                active_mod_ids=active_mod_ids,
                included_mod_ids=self.included_mod_ids,
                version_bump=str(self.version_bump_combo.currentData()),
            )
        except (OSError, ValueError) as error:
            self.log.appendPlainText(f"Build failed: {error}")
            return

        self._set_build_running(True)
        if not self.run_async:
            try:
                report = build_modpack(config, progress=self._update_build_progress)
            except (BuildError, OSError, ValueError) as error:
                self._build_failed(str(error))
                return
            self._build_succeeded(report)
            return

        worker = BuildWorker(config)
        self._workers.add(worker)
        worker.progress.connect(self._update_build_progress)
        worker.succeeded.connect(self._build_succeeded)
        worker.failed.connect(self._build_failed)
        worker.finished.connect(lambda: self._workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()


def main() -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    window = ModpackWindow()
    window.show()
    return application.exec()
