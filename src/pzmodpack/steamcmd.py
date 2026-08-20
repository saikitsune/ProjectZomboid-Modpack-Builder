from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from .version import __version__

PZ_APP_ID = "108600"
STEAMCMD_WINDOWS_URL = "https://client-update.steamstatic.com/installer/steamcmd.zip"
STEAMCMD_LINUX_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
PROJECT_GITHUB_URL = "https://github.com/saikitsune/ProjectZomboid-Modpack-Builder"
WORKSHOP_ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
WORKSHOP_DESCRIPTION_MAX_BYTES = 8000
_SNAPSHOT_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class SteamCredentials:
    username: str | None = None
    password: str | None = None
    guard_code: str | None = None

    @classmethod
    def anonymous(cls) -> SteamCredentials:
        return cls()

    @property
    def is_anonymous(self) -> bool:
        return not self.username


def parse_workshop_ids(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            continue
        if value.isdigit():
            workshop_id = value
        else:
            parsed = urlparse(value)
            candidates = parse_qs(parsed.query).get("id", [])
            if not candidates or not candidates[0].isdigit():
                raise ValueError(f"Could not extract a Workshop ID from: {raw_value}")
            workshop_id = candidates[0]
        if workshop_id not in result:
            result.append(workshop_id)
    return tuple(result)


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Steam credentials cannot contain line breaks")
    return '"' + value.replace('"', '\\"') + '"'


def _login_command(credentials: SteamCredentials) -> str:
    if credentials.is_anonymous:
        return "login anonymous"
    username = _quote(credentials.username or "")
    if not credentials.password:
        return f"login {username}"
    login = f"login {username} {_quote(credentials.password)}"
    if credentials.guard_code:
        login += f" {_quote(credentials.guard_code)}"
    return login


def build_command_script(
    workshop_ids: tuple[str, ...],
    credentials: SteamCredentials,
    library_root: Path,
) -> str:
    lines = [
        "@ShutdownOnFailedCommand 1",
        "@NoPromptForPassword 1",
        f"force_install_dir {_quote(str(Path(library_root)))}",
        _login_command(credentials),
    ]
    for workshop_id in workshop_ids:
        if not re.fullmatch(r"\d+", workshop_id):
            raise ValueError(f"Invalid Workshop ID: {workshop_id}")
        lines.append(f"workshop_download_item {PZ_APP_ID} {workshop_id} validate")
    lines.append("quit")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class WorkshopUploadConfig:
    published_file_id: str
    content_folder: Path
    preview_file: Path
    visibility: int
    title: str
    description: str
    change_note: str


_OPENING_QUOTE_PRECEDERS = frozenset("([{<:=,;!?/\\—–-")


def _typographic_double_quotes(value: str) -> str:
    """Replace VDF-breaking ASCII quotes while preserving readable punctuation."""
    converted: list[str] = []
    for index, character in enumerate(value):
        if character != '"':
            converted.append(character)
            continue
        previous = value[index - 1] if index else ""
        is_opening = (
            not previous
            or previous.isspace()
            or previous in _OPENING_QUOTE_PRECEDERS
        )
        converted.append("“" if is_opening else "”")
    return "".join(converted)


def _vdf_escape(value: str) -> str:
    # SteamCMD's Workshop KeyValues reader does not enable escape-sequence
    # conversion. Keep real line breaks and backslashes verbatim, and replace
    # only ASCII quotes that would otherwise terminate the value.
    if "\0" in value:
        raise ValueError("Workshop upload text cannot contain NUL characters")
    if "\x7f" in value:
        raise ValueError("Workshop upload text cannot contain DEL characters")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return _typographic_double_quotes(normalized)


def write_upload_vdf(path: Path, config: WorkshopUploadConfig) -> Path:
    if not re.fullmatch(r"\d+", config.published_file_id):
        raise ValueError("Published Workshop ID must contain digits only; use 0 for a new item")
    if config.visibility not in {0, 1, 2, 3}:
        raise ValueError("Workshop visibility must be 0, 1, 2, or 3")
    content_folder = Path(config.content_folder).resolve()
    preview_file = Path(config.preview_file).resolve()
    for field, field_path in (
        ("content folder", content_folder),
        ("preview file", preview_file),
    ):
        if '"' in str(field_path):
            raise ValueError(
                f"Workshop upload {field} path cannot contain double quotes: "
                f"{field_path}"
            )
    if not content_folder.is_dir():
        raise FileNotFoundError(f"Workshop content folder not found: {content_folder}")
    if not preview_file.is_file():
        raise FileNotFoundError(f"Workshop preview file not found: {preview_file}")
    values = (
        ("appid", PZ_APP_ID),
        ("publishedfileid", config.published_file_id),
        ("contentfolder", str(content_folder)),
        ("previewfile", str(preview_file)),
        ("visibility", str(config.visibility)),
        ("title", config.title),
        ("description", config.description),
        ("changenote", config.change_note),
    )
    lines = ['"workshopitem"', "{"]
    lines.extend(
        f'\t"{key}"\t\t"{_vdf_escape(value)}"'
        for key, value in values
    )
    lines.extend(("}", ""))
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def build_upload_script(
    vdf_path: Path,
    credentials: SteamCredentials,
    library_root: Path,
) -> str:
    if credentials.is_anonymous:
        raise ValueError("Uploading Workshop items requires a Steam account login")
    lines = [
        "@ShutdownOnFailedCommand 1",
        "@NoPromptForPassword 1",
        f"force_install_dir {_quote(str(Path(library_root)))}",
        _login_command(credentials),
        f"workshop_build_item {_quote(str(Path(vdf_path).resolve()))}",
        "quit",
    ]
    return "\n".join(lines) + "\n"


def redact_secrets(text: str, credentials: SteamCredentials) -> str:
    redacted = text
    for secret in (credentials.password, credentials.guard_code):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _default_fetcher(url: str) -> bytes:
    with urlopen(url, timeout=120) as response:
        return response.read()


def _safe_destination(root: Path, member_name: str) -> Path:
    destination = (root / member_name).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"Unsafe path in SteamCMD archive: {member_name}") from error
    return destination


