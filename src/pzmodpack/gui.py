from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
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
    QMessageBox,
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
    WorkshopSnapshotRevision,
    build_modpack,
    discover_mods,
    find_bundled_conflicts,
    find_mod_requirements,
    resolve_workshop_snapshots,
    select_mods,
    validate_mods,
    validate_mod_selection,
    workshop_snapshot_groups,
)
from .project import ProjectSettings, load_project, save_project
from .session import (
    SavedSteamSession,
    clear_steam_session,
    load_steam_session,
    save_steam_session,
)
from .steamcmd import (
    StoredWorkshopSnapshot,
    SteamCmdClient,
    SteamCredentials,
    delete_all_stored_workshop_snapshots,
    delete_stored_workshop_snapshot,
    download_and_snapshot,
    install_steamcmd,
    list_stored_workshop_snapshots,
    parse_workshop_ids,
    upload_modpack,
)


_MANAGED_KIND_ROLE = int(Qt.ItemDataRole.UserRole)
_MANAGED_WORKSHOP_ID_ROLE = _MANAGED_KIND_ROLE + 1
_MANAGED_REVISION_ROLE = _MANAGED_KIND_ROLE + 2


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


def _display_snapshot_time(value: str | None) -> str:
    if not value:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%Y-%m-%d %I:%M %p %Z")


