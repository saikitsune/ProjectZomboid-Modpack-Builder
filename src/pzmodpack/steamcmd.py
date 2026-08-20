from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import shutil
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


def _vdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def write_upload_vdf(path: Path, config: WorkshopUploadConfig) -> Path:
    if not re.fullmatch(r"\d+", config.published_file_id):
        raise ValueError("Published Workshop ID must contain digits only; use 0 for a new item")
    if config.visibility not in {0, 1, 2, 3}:
        raise ValueError("Workshop visibility must be 0, 1, 2, or 3")
    content_folder = Path(config.content_folder).resolve()
    preview_file = Path(config.preview_file).resolve()
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
    path.write_text("\n".join(lines), encoding="utf-8")
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


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


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


def create_snapshot(
    downloaded_item: Path,
    snapshot_root: Path,
    workshop_id: str,
    *,
    workshop_updated_at_utc: str | None = None,
    workshop_manifest_id: str | None = None,
) -> WorkshopSnapshot:
    if not re.fullmatch(r"\d+", workshop_id):
        raise ValueError(f"Invalid Workshop ID: {workshop_id}")
    downloaded_item = Path(downloaded_item).resolve()
    if not downloaded_item.is_dir():
        raise FileNotFoundError(f"Downloaded Workshop item not found: {downloaded_item}")
    sha256 = _directory_hash(downloaded_item)
    destination = Path(snapshot_root).resolve() / workshop_id / sha256[:16]
    metadata_path = destination / "snapshot.json"
    snapshot_created_at_utc = datetime.now(UTC).isoformat(timespec="seconds")
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(downloaded_item, destination)
        existing: dict[str, object] = {}
    else:
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            loaded = {}
        existing = loaded if isinstance(loaded, dict) else {}
        if existing:
            existing_workshop_id = str(existing.get("workshop_id") or "")
            existing_sha256 = str(existing.get("sha256") or "")
            if existing_workshop_id != workshop_id or existing_sha256 != sha256:
                raise ValueError(
                    f"Existing snapshot metadata does not match Workshop item {workshop_id}"
                )
            snapshot_created_at_utc = str(
                existing.get("snapshot_created_at_utc")
                or _snapshot_metadata_created_at(metadata_path)
            )

    effective_updated_at = str(
        existing.get("workshop_updated_at_utc") or workshop_updated_at_utc or ""
    ).strip()
    effective_manifest_id = str(
        existing.get("workshop_manifest_id") or workshop_manifest_id or ""
    ).strip()
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
        destination,
        snapshot_created_at_utc,
        effective_updated_at or None,
        effective_manifest_id or None,
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