def install_steamcmd(
    destination: Path,
    platform_name: str | None = None,
    fetcher: Callable[[str], bytes] = _default_fetcher,
) -> Path:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    platform_name = platform_name or sys.platform
    if platform_name.startswith("win"):
        archive_data = fetcher(STEAMCMD_WINDOWS_URL)
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            for member in archive.infolist():
                target = _safe_destination(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    output.write(source.read())
        executable = destination / "steamcmd.exe"
    elif platform_name.startswith("linux"):
        archive_data = fetcher(STEAMCMD_LINUX_URL)
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError(f"Links are not allowed in SteamCMD archive: {member.name}")
                target = _safe_destination(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as output:
                    output.write(source.read())
                os.chmod(target, member.mode & 0o777)
        executable = destination / "steamcmd.sh"
        if executable.exists():
            executable.chmod(executable.stat().st_mode | 0o111)
    else:
        raise ValueError(f"Automatic SteamCMD installation is unsupported on {platform_name}")
    if not executable.is_file():
        raise FileNotFoundError("The SteamCMD archive did not contain the expected executable")
    return executable


def _absolute_path(path: Path) -> Path:
    return Path(path).expanduser().absolute()


def _is_link_or_reparse(path: Path) -> bool:
    details = os.lstat(path)
    attributes = int(getattr(details, "st_file_attributes", 0))
    return stat.S_ISLNK(details.st_mode) or bool(
        attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_plain_directory(path: Path, label: str) -> os.stat_result:
    try:
        details = os.lstat(path)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} not found: {path}") from error
    attributes = int(getattr(details, "st_file_attributes", 0))
    if stat.S_ISLNK(details.st_mode) or (
        attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"{label} cannot be a symbolic link or reparse point: {path}")
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"{label} must be a directory: {path}")
    return details


def _assert_plain_tree(root: Path, label: str) -> None:
    root_details = _assert_plain_directory(root, label)
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directory_names, *file_names):
            candidate = current_path / name
            details = os.lstat(candidate)
            attributes = int(getattr(details, "st_file_attributes", 0))
            if stat.S_ISLNK(details.st_mode) or (
                attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError(
                    f"{label} contains a symbolic link or reparse point: {candidate}"
                )
            if stat.S_ISDIR(details.st_mode) and (
                details.st_dev != root_details.st_dev or os.path.ismount(candidate)
            ):
                raise ValueError(f"{label} contains a nested mount point: {candidate}")


def _directory_hash(
    root: Path,
    *,
    exclude_root_snapshot_metadata: bool = False,
) -> str:
    root = _absolute_path(root)
    _assert_plain_tree(root, "Hashed directory")
    files: list[Path] = []
    for current, _directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(root)
            if exclude_root_snapshot_metadata and relative == Path("snapshot.json"):
                continue
            files.append(path)
    digest = hashlib.sha256()
    # Preserve the original pathlib ordering used by existing snapshot hashes.
    # WindowsPath compares case-insensitively, which differs from sorting POSIX
    # strings when a Workshop item contains mixed-case sibling names.
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class StoredWorkshopSnapshot:
    workshop_id: str
    revision_directory: str
    sha256: str | None
    path: Path
    snapshot_created_at_utc: str | None
    workshop_updated_at_utc: str | None
    workshop_manifest_id: str | None
    mod_folders: tuple[str, ...]
    metadata_state: str
    metadata_message: str

    @property
    def is_valid(self) -> bool:
        return self.metadata_state in {"valid", "legacy"}

    @property
    def is_deletable(self) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{16,64}", self.revision_directory))


@dataclass(frozen=True)
class WorkshopSnapshotInventory:
    snapshot_root: Path
    snapshots: tuple[StoredWorkshopSnapshot, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkshopSnapshotDeletionResult:
    deleted_paths: tuple[Path, ...]
    remaining: tuple[StoredWorkshopSnapshot, ...]


def _stored_timestamp(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return float("-inf")


def _filesystem_timestamp(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(
            os.lstat(path).st_mtime,
            UTC,
        ).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def _metadata_timestamp(
    payload: dict[str, object],
    field: str,
    errors: list[str],
) -> str | None:
    value = payload.get(field)
    if value is None or str(value).strip() == "":
        return None
    cleaned = str(value).strip()
    if _stored_timestamp(cleaned) == float("-inf"):
        errors.append(f"{field} is not a valid ISO-8601 timestamp")
        return None
    return cleaned


def _stored_mod_folders(path: Path) -> tuple[str, ...]:
    mods = path / "mods"
    try:
        if _is_link_or_reparse(mods):
            return ()
        details = os.lstat(mods)
    except OSError:
        return ()
    if not stat.S_ISDIR(details.st_mode):
        return ()
    result: list[str] = []
    try:
        for entry in os.scandir(mods):
            entry_path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False) and not _is_link_or_reparse(
                entry_path
            ):
                result.append(entry.name)
    except OSError:
        return ()
    return tuple(sorted(result, key=str.casefold))


def _stored_snapshot_record(
    workshop_id: str,
    path: Path,
) -> StoredWorkshopSnapshot:
    revision_directory = path.name
    metadata_path = path / "snapshot.json"
    fallback_created = _filesystem_timestamp(metadata_path) or _filesystem_timestamp(path)
    errors: list[str] = []
    sha256: str | None = None
    snapshot_created_at_utc = fallback_created
    workshop_updated_at_utc: str | None = None
    workshop_manifest_id: str | None = None
    metadata_state = "malformed"
    plain_snapshot_entry = False

    try:
        path_details = os.lstat(path)
        path_attributes = int(getattr(path_details, "st_file_attributes", 0))
        if stat.S_ISLNK(path_details.st_mode) or (
            path_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            errors.append("snapshot entry is a symbolic link or reparse point")
        elif not stat.S_ISDIR(path_details.st_mode):
            errors.append("snapshot entry is not a directory")
        else:
            plain_snapshot_entry = True
    except OSError as error:
        errors.append(f"snapshot entry cannot be inspected: {error}")

    payload: dict[str, object] = {}
    metadata_loaded = False
    if not errors:
        try:
            if _is_link_or_reparse(metadata_path):
                errors.append("snapshot.json is a symbolic link or reparse point")
            else:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
                    metadata_loaded = True
                else:
                    errors.append("snapshot.json must contain a JSON object")
        except FileNotFoundError:
            errors.append("snapshot.json is missing")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"snapshot.json is malformed: {error}")

    format_version = payload.get("format_version")
    if metadata_loaded:
        if isinstance(format_version, bool) or format_version not in {1, 2}:
            errors.append(f"unsupported snapshot metadata format: {format_version!r}")
        metadata_workshop_id = str(payload.get("workshop_id") or "")
        if metadata_workshop_id != workshop_id:
            errors.append(
                "snapshot Workshop ID does not match its parent directory"
            )
        metadata_sha256 = str(payload.get("sha256") or "").strip()
        if re.fullmatch(r"[0-9a-f]{64}", metadata_sha256):
            sha256 = metadata_sha256
        else:
            errors.append("snapshot SHA-256 must contain 64 lowercase hexadecimal digits")
        if not re.fullmatch(r"[0-9a-f]{16,64}", revision_directory) or (
            sha256 is not None and not sha256.startswith(revision_directory)
        ):
            errors.append(
                "snapshot revision directory must be a 16-64 character prefix "
                "of its SHA-256"
            )
        captured = _metadata_timestamp(
            payload,
            "snapshot_created_at_utc",
            errors,
        )
        if captured is not None:
            snapshot_created_at_utc = captured
        elif format_version == 2:
            errors.append("format-2 metadata requires snapshot_created_at_utc")
        workshop_updated_at_utc = _metadata_timestamp(
            payload,
            "workshop_updated_at_utc",
            errors,
        )
        raw_manifest_id = payload.get("workshop_manifest_id")
        if raw_manifest_id is not None and str(raw_manifest_id).strip():
            cleaned_manifest_id = str(raw_manifest_id).strip()
            if cleaned_manifest_id.isdigit() and int(cleaned_manifest_id) > 0:
                workshop_manifest_id = cleaned_manifest_id
            else:
                errors.append("workshop_manifest_id must be a positive integer")

    if not errors:
        if format_version == 1:
            metadata_state = "legacy"
            metadata_message = "Legacy snapshot metadata format 1"
        else:
            metadata_state = "valid"
            metadata_message = "Snapshot metadata is valid"
    else:
        metadata_message = "; ".join(dict.fromkeys(errors))
    return StoredWorkshopSnapshot(
        workshop_id=workshop_id,
        revision_directory=revision_directory,
        sha256=sha256,
        path=path,
        snapshot_created_at_utc=snapshot_created_at_utc,
        workshop_updated_at_utc=workshop_updated_at_utc,
        workshop_manifest_id=workshop_manifest_id,
        mod_folders=_stored_mod_folders(path) if plain_snapshot_entry else (),
        metadata_state=metadata_state,
        metadata_message=metadata_message,
    )


def _stored_snapshot_sort_key(
    snapshot: StoredWorkshopSnapshot,
) -> tuple[int, float, float, str]:
    updated = _stored_timestamp(snapshot.workshop_updated_at_utc)
    captured = _stored_timestamp(snapshot.snapshot_created_at_utc)
    effective = updated if updated != float("-inf") else captured
    return (
        1 if snapshot.is_valid else 0,
        effective,
        captured,
        snapshot.revision_directory,
    )


def list_stored_workshop_snapshots(snapshot_root: Path) -> WorkshopSnapshotInventory:
    root = _absolute_path(snapshot_root)
    if not root.exists():
        return WorkshopSnapshotInventory(root, ())
    _assert_plain_directory(root, "Snapshot root")
    snapshots_by_item: dict[str, list[StoredWorkshopSnapshot]] = {}
    warnings: list[str] = []
    with _SNAPSHOT_STORE_LOCK:
        for item_entry in sorted(os.scandir(root), key=lambda item: item.name):
            item_path = Path(item_entry.path)
            if not item_entry.name.isdigit():
                warnings.append(
                    f"Ignored unexpected entry in snapshot root: {item_entry.name}"
                )
                continue
            if not item_entry.is_dir(follow_symlinks=False) or _is_link_or_reparse(
                item_path
            ):
                warnings.append(
                    f"Workshop {item_entry.name} storage is not a plain directory"
                )
                continue
            records: list[StoredWorkshopSnapshot] = []
            try:
                revision_entries = sorted(
                    os.scandir(item_path),
                    key=lambda item: item.name,
                )
            except OSError as error:
                warnings.append(
                    f"Could not inspect Workshop {item_entry.name} snapshots: {error}"
                )
                continue
            for revision_entry in revision_entries:
                record = _stored_snapshot_record(
                    item_entry.name,
                    Path(revision_entry.path),
                )
                records.append(record)
                if not record.is_valid:
                    warnings.append(
                        f"Workshop {record.workshop_id} snapshot "
                        f"{record.revision_directory} is malformed: "
                        f"{record.metadata_message}"
                    )
            snapshots_by_item[item_entry.name] = records

    ordered: list[StoredWorkshopSnapshot] = []
    for workshop_id in sorted(snapshots_by_item, key=lambda value: (int(value), value)):
        ordered.extend(
            sorted(
                snapshots_by_item[workshop_id],
                key=_stored_snapshot_sort_key,
                reverse=True,
            )
        )
    return WorkshopSnapshotInventory(root, tuple(ordered), tuple(warnings))


def _validate_workshop_storage_id(workshop_id: str) -> None:
    if not re.fullmatch(r"\d+", workshop_id):
        raise ValueError(f"Invalid Workshop ID: {workshop_id}")


def _validate_revision_directory(revision_directory: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{16,64}", revision_directory):
        raise ValueError(f"Invalid snapshot revision directory: {revision_directory!r}")


def _assert_not_steam_workshop_cache(path: Path) -> None:
    parts = tuple(
        part.casefold()
        for part in _absolute_path(path).resolve(strict=False).parts
    )
    cache_signature = ("steamapps", "workshop", "content", PZ_APP_ID)
    signature_length = len(cache_signature)
    if any(
        parts[index : index + signature_length] == cache_signature
        for index in range(len(parts) - signature_length + 1)
    ):
        raise ValueError(
            "Immutable snapshot storage cannot be inside SteamCMD's mutable "
            f"Workshop cache: {path}"
        )


def _validated_snapshot_delete_target(
    root: Path,
    workshop_id: str,
    record: StoredWorkshopSnapshot,
) -> Path:
    _validate_revision_directory(record.revision_directory)
    item_root = root / workshop_id
    _assert_plain_directory(root, "Snapshot root")
    _assert_plain_directory(item_root, f"Workshop {workshop_id} snapshot directory")
    _assert_plain_tree(record.path, "Stored snapshot")
    resolved_root = root.resolve(strict=True)
    resolved_item = item_root.resolve(strict=True)
    resolved_target = record.path.resolve(strict=True)
    if resolved_item.parent != resolved_root or resolved_target.parent != resolved_item:
        raise ValueError(
            f"Stored snapshot is outside the configured snapshot root: {record.path}"
        )
    return record.path


def _remove_snapshot_tree(path: Path) -> None:
    def handle_read_only(
        function: Callable[[str], object],
        candidate: str,
        _error: tuple[type[BaseException], BaseException, object],
    ) -> None:
        os.chmod(candidate, stat.S_IWRITE)
        function(candidate)

    shutil.rmtree(path, onerror=handle_read_only)


def delete_stored_workshop_snapshot(
    snapshot_root: Path,
    workshop_id: str,
    revision_directory: str,
) -> WorkshopSnapshotDeletionResult:
    _validate_workshop_storage_id(workshop_id)
    _validate_revision_directory(revision_directory)
    root = _absolute_path(snapshot_root)
    _assert_not_steam_workshop_cache(root)
    with _SNAPSHOT_STORE_LOCK:
        inventory = list_stored_workshop_snapshots(root)
        matches = [
            item
            for item in inventory.snapshots
            if item.workshop_id == workshop_id
            and item.revision_directory == revision_directory
        ]
        if len(matches) != 1:
            raise FileNotFoundError(
                f"Stored Workshop {workshop_id} snapshot "
                f"{revision_directory!r} was not found"
            )
        target = _validated_snapshot_delete_target(root, workshop_id, matches[0])
        _remove_snapshot_tree(target)
        item_root = root / workshop_id
        try:
            item_root.rmdir()
        except OSError:
            pass
        remaining = tuple(
            item
            for item in list_stored_workshop_snapshots(root).snapshots
            if item.workshop_id == workshop_id
        )
        return WorkshopSnapshotDeletionResult((target,), remaining)


def delete_all_stored_workshop_snapshots(
    snapshot_root: Path,
    workshop_id: str,
) -> WorkshopSnapshotDeletionResult:
    _validate_workshop_storage_id(workshop_id)
    root = _absolute_path(snapshot_root)
    _assert_not_steam_workshop_cache(root)
    item_root = root / workshop_id
    with _SNAPSHOT_STORE_LOCK:
        _assert_plain_directory(root, "Snapshot root")
        _assert_plain_directory(
            item_root,
            f"Workshop {workshop_id} snapshot directory",
        )
        inventory = list_stored_workshop_snapshots(root)
        records = tuple(
            item for item in inventory.snapshots if item.workshop_id == workshop_id
        )
        direct_entries = tuple(os.scandir(item_root))
        if len(records) != len(direct_entries):
            raise ValueError(
                f"Workshop {workshop_id} snapshot directory contains entries that "
                "could not be inventoried safely"
            )
        targets = tuple(
            _validated_snapshot_delete_target(root, workshop_id, record)
            for record in records
        )
        for target in targets:
            _remove_snapshot_tree(target)
        item_root.rmdir()
        return WorkshopSnapshotDeletionResult(targets, ())


@dataclass(frozen=True)
class _WorkshopItemMetadata:
    updated_at_utc: str | None = None
    manifest_id: str | None = None


_KEYVALUES_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


def _parse_keyvalues(text: str) -> dict[str, object]:
    """Parse the small Valve KeyValues subset used by appworkshop ACF files."""
    tokens: list[str] = []
    cursor = 0
    for match in _KEYVALUES_TOKEN.finditer(text):
        if text[cursor : match.start()].strip():
            raise ValueError("Malformed Valve KeyValues data")
        quoted, brace = match.groups()
        token = brace if brace is not None else quoted
        if token is None:
            raise ValueError("Malformed Valve KeyValues data")
        tokens.append(token)
        cursor = match.end()
    if text[cursor:].strip() or not tokens:
        raise ValueError("Malformed Valve KeyValues data")

    def parse_object(index: int, nested: bool) -> tuple[dict[str, object], int]:
        result: dict[str, object] = {}
        while index < len(tokens):
            key = tokens[index]
            if key == "}":
                if not nested:
                    raise ValueError("Unexpected closing brace in Valve KeyValues data")
                return result, index + 1
            if key == "{":
                raise ValueError("Unexpected opening brace in Valve KeyValues data")
            index += 1
            if index >= len(tokens) or tokens[index] == "}":
                raise ValueError("Missing value in Valve KeyValues data")
            value: object = tokens[index]
            index += 1
            if value == "{":
                value, index = parse_object(index, True)
            result[key] = value
        if nested:
            raise ValueError("Unclosed object in Valve KeyValues data")
        return result, index

    parsed, final_index = parse_object(0, False)
    if final_index != len(tokens):
        raise ValueError("Trailing Valve KeyValues data")
    return parsed


def _casefolded_value(mapping: object, key: str) -> object | None:
    if not isinstance(mapping, dict):
        return None
    wanted = key.casefold()
    return next(
        (value for candidate, value in mapping.items() if str(candidate).casefold() == wanted),
        None,
    )


def _positive_numeric_value(mapping: object, key: str) -> str | None:
    value = _casefolded_value(mapping, key)
    cleaned = str(value).strip() if value is not None else ""
    return cleaned if cleaned.isdigit() and int(cleaned) > 0 else None


def _unix_timestamp_utc(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _read_workshop_item_metadata(
    library_root: Path,
    workshop_ids: tuple[str, ...],
) -> dict[str, _WorkshopItemMetadata]:
    """Read current Workshop revision metadata without making downloads depend on it."""
    metadata_path = (
        Path(library_root)
        / "steamapps"
        / "workshop"
        / f"appworkshop_{PZ_APP_ID}.acf"
    )
    try:
        payload = _parse_keyvalues(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return {}
    app_workshop = _casefolded_value(payload, "AppWorkshop")
    installed_items = _casefolded_value(app_workshop, "WorkshopItemsInstalled")
    item_details = _casefolded_value(app_workshop, "WorkshopItemDetails")
    result: dict[str, _WorkshopItemMetadata] = {}
    for workshop_id in workshop_ids:
        installed = _casefolded_value(installed_items, workshop_id)
        details = _casefolded_value(item_details, workshop_id)
        updated_timestamp = _positive_numeric_value(details, "latest_timeupdated")
        if updated_timestamp is None:
            updated_timestamp = _positive_numeric_value(installed, "timeupdated")
        manifest_id = _positive_numeric_value(details, "latest_manifest")
        if manifest_id is None:
            manifest_id = _positive_numeric_value(installed, "manifest")
        updated_at_utc = _unix_timestamp_utc(updated_timestamp)
        if updated_at_utc is not None or manifest_id is not None:
            result[workshop_id] = _WorkshopItemMetadata(
                updated_at_utc=updated_at_utc,
                manifest_id=manifest_id,
            )
    return result


@dataclass(frozen=True)
class WorkshopSnapshot:
    workshop_id: str
    sha256: str
    path: Path
    snapshot_created_at_utc: str | None = None
    workshop_updated_at_utc: str | None = None
    workshop_manifest_id: str | None = None
    created: bool = False


def _snapshot_metadata_created_at(metadata_path: Path) -> str:
    try:
        timestamp = metadata_path.stat().st_mtime
    except OSError:
        return datetime.now(UTC).isoformat(timespec="seconds")
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _write_snapshot_metadata(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validated_snapshot_provenance(
    workshop_updated_at_utc: str | None,
    workshop_manifest_id: str | None,
) -> tuple[str | None, str | None]:
    cleaned_updated_at = (
        workshop_updated_at_utc.strip()
        if workshop_updated_at_utc is not None
        else None
    )
    if cleaned_updated_at and _stored_timestamp(cleaned_updated_at) == float("-inf"):
        raise ValueError("Workshop update time must be a valid ISO-8601 timestamp")
    cleaned_manifest_id = (
        workshop_manifest_id.strip()
        if workshop_manifest_id is not None
        else None
    )
    if cleaned_manifest_id and (
        not cleaned_manifest_id.isdigit() or int(cleaned_manifest_id) <= 0
    ):
        raise ValueError("Workshop manifest ID must be a positive integer")
    return cleaned_updated_at or None, cleaned_manifest_id or None


def _read_existing_snapshot_metadata(
    record: StoredWorkshopSnapshot,
    expected_workshop_id: str,
    expected_sha256: str,
) -> dict[str, object]:
    if (
        not record.is_valid
        or record.workshop_id != expected_workshop_id
        or record.sha256 != expected_sha256
    ):
        raise ValueError(
            f"Existing snapshot metadata does not match Workshop item "
            f"{expected_workshop_id}: {record.path}"
        )
    metadata_path = record.path / "snapshot.json"
    try:
        loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Existing snapshot metadata is unreadable: {metadata_path}"
        ) from error
    if not isinstance(loaded, dict):
        raise ValueError(f"Existing snapshot metadata is invalid: {metadata_path}")
    return loaded


def _verify_existing_snapshot_payload(
    record: StoredWorkshopSnapshot,
    expected_sha256: str,
) -> None:
    actual_sha256 = _directory_hash(
        record.path,
        exclude_root_snapshot_metadata=True,
    )
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Existing snapshot payload hash does not match its immutable identity: "
            f"{record.path}"
        )


def create_snapshot(
    downloaded_item: Path,
    snapshot_root: Path,
    workshop_id: str,
    *,
    workshop_updated_at_utc: str | None = None,
    workshop_manifest_id: str | None = None,
) -> WorkshopSnapshot:
    _validate_workshop_storage_id(workshop_id)
    workshop_updated_at_utc, workshop_manifest_id = _validated_snapshot_provenance(
        workshop_updated_at_utc,
        workshop_manifest_id,
    )
    downloaded_item = Path(downloaded_item).resolve()
    if not downloaded_item.is_dir():
        raise FileNotFoundError(f"Downloaded Workshop item not found: {downloaded_item}")
    root = _absolute_path(snapshot_root)
    resolved_root = root.resolve(strict=False)
    if (
        resolved_root == downloaded_item
        or resolved_root.is_relative_to(downloaded_item)
        or downloaded_item.is_relative_to(resolved_root)
    ):
        raise ValueError(
            "Immutable snapshot storage and the mutable Workshop download cache "
            f"must be separate: {root} and {downloaded_item}"
        )
    _assert_not_steam_workshop_cache(root)
    reserved_metadata = downloaded_item / "snapshot.json"
    if reserved_metadata.exists() or reserved_metadata.is_symlink():
        raise ValueError(
            f"Downloaded Workshop item contains reserved snapshot.json: {downloaded_item}"
        )
    sha256 = _directory_hash(downloaded_item)
    with _SNAPSHOT_STORE_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        _assert_plain_directory(root, "Snapshot root")
        item_root = root / workshop_id
        item_root.mkdir(exist_ok=True)
        _assert_plain_directory(
            item_root,
            f"Workshop {workshop_id} snapshot directory",
        )
        inventory = list_stored_workshop_snapshots(root)
        same_content = [
            record
            for record in inventory.snapshots
            if record.workshop_id == workshop_id
            and record.sha256 == sha256
            and record.is_valid
        ]
        if len(same_content) > 1:
            raise ValueError(
                f"Multiple stored snapshots claim Workshop {workshop_id} hash {sha256}"
            )
        if same_content:
            record = same_content[0]
            existing = _read_existing_snapshot_metadata(
                record,
                workshop_id,
                sha256,
            )
            _verify_existing_snapshot_payload(record, sha256)
            metadata_path = record.path / "snapshot.json"
            snapshot_created_at_utc = str(
                existing.get("snapshot_created_at_utc")
                or _snapshot_metadata_created_at(metadata_path)
            )
            effective_updated_at = (
                workshop_updated_at_utc
                or str(existing.get("workshop_updated_at_utc") or "").strip()
                or None
            )
            effective_manifest_id = (
                workshop_manifest_id
                or str(existing.get("workshop_manifest_id") or "").strip()
                or None
            )
            metadata: dict[str, object] = {
                **existing,
                "format_version": 2,
                "workshop_id": workshop_id,
                "sha256": sha256,
                "snapshot_created_at_utc": snapshot_created_at_utc,
            }
            if effective_updated_at:
                metadata["workshop_updated_at_utc"] = effective_updated_at
            if effective_manifest_id:
                metadata["workshop_manifest_id"] = effective_manifest_id
            if metadata != existing:
                _write_snapshot_metadata(metadata_path, metadata)
            return WorkshopSnapshot(
                workshop_id,
                sha256,
                record.path,
                snapshot_created_at_utc,
                effective_updated_at,
                effective_manifest_id,
                False,
            )

        preferred_destination = item_root / sha256[:16]
        destination = preferred_destination
        if preferred_destination.exists() or preferred_destination.is_symlink():
            preferred_records = [
                record
                for record in inventory.snapshots
                if record.workshop_id == workshop_id
                and record.revision_directory == preferred_destination.name
            ]
            if len(preferred_records) != 1 or not preferred_records[0].is_valid:
                raise ValueError(
                    f"Existing snapshot metadata is missing or malformed: "
                    f"{preferred_destination}"
                )
            destination = item_root / sha256
            if destination.exists() or destination.is_symlink():
                raise ValueError(
                    f"Full-hash snapshot destination already exists unexpectedly: "
                    f"{destination}"
                )

        snapshot_created_at_utc = datetime.now(UTC).isoformat(timespec="seconds")
        metadata = {
            "format_version": 2,
            "workshop_id": workshop_id,
            "sha256": sha256,
            "snapshot_created_at_utc": snapshot_created_at_utc,
        }
        if workshop_updated_at_utc:
            metadata["workshop_updated_at_utc"] = workshop_updated_at_utc
        if workshop_manifest_id:
            metadata["workshop_manifest_id"] = workshop_manifest_id
        staging_parent = Path(
            tempfile.mkdtemp(
                prefix=f".{sha256[:16]}.",
                suffix=".tmp",
                dir=item_root,
            )
        )
        staging = staging_parent / "snapshot"
        try:
            shutil.copytree(downloaded_item, staging)
            copied_sha256 = _directory_hash(staging)
            if copied_sha256 != sha256:
                raise ValueError(
                    f"Workshop {workshop_id} content changed while being copied into "
                    "an immutable snapshot"
                )
            _write_snapshot_metadata(staging / "snapshot.json", metadata)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(
                    f"Snapshot destination appeared while copying: {destination}"
                )
            staging.rename(destination)
        finally:
            if staging_parent.exists():
                shutil.rmtree(staging_parent)
        return WorkshopSnapshot(
            workshop_id,
            sha256,
            destination,
            snapshot_created_at_utc,
            workshop_updated_at_utc,
            workshop_manifest_id,
            True,
        )


@dataclass(frozen=True)
class SteamCmdResult:
    success: bool
    return_code: int
    output: str


def _login_confirmed(output: str) -> bool:
    normalized = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", output)
    normalized = normalized.replace("\r", "")
    if "logged in ok" in normalized.lower():
        return True
    return bool(
        re.search(
            r"to\s+Steam\s+Public\s*\.\.\.\s*OK",
            normalized,
            re.IGNORECASE,
        )
        and re.search(
            r"Waiting\s+for\s+user\s+info\s*\.\.\.\s*OK",
            normalized,
            re.IGNORECASE,
        )
    )


class SteamCmdClient:
    def __init__(self, executable: Path, library_root: Path) -> None:
        self.executable = Path(executable).resolve()
        self.library_root = Path(library_root).resolve()

    def _execute_script(
        self,
        script: str,
        credentials: SteamCredentials,
        timeout: int,
        output_callback: Callable[[str], None] | None = None,
    ) -> SteamCmdResult:
        if not self.executable.is_file():
            raise FileNotFoundError(f"SteamCMD executable not found: {self.executable}")
        self.library_root.mkdir(parents=True, exist_ok=True)
        runscript_directory = self.library_root / ".pzmodpack-runscripts"
        runscript_directory.mkdir(parents=True, exist_ok=True)
        descriptor, runscript_name = tempfile.mkstemp(
            prefix="steamcmd-",
            suffix=".txt",
            dir=runscript_directory,
            text=True,
        )
        runscript_path = Path(runscript_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(script)
            if output_callback is None:
                try:
                    completed = subprocess.run(
                        [str(self.executable), "+runscript", str(runscript_path)],
                        cwd=self.executable.parent,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    partial = error.output or ""
                    if isinstance(partial, bytes):
                        partial = partial.decode("utf-8", "replace")
                    output = redact_secrets(
                        partial
                        + f"\nSteamCMD timed out after {timeout} seconds. "
                        "It may be waiting for a Steam Guard code or completing its "
                        "first-run update.",
                        credentials,
                    )
                    return SteamCmdResult(False, 124, output.strip())
                return_code = completed.returncode
                raw_output = completed.stdout or ""
            else:
                return_code, raw_output, timed_out = self._execute_streaming(
                    runscript_path,
                    credentials,
                    timeout,
                    output_callback,
                )
                if timed_out:
                    output = redact_secrets(
                        raw_output
                        + f"\nSteamCMD timed out after {timeout} seconds. "
                        "It may be waiting for a Steam Guard code or completing its "
                        "first-run update.",
                        credentials,
                    )
                    return SteamCmdResult(False, 124, output.strip())
        finally:
            runscript_path.unlink(missing_ok=True)
        output = redact_secrets(raw_output, credentials)
        failure_patterns = (
            r"Invalid Password",
            r"Login Failure",
            r"No subscription",
            r"ERROR!",
            r"(?:^|\n)\s*FAILED(?:\s*\(|\s*:)",
        )
        success = return_code == 0 and not any(
            re.search(pattern, output, re.IGNORECASE)
            for pattern in failure_patterns
        )
        return SteamCmdResult(success, return_code, output)

    def _execute_streaming(
        self,
        runscript_path: Path,
        credentials: SteamCredentials,
        timeout: int,
        output_callback: Callable[[str], None],
    ) -> tuple[int, str, bool]:
        process = subprocess.Popen(
            [str(self.executable), "+runscript", str(runscript_path)],
            cwd=self.executable.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            process.kill()
            process.wait()
            raise OSError("SteamCMD output stream was not available")

        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        output_parts: list[str] = []
        timed_out = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    process.kill()
                    break
                try:
                    line = output_queue.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    break
                output_parts.append(line)
                output_callback(redact_secrets(line.rstrip("\r\n"), credentials))
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=1)
            raise

        if timed_out:
            return_code = process.wait()
        else:
            try:
                return_code = process.wait(timeout=max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                return_code = process.wait()
        reader.join(timeout=1)
        while not output_queue.empty():
            line = output_queue.get_nowait()
            if line is not None:
                output_parts.append(line)
        return return_code, "".join(output_parts), timed_out

    def test_login(
        self,
        credentials: SteamCredentials,
        timeout: int = 120,
    ) -> SteamCmdResult:
        script = build_command_script((), credentials, self.library_root)
        result = self._execute_script(script, credentials, timeout)
        if result.success and not _login_confirmed(result.output):
            return SteamCmdResult(
                False,
                result.return_code or 1,
                result.output
                + "\nSteamCMD exited but did not confirm a successful login.",
            )
        return result

    def download(
        self,
        workshop_ids: tuple[str, ...],
        credentials: SteamCredentials,
        timeout: int = 1800,
        output_callback: Callable[[str], None] | None = None,
    ) -> SteamCmdResult:
        if not workshop_ids:
            raise ValueError("At least one Workshop ID is required")
        script = build_command_script(workshop_ids, credentials, self.library_root)
        return self._execute_script(
            script,
            credentials,
            timeout,
            output_callback=output_callback,
        )

    def upload(
        self,
        vdf_path: Path,
        credentials: SteamCredentials,
        timeout: int = 1800,
    ) -> SteamCmdResult:
        script = build_upload_script(vdf_path, credentials, self.library_root)
        return self._execute_script(script, credentials, timeout)

    def downloaded_item_path(self, workshop_id: str) -> Path:
        return (
            self.library_root
            / "steamapps"
            / "workshop"
            / "content"
            / PZ_APP_ID
            / workshop_id
        )


@dataclass(frozen=True)
class DownloadBatchResult:
    command_result: SteamCmdResult
    snapshots: tuple[WorkshopSnapshot, ...]


class _WorkshopDownloadProgress:
    def __init__(
        self,
        workshop_ids: tuple[str, ...],
        callback: Callable[[int, int, str], None],
    ) -> None:
        self.workshop_ids = workshop_ids
        self.callback = callback
        self.completed: set[str] = set()
        self.current_id: str | None = None

    def _next_pending(self) -> str | None:
        return next(
            (item for item in self.workshop_ids if item not in self.completed),
            None,
        )

    def _position(self, workshop_id: str) -> int:
        return self.workshop_ids.index(workshop_id) + 1

    def _download_progress(self, fraction: float = 0.0) -> int:
        completed = min(len(self.completed) + fraction, len(self.workshop_ids))
        return int(85 * completed / len(self.workshop_ids))

    def feed(self, line: str) -> None:
        command = re.search(
            rf"(?:workshop_download_item\s+{PZ_APP_ID}\s+|"
            r"downloading\s+(?:Workshop\s+)?item\s+)(\d+)",
            line,
            re.IGNORECASE,
        )
        if command and command.group(1) in self.workshop_ids:
            self.current_id = command.group(1)
            self.callback(
                self._download_progress(),
                100,
                f"Downloading Workshop item {self._position(self.current_id)}/"
                f"{len(self.workshop_ids)} ({self.current_id})...",
            )

        success = re.search(
            r"Success\.\s+(?:Downloaded|Updated)\s+item\s+(\d+)",
            line,
            re.IGNORECASE,
        )
        if success and success.group(1) in self.workshop_ids:
            completed_id = success.group(1)
            self.completed.add(completed_id)
            self.current_id = self._next_pending()
            self.callback(
                self._download_progress(),
                100,
                f"Downloaded {len(self.completed)}/{len(self.workshop_ids)} Workshop "
                f"items (finished {completed_id}).",
            )
            return

        progress_match = re.search(
            r"progress:\s*(\d+(?:\.\d+)?)",
            line,
            re.IGNORECASE,
        )
        if progress_match and self.current_id is not None:
            item_progress = min(max(float(progress_match.group(1)), 0.0), 100.0)
            self.callback(
                self._download_progress(item_progress / 100),
                100,
                f"Downloading Workshop item {self._position(self.current_id)}/"
                f"{len(self.workshop_ids)} ({self.current_id}): "
                f"{item_progress:.0f}%",
            )
            return

        activity = line.strip()
        if activity and any(
            token in activity.lower()
            for token in ("logging in", "waiting for", "checking for", "downloading")
        ):
            self.callback(
                self._download_progress(),
                100,
                f"SteamCMD: {activity[:180]}",
            )

    def finish(self) -> None:
        self.completed.update(self.workshop_ids)
        self.current_id = None
        self.callback(
            85,
            100,
            f"Downloaded {len(self.workshop_ids)}/{len(self.workshop_ids)} Workshop "
            "items. Creating immutable snapshots...",
        )


def download_and_snapshot(
    client: SteamCmdClient,
    workshop_ids: tuple[str, ...],
    credentials: SteamCredentials,
    snapshot_root: Path,
    timeout: int = 1800,
    progress: Callable[[int, int, str], None] | None = None,
) -> DownloadBatchResult:
    tracker = (
        _WorkshopDownloadProgress(workshop_ids, progress)
        if progress is not None
        else None
    )
    if tracker is not None:
        progress(
            0,
            100,
            f"Starting SteamCMD download for {len(workshop_ids)} Workshop item(s)...",
        )
        command_result = client.download(
            workshop_ids,
            credentials,
            timeout,
            output_callback=tracker.feed,
        )
    else:
        command_result = client.download(workshop_ids, credentials, timeout)
    if not command_result.success:
        if progress is not None:
            progress(0, 100, "SteamCMD download failed; review the output log.")
        return DownloadBatchResult(command_result, ())
    if tracker is not None:
        tracker.finish()
    workshop_metadata = _read_workshop_item_metadata(client.library_root, workshop_ids)
    snapshots: list[WorkshopSnapshot] = []
    for index, workshop_id in enumerate(workshop_ids, start=1):
        if progress is not None:
            progress(
                85 + int(15 * (index - 1) / len(workshop_ids)),
                100,
                f"Snapshotting Workshop item {index}/{len(workshop_ids)} "
                f"({workshop_id})...",
            )
        item_metadata = workshop_metadata.get(workshop_id, _WorkshopItemMetadata())
        snapshots.append(
            create_snapshot(
                client.downloaded_item_path(workshop_id),
                snapshot_root,
                workshop_id,
                workshop_updated_at_utc=item_metadata.updated_at_utc,
                workshop_manifest_id=item_metadata.manifest_id,
            )
        )
        if progress is not None:
            progress(
                85 + int(15 * index / len(workshop_ids)),
                100,
                f"Snapshot complete {index}/{len(workshop_ids)} ({workshop_id}).",
            )
    return DownloadBatchResult(command_result, tuple(snapshots))


@dataclass(frozen=True)
class WorkshopUploadResult:
    command_result: SteamCmdResult
    published_file_id: str
    vdf_path: Path


def _published_file_id(vdf_path: Path, output: str, fallback: str) -> str:
    if vdf_path.is_file():
        match = re.search(
            r'"publishedfileid"\s+"(\d+)"',
            vdf_path.read_text(encoding="utf-8"),
        )
        if match:
            return match.group(1)
    match = re.search(r"published\s*file\s*id\D+(\d+)", output, re.IGNORECASE)
    return match.group(1) if match else fallback


def _replace_setting(path: Path, prefix: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    replacement = f"{prefix}{value}"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _workshop_label(value: object, workshop_id: str) -> str:
    label = re.sub(r"\s+", " ", str(value or "")).strip()
    label = label.replace("[", "(").replace("]", ")")
    return label or f"Workshop item {workshop_id}"


def build_workshop_description(
    manifest: dict[str, object],
    program_version: str = __version__,
) -> str:
    """Append reproducible builder attribution and bundled-mod Workshop links."""
    base_description = str(
        manifest.get("description", "Built with PZ Modpack Builder")
    ).rstrip()
    build_version = str(manifest.get("builder_version") or program_version).strip()
    footer = [
        "Modpack made with "
        f"[url={PROJECT_GITHUB_URL}]ProjectZomboid Modpack Builder[/url] "
        f"Version: {build_version}"
    ]

    bundled_links: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    mods = manifest.get("mods", [])
    if isinstance(mods, list):
        for mod in mods:
            if not isinstance(mod, dict):
                continue
            workshop_id = str(mod.get("source_workshop_id") or "").strip()
            if not re.fullmatch(r"\d+", workshop_id) or workshop_id == "0":
                continue
            label = _workshop_label(
                mod.get("display_name") or mod.get("source_folder"),
                workshop_id,
            )
            key = (workshop_id, label)
            if key in seen:
                continue
            seen.add(key)
            bundled_links.append((label, workshop_id))

    if bundled_links:
        footer.extend(("", "Bundled Workshop mods:"))
        for label, workshop_id in sorted(
            bundled_links,
            key=lambda item: (item[0].casefold(), item[1]),
        ):
            footer.append(
                f"[url={WORKSHOP_ITEM_URL.format(workshop_id)}]"
                f"{label} (Workshop ID: {workshop_id})[/url]"
            )

    footer_text = "\n".join(footer)
    description = (
        f"{base_description}\n\n{footer_text}" if base_description else footer_text
    )
    description = _typographic_double_quotes(description)
    description_size = len(description.encode("utf-8"))
    if description_size > WORKSHOP_DESCRIPTION_MAX_BYTES:
        raise ValueError(
            "Generated Workshop description is "
            f"{description_size} bytes, exceeding Steam's "
            f"{WORKSHOP_DESCRIPTION_MAX_BYTES}-byte limit. Shorten the pack "
            "description or bundled-mod display names; attribution links were not "
            "silently removed."
        )
    return description


def upload_modpack(
    client: SteamCmdClient,
    build_output: Path,
    credentials: SteamCredentials,
    change_note: str | None = None,
    timeout: int = 1800,
) -> WorkshopUploadResult:
    if credentials.is_anonymous:
        raise ValueError("Uploading Workshop items requires a Steam account login")
    build_output = Path(build_output).resolve()
    if not (build_output / ".pzmodpack-output").is_file():
        raise ValueError("Upload source is not a generated PZ Modpack Builder output")
    manifest_path = build_output / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Build manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    supplied_change_note = str(change_note or "").strip()
    effective_change_note = supplied_change_note or str(
        manifest.get("generated_change_note") or ""
    ).strip()
    if not effective_change_note:
        raise ValueError(
            "Workshop change note is empty; build a versioned pack or provide one"
        )
    config = WorkshopUploadConfig(
        published_file_id=str(manifest.get("workshop_id", "0")),
        content_folder=build_output / "Contents",
        preview_file=build_output / "preview.png",
        visibility=int(manifest.get("visibility", 2)),
        title=str(manifest.get("name", "Project Zomboid Modpack")),
        description=build_workshop_description(manifest),
        change_note=effective_change_note,
    )
    vdf_path = write_upload_vdf(build_output / "workshop_upload.vdf", config)
    command_result = client.upload(vdf_path, credentials, timeout)
    published_file_id = _published_file_id(
        vdf_path,
        command_result.output,
        config.published_file_id,
    )
    if command_result.success and published_file_id != "0":
        manifest["workshop_id"] = published_file_id
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _replace_setting(build_output / "workshop.txt", "id=", published_file_id)
        _replace_setting(
            build_output / "amp-config.txt",
            "WorkshopItems=",
            f"{published_file_id};",
        )
    return WorkshopUploadResult(command_result, published_file_id, vdf_path)