def _snapshot_time_value(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _stored_snapshot_sort_key(
    record: StoredWorkshopSnapshot,
) -> tuple[float, float, str]:
    captured = _snapshot_time_value(record.snapshot_created_at_utc)
    updated = _snapshot_time_value(record.workshop_updated_at_utc)
    return (
        updated if updated != float("-inf") else captured,
        captured,
        record.revision_directory,
    )


class WorkshopSnapshotSelectionDialog(QDialog):
    def __init__(
        self,
        mods: list[DiscoveredMod],
        selections: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Workshop snapshot versions")
        self.resize(1120, 520)
        self._groups = workshop_snapshot_groups(mods)
        self._combos: dict[str, QComboBox] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        requested = selections or {}

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Each Workshop item is one bundled source. Choose exactly one immutable "
            "snapshot version for it. The newest downloaded Workshop revision is "
            "selected by default; older snapshots remain available for rollback."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            (
                "Workshop item",
                "Snapshot version",
                "Workshop updated",
                "Snapshot captured",
                "Status",
            )
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        for workshop_id, revisions in self._groups.items():
            item = QTreeWidgetItem(self.tree)
            names = sorted(
                {
                    mod.display_name or mod.folder_name
                    for revision in revisions
                    for mod in revision.mods
                },
                key=str.casefold,
            )
            name_summary = ", ".join(names[:2])
            if len(names) > 2:
                name_summary += f" (+{len(names) - 2} more)"
            item.setText(0, f"{workshop_id} — {name_summary}")
            combo = QComboBox()
            for index, revision in enumerate(revisions):
                combo.addItem(
                    self._revision_label(revision, is_latest=index == 0),
                    revision.revision_key,
                )
            selected_index = combo.findData(requested.get(workshop_id))
            combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
            combo.currentIndexChanged.connect(
                lambda _index, item_id=workshop_id: self._update_row(item_id)
            )
            self.tree.setItemWidget(item, 1, combo)
            self._combos[workshop_id] = combo
            self._items[workshop_id] = item
            self._update_row(workshop_id)

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        note = QLabel(
            "Workshop update dates come from Steam's local Workshop manifest when "
            "available. Legacy snapshots show Unknown rather than guessing from a "
            "filesystem timestamp; their displayed capture time may be inferred from "
            "the legacy snapshot metadata file."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @staticmethod
    def _revision_label(
        revision: WorkshopSnapshotRevision,
        *,
        is_latest: bool,
    ) -> str:
        status = "LATEST" if is_latest else "Older"
        short_hash = (revision.sha256 or revision.revision_key)[0:12]
        return f"{status} — {short_hash}"

    def _selected_revision(self, workshop_id: str) -> WorkshopSnapshotRevision:
        combo = self._combos[workshop_id]
        selected_key = str(combo.currentData())
        return next(
            revision
            for revision in self._groups[workshop_id]
            if revision.revision_key == selected_key
        )

    def _update_row(self, workshop_id: str) -> None:
        if workshop_id not in self._combos:
            return
        revision = self._selected_revision(workshop_id)
        item = self._items[workshop_id]
        latest = revision is self._groups[workshop_id][0]
        item.setText(2, _display_snapshot_time(revision.workshop_updated_at_utc))
        item.setText(3, _display_snapshot_time(revision.snapshot_created_at_utc))
        item.setText(4, "Latest downloaded" if latest else "Pinned older snapshot")
        details = (
            f"Source: {revision.source_root}\n"
            f"Full SHA-256: {revision.sha256 or 'Unavailable'}\n"
            f"Workshop manifest: {revision.workshop_manifest_id or 'Unavailable'}\n"
            f"Workshop updated: "
            f"{_display_snapshot_time(revision.workshop_updated_at_utc)}\n"
            f"Snapshot captured: "
            f"{_display_snapshot_time(revision.snapshot_created_at_utc)}"
        )
        for column in range(self.tree.columnCount()):
            item.setToolTip(column, details)

    def selected_revisions(self) -> dict[str, str]:
        return {
            workshop_id: str(combo.currentData())
            for workshop_id, combo in self._combos.items()
        }


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
            f"Snapshot SHA-256: {mod.snapshot_sha256 or 'local/unavailable'}",
            f"Workshop manifest: {mod.workshop_manifest_id or 'Unavailable'}",
            "Workshop updated: "
            f"{_display_snapshot_time(mod.workshop_updated_at_utc)}",
            "Snapshot captured: "
            f"{_display_snapshot_time(mod.snapshot_created_at_utc)}",
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
        self._build_running = False
        self._download_running = False
        self._download_in_manager = False
        self._manager_delete_running = False
        self._generic_operation_running = False
        self._mod_selection_needs_review = False
        self._managed_records: dict[tuple[str, str], StoredWorkshopSnapshot] = {}
        self._manager_restore_selection: tuple[str, str | None] | None = None
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
        self.snapshot_selections: dict[str, str] = {}
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

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tab(), "Build pack")
        self.tabs.addTab(self._steam_tab(), "Workshop downloads")
        self.manager_tab_index = self.tabs.addTab(
            self._manage_downloads_tab(),
            "Manage downloads",
        )
        self.tabs.addTab(self._upload_tab(), "Workshop upload")
        self.tabs.currentChanged.connect(self._tab_changed)
        self._update_login_fields(self.anonymous_check.isChecked())
        self._update_operation_controls()

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tabs)
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
            QLineEdit, QListWidget, QPlainTextEdit, QComboBox, QTreeWidget,
            QTabWidget::pane {
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
        self.browse_preview_button = QPushButton("Browse...")
        self.browse_preview_button.clicked.connect(self.choose_preview)
        preview_row.addWidget(self.browse_preview_button)
        form.addRow("Workshop preview", preview_row)
        form.addRow("Workshop visibility", self.visibility_combo)
        form.addRow("Version bump on rebuild", self.version_bump_combo)
        form.addRow("Version status", self.version_status_label)
        form.addRow("Active ID overrides", self.active_ids_edit)
        bundled_mod_row = QHBoxLayout()
        self.mod_selection_label = QLabel("All discovered bundled mods")
        bundled_mod_row.addWidget(self.mod_selection_label, 1)
        self.select_bundled_mods_button = QPushButton(
            "Select snapshots and bundled mods..."
        )
        self.select_bundled_mods_button.clicked.connect(self.select_bundled_mods)
        bundled_mod_row.addWidget(self.select_bundled_mods_button)
        form.addRow("Snapshot and mod selection", bundled_mod_row)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        self.browse_output_button = QPushButton("Browse...")
        self.browse_output_button.clicked.connect(self.choose_output)
        output_row.addWidget(self.browse_output_button)
        form.addRow("Output directory", output_row)
        layout.addLayout(form)
        project_buttons = QHBoxLayout()
        self.open_project_button = QPushButton("Open project...")
        self.open_project_button.clicked.connect(self.open_project_dialog)
        self.save_project_button = QPushButton("Save project...")
        self.save_project_button.clicked.connect(self.save_project_dialog)
        project_buttons.addWidget(self.open_project_button)
        project_buttons.addWidget(self.save_project_button)
        project_buttons.addStretch(1)
        layout.addLayout(project_buttons)
        layout.addWidget(QLabel("Workshop snapshots or local mod source folders"))
        layout.addWidget(self.source_list, 1)
        source_buttons = QHBoxLayout()
        self.add_source_button = QPushButton("Add source folder")
        self.add_source_button.clicked.connect(self.add_source_dialog)
        self.remove_source_button = QPushButton("Remove selected")
        self.remove_source_button.clicked.connect(self.remove_selected_sources)
        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.scan_sources)
        source_buttons.addWidget(self.add_source_button)
        source_buttons.addWidget(self.remove_source_button)
        source_buttons.addStretch(1)
        source_buttons.addWidget(self.scan_button)
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
        self.browse_steamcmd_button = QPushButton("Browse...")
        self.browse_steamcmd_button.clicked.connect(self.choose_steamcmd)
        self.install_steamcmd_button = QPushButton("Install SteamCMD")
        self.install_steamcmd_button.clicked.connect(self.install_managed_steamcmd)
        steamcmd_row.addWidget(self.browse_steamcmd_button)
        steamcmd_row.addWidget(self.install_steamcmd_button)
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
        self.test_login_button = QPushButton("Test Steam login")
        self.test_login_button.clicked.connect(self.test_steam_login)
        self.forget_login_button = QPushButton("Forget saved account")
        self.forget_login_button.clicked.connect(self.forget_saved_steam_account)
        login_buttons.addWidget(self.test_login_button)
        login_buttons.addWidget(self.forget_login_button)
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

    def _manage_downloads_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        heading = QLabel("Manage immutable Workshop downloads")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        explanation = QLabel(
            "Browse every Workshop item in the configured snapshot library, update one "
            "item through SteamCMD, attach a stored revision to this pack, or explicitly "
            "delete snapshots you no longer need. Updates never replace older revisions."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.manager_root_label = QLabel()
        self.manager_root_label.setWordWrap(True)
        layout.addWidget(self.manager_root_label)
        self.manager_summary_label = QLabel("Snapshot library has not been scanned yet.")
        self.manager_summary_label.setWordWrap(True)
        layout.addWidget(self.manager_summary_label)

        self.manager_tree = QTreeWidget()
        self.manager_tree.setColumnCount(6)
        self.manager_tree.setHeaderLabels(
            (
                "Workshop item / snapshot",
                "Bundled folders",
                "Workshop updated",
                "Snapshot captured",
                "Manifest",
                "Status",
            )
        )
        manager_header = self.manager_tree.header()
        manager_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        manager_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 5):
            manager_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        manager_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.manager_tree.itemSelectionChanged.connect(
            self._managed_selection_changed
        )
        layout.addWidget(self.manager_tree, 1)

        buttons = QHBoxLayout()
        self.manager_refresh_button = QPushButton("Refresh")
        self.manager_refresh_button.clicked.connect(self.refresh_managed_downloads)
        self.manager_update_button = QPushButton("Update selected item")
        self.manager_update_button.clicked.connect(
            self.update_managed_workshop_item
        )
        self.manager_add_button = QPushButton("Use snapshot in current pack")
        self.manager_add_button.clicked.connect(
            self.add_managed_snapshot_to_pack
        )
        self.manager_delete_snapshot_button = QPushButton("Delete snapshot...")
        self.manager_delete_snapshot_button.clicked.connect(
            self.delete_managed_snapshot
        )
        self.manager_delete_item_button = QPushButton("Delete all for item...")
        self.manager_delete_item_button.clicked.connect(
            self.delete_managed_workshop_item
        )
        buttons.addWidget(self.manager_refresh_button)
        buttons.addWidget(self.manager_update_button)
        buttons.addWidget(self.manager_add_button)
        buttons.addStretch(1)
        buttons.addWidget(self.manager_delete_snapshot_button)
        buttons.addWidget(self.manager_delete_item_button)
        layout.addLayout(buttons)

        self.manager_details = QPlainTextEdit()
        self.manager_details.setReadOnly(True)
        self.manager_details.setMaximumHeight(120)
        self.manager_details.setPlaceholderText(
            "Select a Workshop item or snapshot to see its stored provenance."
        )
        layout.addWidget(self.manager_details)
        self.manager_operation_status = QLabel("Ready")
        self.manager_operation_status.hide()
        layout.addWidget(self.manager_operation_status)
        self.manager_progress = QProgressBar()
        self.manager_progress.setRange(0, 100)
        self.manager_progress.setValue(0)
        self.manager_progress.setFormat("%p%")
        self.manager_progress.hide()
        layout.addWidget(self.manager_progress)

        warning = QLabel(
            "Deleting removes only the builder's immutable snapshot copies. SteamCMD's "
            "mutable cache is left intact. Other saved project files may still refer to "
            "a deleted path, so deletion always requires confirmation."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.snapshot_edit.editingFinished.connect(self.refresh_managed_downloads)
        self.manager_root_label.setText(
            f"Snapshot library: {Path(self.snapshot_edit.text()).expanduser()}"
        )
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
            snapshot_selections=self.snapshot_selections,
        )

    def save_project_to(self, path: Path) -> None:
        save_project(path, self.project_settings())
        self.log.appendPlainText(f"Project saved to {Path(path).resolve()}")

    def load_project_from(self, path: Path) -> None:
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "Project loading is unavailable while another operation is running."
            )
            return
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
        self._mod_selection_needs_review = False
        self.snapshot_selections = dict(settings.snapshot_selections)
        version_bump_index = self.version_bump_combo.findData(settings.version_bump)
        self.version_bump_combo.setCurrentIndex(
            version_bump_index if version_bump_index >= 0 else 0
        )
        self._update_mod_selection_label()
        self.workshop_input.setPlainText("\n".join(settings.workshop_items))
        self._clear_transient_secrets()
        self.refresh_managed_downloads()
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

    def add_source_path(
        self,
        path: Path,
        *,
        preserve_mod_selection: bool = False,
    ) -> None:
        resolved = str(Path(path).resolve())
        existing = {self.source_list.item(index).text() for index in range(self.source_list.count())}
        if resolved not in existing:
            self.source_list.addItem(resolved)
            if not preserve_mod_selection:
                self.included_mod_ids = None
                self._mod_selection_needs_review = False
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
            self._mod_selection_needs_review = False
            try:
                groups = workshop_snapshot_groups(
                    discover_mods(self.source_paths())
                )
            except OSError:
                groups = {}
            self.snapshot_selections = {
                workshop_id: revision
                for workshop_id, revision in self.snapshot_selections.items()
                if workshop_id in groups
                and any(
                    candidate.revision_key == revision
                    for candidate in groups[workshop_id]
                )
            }
            self._update_mod_selection_label()

    def _tab_changed(self, index: int) -> None:
        if (
            index == self.manager_tab_index
            and not self._build_running
            and not self._download_running
            and not self._manager_delete_running
            and not self._generic_operation_running
        ):
            self.refresh_managed_downloads()

    @staticmethod
    def _source_belongs_to_snapshot(source: Path, snapshot: Path) -> bool:
        try:
            Path(source).resolve().relative_to(Path(snapshot).resolve())
        except (OSError, ValueError):
            return False
        return True

    def _snapshot_is_attached(self, record: StoredWorkshopSnapshot) -> bool:
        return any(
            self._source_belongs_to_snapshot(source, record.path)
            for source in self.source_paths()
        )

    def _steam_workshop_cache_root(self) -> Path:
        return (
            Path(self.library_edit.text()).expanduser()
            / "steamapps"
            / "workshop"
            / "content"
            / "108600"
        )

    def _validate_snapshot_root_separation(
        self,
        snapshot_root: Path | None = None,
    ) -> None:
        snapshot = (snapshot_root or Path(self.snapshot_edit.text()).expanduser()).resolve(
            strict=False
        )
        cache = self._steam_workshop_cache_root().resolve(strict=False)
        if (
            snapshot == cache
            or snapshot.is_relative_to(cache)
            or cache.is_relative_to(snapshot)
        ):
            raise ValueError(
                "the immutable snapshot library must be separate from SteamCMD's "
                f"mutable Workshop cache ({cache})"
            )

    def _manager_selection_identity(self) -> tuple[str, str | None] | None:
        item = self.manager_tree.currentItem()
        if item is None:
            return None
        workshop_id = str(item.data(0, _MANAGED_WORKSHOP_ID_ROLE) or "")
        if not workshop_id:
            return None
        revision = item.data(0, _MANAGED_REVISION_ROLE)
        return workshop_id, str(revision) if revision else None

    def _selected_managed_record(self) -> StoredWorkshopSnapshot | None:
        identity = self._manager_selection_identity()
        if identity is None or identity[1] is None:
            return None
        return self._managed_records.get((identity[0], identity[1]))

    def _managed_records_for_item(
        self,
        workshop_id: str,
    ) -> tuple[StoredWorkshopSnapshot, ...]:
        return tuple(
            record
            for (candidate_id, _revision), record in self._managed_records.items()
            if candidate_id == workshop_id
        )

    def refresh_managed_downloads(self) -> None:
        if not hasattr(self, "manager_tree"):
            return
        if (
            self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            return
        previous_selection = (
            self._manager_restore_selection or self._manager_selection_identity()
        )
        self._manager_restore_selection = None
        snapshot_root = Path(self.snapshot_edit.text()).expanduser()
        self.manager_root_label.setText(f"Snapshot library: {snapshot_root}")
        self.manager_tree.clear()
        self._managed_records = {}
        try:
            self._validate_snapshot_root_separation(snapshot_root)
        except (OSError, ValueError) as error:
            self.manager_summary_label.setText(f"Unsafe snapshot library setting: {error}")
            self.manager_details.clear()
            self._update_manager_action_buttons()
            return
        try:
            inventory = list_stored_workshop_snapshots(snapshot_root)
        except (OSError, ValueError) as error:
            self.manager_summary_label.setText(
                f"Could not read the snapshot library: {error}"
            )
            self.manager_details.clear()
            self._update_manager_action_buttons()
            return
        inventory_warnings = list(inventory.warnings)

        grouped: dict[str, list[StoredWorkshopSnapshot]] = {}
        for record in inventory.snapshots:
            grouped.setdefault(record.workshop_id, []).append(record)
            self._managed_records[
                (record.workshop_id, record.revision_directory)
            ] = record

        cache_root = self._steam_workshop_cache_root()
        cache_items: dict[str, Path] = {}
        if cache_root.is_dir():
            try:
                cache_items = {
                    path.name: path
                    for path in cache_root.iterdir()
                    if path.is_dir() and path.name.isdigit()
                }
            except OSError as error:
                inventory_warnings.append(
                    f"Could not inspect SteamCMD cache: {error}"
                )
        for workshop_id in cache_items:
            grouped.setdefault(workshop_id, [])

        selected_item: QTreeWidgetItem | None = None
        for workshop_id in sorted(grouped, key=int):
            records = grouped[workshop_id]
            latest = records[0] if records else None
            cached_path = cache_items.get(workshop_id)
            cached_mods_root = cached_path / "mods" if cached_path else None
            cached_folders: tuple[str, ...] = ()
            if cached_mods_root is not None and cached_mods_root.is_dir():
                try:
                    cached_folders = tuple(
                        sorted(
                            path.name
                            for path in cached_mods_root.iterdir()
                            if path.is_dir()
                        )
                    )
                except OSError:
                    cached_folders = ()
            mod_summary = ", ".join(
                latest.mod_folders if latest is not None else cached_folders
            ) or "No mod folders detected"
            attached_count = sum(self._snapshot_is_attached(record) for record in records)
            cached = cached_path is not None
            parent_status = [f"{len(records)} snapshot(s)"]
            if attached_count:
                parent_status.append(f"{attached_count} in current pack")
            parent_status.append("SteamCMD cache present" if cached else "Snapshot only")
            parent = QTreeWidgetItem(
                (
                    f"Workshop {workshop_id}",
                    mod_summary,
                    _display_snapshot_time(
                        latest.workshop_updated_at_utc if latest else None
                    ),
                    _display_snapshot_time(
                        latest.snapshot_created_at_utc if latest else None
                    ),
                    latest.workshop_manifest_id
                    if latest and latest.workshop_manifest_id
                    else "Unknown",
                    "; ".join(parent_status),
                )
            )
            parent.setData(0, _MANAGED_KIND_ROLE, "item")
            parent.setData(0, _MANAGED_WORKSHOP_ID_ROLE, workshop_id)
            self.manager_tree.addTopLevelItem(parent)

            for index, record in enumerate(records):
                attached = self._snapshot_is_attached(record)
                selected_revision = self.snapshot_selections.get(workshop_id)
                selected = selected_revision in {
                    record.sha256,
                    f"path:{record.path.as_posix()}",
                }
                statuses: list[str] = []
                if index == 0:
                    statuses.append("Latest stored")
                if attached:
                    statuses.append("In current pack")
                if selected and attached:
                    statuses.append(
                        "Selected latest" if index == 0 else "Pinned older"
                    )
                elif selected:
                    statuses.append("Selected source missing")
                elif attached and index == 0 and selected_revision is None:
                    statuses.append("Build default")
                if record.metadata_state == "legacy":
                    statuses.append("Legacy metadata")
                elif record.metadata_state != "valid":
                    statuses.append("Metadata warning")
                child = QTreeWidgetItem(
                    (
                        record.sha256[:16]
                        if record.sha256
                        else record.revision_directory,
                        ", ".join(record.mod_folders) or "No mod folders detected",
                        _display_snapshot_time(record.workshop_updated_at_utc),
                        _display_snapshot_time(record.snapshot_created_at_utc),
                        record.workshop_manifest_id or "Unknown",
                        "; ".join(statuses) or "Stored",
                    )
                )
                child.setData(0, _MANAGED_KIND_ROLE, "snapshot")
                child.setData(0, _MANAGED_WORKSHOP_ID_ROLE, workshop_id)
                child.setData(
                    0,
                    _MANAGED_REVISION_ROLE,
                    record.revision_directory,
                )
                tooltip = (
                    f"Path: {record.path}\n"
                    f"Full SHA-256: {record.sha256 or 'Unavailable'}\n"
                    f"Metadata: {record.metadata_message or record.metadata_state}"
                )
                for column in range(6):
                    child.setToolTip(column, tooltip)
                parent.addChild(child)
                if previous_selection == (
                    workshop_id,
                    record.revision_directory,
                ):
                    selected_item = child
            if previous_selection == (workshop_id, None):
                selected_item = parent
            parent.setExpanded(True)

        snapshot_count = len(inventory.snapshots)
        summary = (
            f"{len(grouped)} Workshop item(s), {snapshot_count} immutable snapshot(s)."
        )
        if inventory_warnings:
            summary += (
                f" {len(inventory_warnings)} storage warning(s); hover for details."
            )
            self.manager_summary_label.setToolTip("\n".join(inventory_warnings))
        else:
            self.manager_summary_label.setToolTip("")
        if not snapshot_count:
            if grouped:
                summary = (
                    f"No immutable snapshots found; {len(grouped)} SteamCMD cache "
                    "item(s) are shown."
                )
            else:
                summary = f"No stored Workshop snapshots found in {snapshot_root}."
            if inventory_warnings:
                summary += f" {len(inventory_warnings)} storage warning(s)."
        self.manager_summary_label.setText(summary)
        if selected_item is not None:
            self.manager_tree.setCurrentItem(selected_item)
        else:
            self.manager_details.clear()
        self._managed_selection_changed()

    def _managed_selection_changed(self) -> None:
        identity = self._manager_selection_identity()
        if identity is None:
            self.manager_details.clear()
            self._update_manager_action_buttons()
            return
        workshop_id, revision = identity
        if revision is None:
            records = self._managed_records_for_item(workshop_id)
            attached = sum(self._snapshot_is_attached(record) for record in records)
            self.manager_details.setPlainText(
                f"Workshop item: {workshop_id}\n"
                f"Stored snapshots: {len(records)}\n"
                f"Attached to current pack: {attached}\n"
                "Update checks this item through SteamCMD and keeps every older "
                "immutable revision."
            )
        else:
            record = self._managed_records.get((workshop_id, revision))
            if record is None:
                self.manager_details.clear()
            else:
                self.manager_details.setPlainText(
                    f"Workshop item: {record.workshop_id}\n"
                    f"Snapshot path: {record.path}\n"
                    f"Full SHA-256: {record.sha256 or 'Unavailable'}\n"
                    f"Workshop manifest: {record.workshop_manifest_id or 'Unknown'}\n"
                    f"Workshop updated: "
                    f"{_display_snapshot_time(record.workshop_updated_at_utc)}\n"
                    f"Snapshot captured: "
                    f"{_display_snapshot_time(record.snapshot_created_at_utc)}\n"
                    f"Metadata: {record.metadata_message or record.metadata_state}"
                )
        self._update_manager_action_buttons()

    def _update_manager_action_buttons(self) -> None:
        if not hasattr(self, "manager_update_button"):
            return
        identity = self._manager_selection_identity()
        record = self._selected_managed_record()
        item_records = (
            self._managed_records_for_item(identity[0])
            if identity is not None
            else ()
        )
        busy = (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        )
        self.manager_update_button.setEnabled(identity is not None and not busy)
        self.manager_add_button.setEnabled(
            record is not None and record.is_valid and not busy
        )
        self.manager_delete_snapshot_button.setEnabled(
            record is not None and record.is_deletable and not busy
        )
        self.manager_delete_item_button.setEnabled(
            bool(item_records)
            and all(record.is_deletable for record in item_records)
            and not busy
        )
        self.manager_refresh_button.setEnabled(not busy)

    def _update_operation_controls(self) -> None:
        busy = (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        )
        if hasattr(self, "build_button"):
            self.build_button.setEnabled(not busy)
        if hasattr(self, "download_button"):
            self.download_button.setEnabled(not busy)
        for button_name in (
            "open_project_button",
            "save_project_button",
            "select_bundled_mods_button",
            "add_source_button",
            "remove_source_button",
            "scan_button",
            "browse_preview_button",
            "browse_output_button",
            "browse_steamcmd_button",
            "install_steamcmd_button",
            "test_login_button",
            "forget_login_button",
        ):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setEnabled(not busy)
        for editor_name in (
            "name_edit",
            "namespace_edit",
            "workshop_edit",
            "description_edit",
            "output_edit",
            "preview_edit",
            "visibility_combo",
            "version_bump_combo",
            "active_ids_edit",
            "source_list",
            "workshop_input",
            "upload_change_edit",
        ):
            editor = getattr(self, editor_name, None)
            if editor is not None:
                editor.setEnabled(not busy)
        for field_name in ("steamcmd_edit", "library_edit", "snapshot_edit"):
            field = getattr(self, field_name, None)
            if field is not None:
                field.setEnabled(not busy)
        credential_busy = self._download_running or self._generic_operation_running
        if hasattr(self, "anonymous_check"):
            self.anonymous_check.setEnabled(not credential_busy)
        if hasattr(self, "username_edit"):
            credential_fields_enabled = (
                not self.anonymous_check.isChecked() and not credential_busy
            )
            for field in (self.username_edit, self.password_edit, self.guard_edit):
                field.setEnabled(credential_fields_enabled)
        if hasattr(self, "upload_permission_check"):
            self.upload_permission_check.setEnabled(not busy)
        self._update_manager_action_buttons()
        self._update_upload_button()

    def _effective_project_revision(self, workshop_id: str) -> str | None:
        records = tuple(
            record
            for record in self._managed_records_for_item(workshop_id)
            if self._snapshot_is_attached(record)
        )
        if not records:
            return None
        requested = self.snapshot_selections.get(workshop_id)
        if requested is not None and any(
            requested
            in {
                record.sha256,
                f"path:{record.path.as_posix()}",
            }
            for record in records
        ):
            return requested
        first = records[0]
        return first.sha256 or f"path:{first.path.as_posix()}"

    def add_managed_snapshot_to_pack(self) -> None:
        record = self._selected_managed_record()
        if record is None or not record.is_valid or record.sha256 is None:
            self.log.appendPlainText(
                "Snapshot attach failed: select a snapshot with valid metadata."
            )
            return
        effective_before = self._effective_project_revision(record.workshop_id)
        already_attached = self._snapshot_is_attached(record)
        self.add_source_path(record.path, preserve_mod_selection=True)
        self.snapshot_selections[record.workshop_id] = record.sha256
        if (
            effective_before != record.sha256
            and self.included_mod_ids is not None
        ):
            self._mod_selection_needs_review = True
        self._update_mod_selection_label()
        self._manager_restore_selection = (
            record.workshop_id,
            record.revision_directory,
        )
        self.refresh_managed_downloads()
        action = "Selected" if already_attached else "Added"
        self.log.appendPlainText(
            f"{action} Workshop {record.workshop_id} snapshot "
            f"{record.sha256[:12]} for the current pack. Reopen bundled mod "
            "selection if this revision changes the available folders or Mod IDs."
        )

    def update_managed_workshop_item(self) -> None:
        identity = self._manager_selection_identity()
        if identity is None:
            self.log.appendPlainText(
                "Workshop update failed: select a Workshop item or snapshot."
            )
            return
        workshop_id = identity[0]
        attached_ids = {
            workshop_id
            for record in self._managed_records_for_item(workshop_id)
            if self._snapshot_is_attached(record)
        }
        self.log.clear()
        self.log.appendPlainText(
            f"Checking Workshop {workshop_id} for an updated snapshot..."
        )
        self._start_workshop_download(
            (workshop_id,),
            attach_workshop_ids=attached_ids,
            manager=True,
        )

    def _confirm_snapshot_deletion(
        self,
        title: str,
        message: str,
    ) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def delete_managed_snapshot(self) -> None:
        record = self._selected_managed_record()
        if record is None or not record.is_deletable:
            self.log.appendPlainText(
                "Snapshot deletion failed: select an individual hash-named snapshot."
            )
            return
        attached = self._snapshot_is_attached(record)
        selected = self.snapshot_selections.get(record.workshop_id) in {
            record.sha256,
            f"path:{record.path.as_posix()}",
        }
        usage = []
        if attached:
            usage.append("It is attached to the current pack.")
        if selected:
            usage.append("It is the selected revision for the current pack.")
        if not usage:
            usage.append("It is not currently attached to this pack.")
        effective_before = self._effective_project_revision(record.workshop_id)
        record_keys = {
            record.sha256,
            f"path:{record.path.as_posix()}",
        }
        fallback_note = ""
        if effective_before in record_keys:
            remaining = tuple(
                candidate
                for candidate in self._managed_records_for_item(record.workshop_id)
                if candidate.revision_directory != record.revision_directory
                and candidate.is_valid
            )
            if remaining and remaining[0].sha256:
                fallback_note = (
                    f" The current pack will switch to the latest remaining snapshot "
                    f"{remaining[0].sha256[:12]}."
                )
            else:
                fallback_note = (
                    " The Workshop item will be removed from the current pack because "
                    "no valid snapshot will remain."
                )
        elif selected and effective_before is not None:
            fallback_note = (
                " The stale selection will be cleared; the currently attached "
                f"revision {effective_before.removeprefix('path:')[:12]} will remain."
            )
        message = (
            f"Delete Workshop {record.workshop_id} snapshot\n\n"
            f"{record.sha256 or record.revision_directory}\n{record.path}\n\n"
            f"{' '.join(usage)}{fallback_note} The SteamCMD cache will not be deleted. Other saved "
            "project files may still reference this path. This cannot be undone."
        )
        if not self._confirm_snapshot_deletion("Delete immutable snapshot?", message):
            return
        snapshot_root = Path(self.snapshot_edit.text()).expanduser()
        self._start_managed_deletion(
            record.workshop_id,
            effective_before,
            lambda: delete_stored_workshop_snapshot(
                snapshot_root,
                record.workshop_id,
                record.revision_directory,
            ),
        )

    def delete_managed_workshop_item(self) -> None:
        identity = self._manager_selection_identity()
        if identity is None:
            self.log.appendPlainText(
                "Workshop deletion failed: select a Workshop item or snapshot."
            )
            return
        workshop_id = identity[0]
        records = self._managed_records_for_item(workshop_id)
        if not records or not all(record.is_deletable for record in records):
            self.log.appendPlainText(
                f"Workshop deletion failed: Workshop {workshop_id} contains an "
                "unrecognized storage entry and cannot be deleted as a group."
            )
            return
        attached_count = sum(self._snapshot_is_attached(record) for record in records)
        message = (
            f"Delete all {len(records)} immutable snapshot(s) for Workshop "
            f"{workshop_id}?\n\n{attached_count} snapshot(s) are attached to the "
            "current pack. If attached, this Workshop item will be removed from the "
            "current pack. The SteamCMD cache will not be deleted. Other saved project "
            "files may still reference these paths. This cannot be undone."
        )
        if not self._confirm_snapshot_deletion(
            "Delete all snapshots for this item?",
            message,
        ):
            return
        effective_before = self._effective_project_revision(workshop_id)
        snapshot_root = Path(self.snapshot_edit.text()).expanduser()
        self._start_managed_deletion(
            workshop_id,
            effective_before,
            lambda: delete_all_stored_workshop_snapshots(
                snapshot_root,
                workshop_id,
            ),
        )

    def _set_manager_delete_running(self, running: bool) -> None:
        self._manager_delete_running = running
        self.manager_progress.setVisible(running)
        self.manager_operation_status.setVisible(running)
        if running:
            self.manager_progress.setRange(0, 0)
            self.manager_operation_status.setText("Deleting immutable snapshot data...")
        else:
            self.manager_progress.setRange(0, 100)
            self.manager_progress.setValue(0)
        self._update_operation_controls()

    def _start_managed_deletion(
        self,
        workshop_id: str,
        effective_before: str | None,
        operation: Callable[[], object],
    ) -> None:
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "Snapshot deletion is unavailable while another operation is running."
            )
            return
        try:
            self._validate_snapshot_root_separation()
        except (OSError, ValueError) as error:
            self.log.appendPlainText(f"Snapshot deletion refused: {error}")
            return
        self.log.clear()
        self._set_manager_delete_running(True)

        def completed(result: object) -> None:
            self._set_manager_delete_running(False)
            deleted_paths = tuple(getattr(result, "deleted_paths", ()))
            self.log.appendPlainText(
                f"Deleted {len(deleted_paths)} immutable snapshot(s) for Workshop "
                f"{workshop_id}. SteamCMD's cache was left unchanged."
            )
            try:
                self._reconcile_project_after_snapshot_deletion(
                    workshop_id,
                    effective_before,
                )
                self.refresh_managed_downloads()
            except (OSError, ValueError) as error:
                self.log.appendPlainText(
                    "Snapshot files were deleted, but project reconciliation failed: "
                    f"{error}. Refresh the manager and review project sources before "
                    "building."
                )

        def failed(message: str) -> None:
            self._set_manager_delete_running(False)
            self.log.appendPlainText(f"Snapshot deletion failed: {message}")
            try:
                self._reconcile_project_after_snapshot_deletion(
                    workshop_id,
                    effective_before,
                )
                self.refresh_managed_downloads()
            except (OSError, ValueError) as error:
                self.log.appendPlainText(
                    f"Snapshot state refresh also failed: {error}. Review the "
                    "snapshot library before building."
                )

        self._execute(
            operation,
            completed,
            "Snapshot deletion failed",
            on_failure=failed,
        )

    def _reconcile_project_after_snapshot_deletion(
        self,
        workshop_id: str,
        effective_before: str | None,
    ) -> None:
        item_root = (
            Path(self.snapshot_edit.text()).expanduser().resolve()
            / workshop_id
        )
        removed_sources = 0
        for index in range(self.source_list.count() - 1, -1, -1):
            source = Path(self.source_list.item(index).text())
            try:
                belongs_to_item = source.resolve().is_relative_to(item_root)
            except OSError:
                belongs_to_item = False
            if belongs_to_item and not source.exists():
                self.source_list.takeItem(index)
                removed_sources += 1

        inventory = list_stored_workshop_snapshots(
            Path(self.snapshot_edit.text()).expanduser()
        )
        remaining = tuple(
            record
            for record in inventory.snapshots
            if record.workshop_id == workshop_id and record.is_valid
        )
        remaining_keys = {
            key
            for record in remaining
            for key in (
                record.sha256,
                f"path:{record.path.as_posix()}",
            )
            if key
        }
        current_selection = self.snapshot_selections.get(workshop_id)
        effective_removed = (
            effective_before is not None and effective_before not in remaining_keys
        )
        selection_removed = (
            current_selection is not None and current_selection not in remaining_keys
        )
        if effective_removed:
            if remaining and remaining[0].sha256:
                fallback = remaining[0]
                self.add_source_path(fallback.path, preserve_mod_selection=True)
                self.snapshot_selections[workshop_id] = fallback.sha256
                self._manager_restore_selection = (
                    workshop_id,
                    fallback.revision_directory,
                )
                self.log.appendPlainText(
                    f"Workshop {workshop_id} now uses latest remaining snapshot "
                    f"{fallback.sha256[:12]}."
                )
            else:
                self.snapshot_selections.pop(workshop_id, None)
            self._mod_selection_needs_review = self.included_mod_ids is not None
        elif selection_removed:
            self.snapshot_selections.pop(workshop_id, None)
            self.log.appendPlainText(
                f"Cleared the deleted stale selection for Workshop {workshop_id}; "
                "the attached snapshot did not change."
            )
        elif not remaining and removed_sources:
            self.snapshot_selections.pop(workshop_id, None)
            self._mod_selection_needs_review = self.included_mod_ids is not None
        if removed_sources:
            self.log.appendPlainText(
                f"Removed {removed_sources} deleted snapshot source path(s) from "
                "the current project."
            )
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
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "Steam account changes are unavailable while another operation is running."
            )
            return
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
            not self.anonymous_check.isChecked()
            and confirmed
            and not self._build_running
            and not self._download_running
            and not self._manager_delete_running
            and not self._generic_operation_running
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

    def _set_generic_operation_running(self, running: bool) -> None:
        self._generic_operation_running = running
        self._update_operation_controls()

    def _execute(
        self,
        operation: Callable[[], object],
        on_success: Callable[[object], None],
        failure_prefix: str,
        *,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        if not self.run_async:
            try:
                on_success(operation())
            except Exception as error:  # noqa: BLE001 - synchronous test boundary mirrors worker.
                if on_failure is not None:
                    on_failure(str(error))
                else:
                    self.log.appendPlainText(f"{failure_prefix}: {error}")
            return
        worker = OperationWorker(operation)
        self._workers.add(worker)
        worker.succeeded.connect(on_success)
        if on_failure is not None:
            worker.failed.connect(on_failure)
        else:
            worker.failed.connect(
                lambda message: self.log.appendPlainText(
                    f"{failure_prefix}: {message}"
                )
            )
        worker.finished.connect(lambda: self._workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name.
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
            or self._workers
        ):
            self.log.appendPlainText(
                "Wait for the active operation to finish before closing the application."
            )
            QMessageBox.warning(
                self,
                "Operation still running",
                "A build, SteamCMD action, or snapshot deletion is still running. "
                "Wait for it to finish before closing so files are not left incomplete.",
            )
            event.ignore()
            return
        super().closeEvent(event)

    def install_managed_steamcmd(self) -> None:
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "SteamCMD installation is unavailable while another operation is running."
            )
            return
        self.log.clear()
        destination = Path(self.steamcmd_edit.text()).expanduser().parent
        self._set_generic_operation_running(True)

        def installed(executable: object) -> None:
            self._set_generic_operation_running(False)
            path = Path(str(executable))
            self.steamcmd_edit.setText(str(path))
            self.log.appendPlainText(f"SteamCMD installed at {path}")

        def failed(message: str) -> None:
            self._set_generic_operation_running(False)
            self.log.appendPlainText(f"SteamCMD installation failed: {message}")

        self.log.appendPlainText("Installing SteamCMD from Valve...")
        self._execute(
            lambda: install_steamcmd(destination),
            installed,
            "SteamCMD installation failed",
            on_failure=failed,
        )

    def test_steam_login(self) -> None:
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "Steam login testing is unavailable while another operation is running."
            )
            return
        self.log.clear()
        credentials = self.steam_credentials()
        client = self.steam_client()
        username = self.username_edit.text().strip()
        self._clear_transient_secrets()
        self._set_generic_operation_running(True)

        def completed(result: object) -> None:
            self._set_generic_operation_running(False)
            steam_result = result
            self.log.appendPlainText(steam_result.output.strip())
            if steam_result.success:
                self._save_current_steam_session()
                self._update_login_status(
                    f"Cached SteamCMD account: {username}"
                )
                self.log.appendPlainText(
                    "Steam login succeeded. SteamCMD will reuse its cached account session."
                )
            else:
                self._update_login_status("SteamCMD login failed; account was not saved")
                self.log.appendPlainText(
                    f"Steam login failed with exit code {steam_result.return_code}."
                )

        def failed(message: str) -> None:
            self._set_generic_operation_running(False)
            self._update_login_status("SteamCMD login failed; account was not saved")
            self.log.appendPlainText(f"Steam login failed: {message}")

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
            on_failure=failed,
        )

    def download_workshop_items(self) -> None:
        self.log.clear()
        try:
            workshop_ids = parse_workshop_ids(self.workshop_input.toPlainText().splitlines())
            if not workshop_ids:
                raise ValueError("Add at least one Workshop URL or ID")
        except ValueError as error:
            self._clear_transient_secrets()
            self.log.appendPlainText(f"Workshop download failed: {error}")
            return
        self.log.appendPlainText(
            f"Downloading {len(workshop_ids)} Workshop item(s) through SteamCMD..."
        )
        self._start_workshop_download(
            workshop_ids,
            attach_workshop_ids=set(workshop_ids),
            manager=False,
        )

    def _start_workshop_download(
        self,
        workshop_ids: tuple[str, ...],
        *,
        attach_workshop_ids: set[str],
        manager: bool,
    ) -> None:
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self._clear_transient_secrets()
            self.log.appendPlainText(
                "Workshop download is unavailable while another operation is running."
            )
            return
        credentials = self.steam_credentials()
        client = self.steam_client()
        snapshot_root = Path(self.snapshot_edit.text()).expanduser()
        try:
            self._validate_snapshot_root_separation(snapshot_root)
        except (OSError, ValueError) as error:
            self._clear_transient_secrets()
            self.log.appendPlainText(f"Workshop download failed: {error}")
            return
        self._clear_transient_secrets()
        self._set_download_running(True, manager=manager)
        snapshot_roots = {snapshot_root.resolve()}
        for source in self.source_paths():
            for candidate in (source, *source.parents):
                if (
                    (candidate / "snapshot.json").is_file()
                    and candidate.parent.name.isdigit()
                ):
                    snapshot_roots.add(candidate.parent.parent.resolve())
                    break
        previous_records: dict[str, list[StoredWorkshopSnapshot]] = {}
        try:
            for root in sorted(snapshot_roots, key=str):
                inventory = list_stored_workshop_snapshots(root)
                for record in inventory.snapshots:
                    if record.is_valid and self._snapshot_is_attached(record):
                        previous_records.setdefault(record.workshop_id, []).append(
                            record
                        )
        except (OSError, ValueError) as error:
            self._set_download_running(False, manager=manager)
            self.log.appendPlainText(
                f"Workshop download failed while reading current snapshot state: {error}"
            )
            return
        for records in previous_records.values():
            records.sort(key=_stored_snapshot_sort_key, reverse=True)

        def completed(result: object) -> None:
            self._set_download_running(False, manager=manager)
            batch = result
            command_output = batch.command_result.output.strip()
            if command_output:
                self.log.appendPlainText(command_output)
            if not batch.command_result.success:
                self.log.appendPlainText("SteamCMD did not complete the download successfully.")
                self.refresh_managed_downloads()
                return
            for snapshot in batch.snapshots:
                previous_revisions = previous_records.get(snapshot.workshop_id, ())
                previous_latest = (
                    previous_revisions[0].sha256
                    or f"path:{previous_revisions[0].path.as_posix()}"
                    if previous_revisions
                    else None
                )
                previous_selection = self.snapshot_selections.get(
                    snapshot.workshop_id
                )
                followed_latest = (
                    previous_selection is None
                    or previous_selection == previous_latest
                )
                if snapshot.workshop_id in attach_workshop_ids:
                    self.add_source_path(
                        snapshot.path,
                        preserve_mod_selection=True,
                    )
                    if followed_latest:
                        if (
                            previous_latest != snapshot.sha256
                            and self.included_mod_ids is not None
                        ):
                            self._mod_selection_needs_review = True
                        self.snapshot_selections[
                            snapshot.workshop_id
                        ] = snapshot.sha256
                snapshot_state = (
                    "new immutable snapshot"
                    if snapshot.created
                    else "existing snapshot already current"
                )
                self.log.appendPlainText(
                    f"Locked Workshop {snapshot.workshop_id} as "
                    f"{snapshot.sha256[:16]} at {snapshot.path} "
                    f"({snapshot_state})\n"
                    f"  Workshop updated: "
                    f"{_display_snapshot_time(snapshot.workshop_updated_at_utc)}\n"
                    f"  Snapshot captured: "
                    f"{_display_snapshot_time(snapshot.snapshot_created_at_utc)}"
                )
                self._manager_restore_selection = (
                    snapshot.workshop_id,
                    snapshot.path.name,
                )
            self._update_mod_selection_label()
            self.refresh_managed_downloads()

        def failed(message: str) -> None:
            self._set_download_running(False, manager=manager)
            self.refresh_managed_downloads()
            self.log.appendPlainText(f"Workshop download failed: {message}")

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

    def _set_download_running(self, running: bool, *, manager: bool = False) -> None:
        self._download_running = running
        self._download_in_manager = manager if running else False
        self.download_progress.setVisible(running and not manager)
        self.download_status.setVisible(running and not manager)
        self.manager_progress.setVisible(running and manager)
        self.manager_operation_status.setVisible(running and manager)
        if running:
            progress = self.manager_progress if manager else self.download_progress
            status = (
                self.manager_operation_status if manager else self.download_status
            )
            progress.setRange(0, 100)
            progress.setValue(0)
            status.setText("Starting Workshop update..." if manager else "Starting Workshop download...")
        self._update_operation_controls()

    def _update_download_progress(
        self,
        current: int,
        total: int,
        message: str,
    ) -> None:
        progress = (
            self.manager_progress if self._download_in_manager else self.download_progress
        )
        status = (
            self.manager_operation_status
            if self._download_in_manager
            else self.download_status
        )
        progress.setRange(0, max(total, 1))
        progress.setValue(current)
        status.setText(message)

    def upload_built_modpack(self) -> None:
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "Workshop upload is unavailable while another operation is running."
            )
            return
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
        self._set_generic_operation_running(True)

        def completed(result: object) -> None:
            self._set_generic_operation_running(False)
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

        def failed(message: str) -> None:
            self._set_generic_operation_running(False)
            self.log.appendPlainText(f"Workshop upload failed: {message}")

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
            on_failure=failed,
        )

    def _update_mod_selection_label(self) -> None:
        if not hasattr(self, "mod_selection_label"):
            return
        if self.included_mod_ids is None:
            label = "All discovered bundled mods"
        else:
            label = f"Custom selection ({len(self.included_mod_ids)} Mod ID(s))"
        if self.snapshot_selections:
            label += (
                f"; {len(self.snapshot_selections)} Workshop snapshot revision(s) "
                "chosen"
            )
        if self._mod_selection_needs_review:
            label += "; bundled selection review required"
        self.mod_selection_label.setText(label)

    def _snapshot_selections_need_review(
        self,
        mods: list[DiscoveredMod],
    ) -> bool:
        groups = workshop_snapshot_groups(mods)
        if set(self.snapshot_selections) - set(groups):
            return True
        for workshop_id, revisions in groups.items():
            selected = self.snapshot_selections.get(workshop_id)
            if len(revisions) > 1 and selected is None:
                return True
            if selected is not None and not any(
                revision.revision_key == selected for revision in revisions
            ):
                return True
        return False

    def _show_snapshot_selection_dialog(
        self,
        mods: list[DiscoveredMod],
        *,
        force: bool,
    ) -> list[DiscoveredMod] | None:
        groups = workshop_snapshot_groups(mods)
        if not groups:
            if self.snapshot_selections:
                self.snapshot_selections = {}
                self._update_mod_selection_label()
            return mods
        if not force and not self._snapshot_selections_need_review(mods):
            selected, _effective = resolve_workshop_snapshots(
                mods,
                self.snapshot_selections,
            )
            return selected

        try:
            _previous_mods, previous_effective = resolve_workshop_snapshots(
                mods,
                self.snapshot_selections,
            )
        except BuildError:
            previous_effective = {}
        dialog = WorkshopSnapshotSelectionDialog(
            mods,
            self.snapshot_selections,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected_revisions = dialog.selected_revisions()
        selected_mods, effective = resolve_workshop_snapshots(
            mods,
            selected_revisions,
        )
        if effective != previous_effective:
            self._mod_selection_needs_review = self.included_mod_ids is not None
            self._update_mod_selection_label()
        self.snapshot_selections = selected_revisions
        self._update_mod_selection_label()
        for workshop_id, revisions in groups.items():
            selected = next(
                revision
                for revision in revisions
                if revision.revision_key == effective[workshop_id]
            )
            status = "latest" if selected is revisions[0] else "older pinned"
            self.log.appendPlainText(
                f"Workshop {workshop_id}: selected {status} snapshot "
                f"{selected.revision_key[:12]}."
            )
        return selected_mods

    def _show_bundled_mod_selection_dialog(
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
        self._mod_selection_needs_review = False
        self._update_mod_selection_label()
        selected_count = len(select_mods(mods, self.included_mod_ids))
        self.log.appendPlainText(
            f"Selected {selected_count} of {len(mods)} bundled mod folder(s)."
        )
        return True

    def _show_mod_selection_dialog(
        self,
        mods: list[DiscoveredMod],
        active_mod_ids: dict[str, str],
    ) -> bool:
        selected_snapshots = self._show_snapshot_selection_dialog(
            mods,
            force=True,
        )
        if selected_snapshots is None:
            return False
        return self._show_bundled_mod_selection_dialog(
            selected_snapshots,
            active_mod_ids,
        )

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
            all_mods = discover_mods(self.source_paths())
            mods, effective_snapshots = resolve_workshop_snapshots(
                all_mods,
                self.snapshot_selections,
            )
            issues = validate_mods(mods, self.active_mod_id_overrides())
        except (BuildError, OSError, ValueError) as error:
            self.log.appendPlainText(f"Scan failed: {error}")
            return
        self.log.appendPlainText(f"Discovered {len(mods)} mod folder(s).")
        for workshop_id, revisions in workshop_snapshot_groups(all_mods).items():
            selected = next(
                revision
                for revision in revisions
                if revision.revision_key == effective_snapshots[workshop_id]
            )
            latest = selected is revisions[0]
            self.log.appendPlainText(
                f"  Workshop {workshop_id}: using "
                f"{'latest' if latest else 'pinned older'} snapshot "
                f"{selected.revision_key[:12]} of {len(revisions)}; "
                f"Workshop updated "
                f"{_display_snapshot_time(selected.workshop_updated_at_utc)}; "
                f"captured {_display_snapshot_time(selected.snapshot_created_at_utc)}"
            )
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
                "Use 'Select snapshots and bundled mods...' to choose compatible "
                "alternatives."
            )
        if any(issue.code == "missing_required_mod" for issue in issues):
            self.log.appendPlainText(
                "Add the missing required Workshop items or source folders before "
                "building."
            )

    def _set_build_running(self, running: bool) -> None:
        self._build_running = running
        self.build_progress.setVisible(running)
        self.build_status.setVisible(running)
        if running:
            self.build_progress.setRange(0, 100)
            self.build_progress.setValue(0)
            self.build_status.setText("Starting build...")
        self._update_operation_controls()

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
        if (
            self._build_running
            or self._download_running
            or self._manager_delete_running
            or self._generic_operation_running
        ):
            self.log.appendPlainText(
                "Build failed: wait for the active operation to finish."
            )
            return
        if not self.source_paths():
            self.log.appendPlainText("Build failed: add at least one source folder.")
            return
        try:
            all_discovered_mods = discover_mods(self.source_paths())
            discovered_mods = self._show_snapshot_selection_dialog(
                all_discovered_mods,
                force=self._snapshot_selections_need_review(all_discovered_mods),
            )
            if discovered_mods is None:
                self.log.appendPlainText(
                    "Build cancelled before changing the output."
                )
                return
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
            if (
                not selected_mods
                or selected_conflicts
                or requirement_issues
                or self._mod_selection_needs_review
            ):
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
                if self._mod_selection_needs_review:
                    self.log.appendPlainText(
                        "The selected snapshot changed; review bundled folders and "
                        "required mods before building."
                    )
                if not self._show_bundled_mod_selection_dialog(
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
                snapshot_selections=self.snapshot_selections,
            )
        except (BuildError, OSError, ValueError) as error:
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
