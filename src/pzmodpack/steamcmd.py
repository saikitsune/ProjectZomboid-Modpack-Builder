from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

PZ_APP_ID = "108600"
STEAMCMD_WINDOWS_URL = "https://client-update.steamstatic.com/installer/steamcmd.zip"
STEAMCMD_LINUX_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"


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
class WorkshopSnapshot:
    workshop_id: str
    sha256: str
    path: Path


def create_snapshot(
    downloaded_item: Path,
    snapshot_root: Path,
    workshop_id: str,
) -> WorkshopSnapshot:
    if not re.fullmatch(r"\d+", workshop_id):
        raise ValueError(f"Invalid Workshop ID: {workshop_id}")
    downloaded_item = Path(downloaded_item).resolve()
    if not downloaded_item.is_dir():
        raise FileNotFoundError(f"Downloaded Workshop item not found: {downloaded_item}")
    sha256 = _directory_hash(downloaded_item)
    destination = Path(snapshot_root).resolve() / workshop_id / sha256[:16]
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(downloaded_item, destination)
        (destination / "snapshot.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "workshop_id": workshop_id,
                    "sha256": sha256,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return WorkshopSnapshot(workshop_id, sha256, destination)


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
                    "It may be waiting for a Steam Guard code or completing its first-run update.",
                    credentials,
                )
                return SteamCmdResult(False, 124, output.strip())
        finally:
            runscript_path.unlink(missing_ok=True)
        output = redact_secrets(completed.stdout or "", credentials)
        failure_patterns = (
            r"Invalid Password",
            r"Login Failure",
            r"No subscription",
            r"ERROR!",
            r"(?:^|\n)\s*FAILED(?:\s*\(|\s*:)",
        )
        success = completed.returncode == 0 and not any(
            re.search(pattern, output, re.IGNORECASE)
            for pattern in failure_patterns
        )
        return SteamCmdResult(success, completed.returncode, output)

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
    ) -> SteamCmdResult:
        if not workshop_ids:
            raise ValueError("At least one Workshop ID is required")
        script = build_command_script(workshop_ids, credentials, self.library_root)
        return self._execute_script(script, credentials, timeout)

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


def download_and_snapshot(
    client: SteamCmdClient,
    workshop_ids: tuple[str, ...],
    credentials: SteamCredentials,
    snapshot_root: Path,
    timeout: int = 1800,
) -> DownloadBatchResult:
    command_result = client.download(workshop_ids, credentials, timeout)
    if not command_result.success:
        return DownloadBatchResult(command_result, ())
    snapshots = tuple(
        create_snapshot(client.downloaded_item_path(workshop_id), snapshot_root, workshop_id)
        for workshop_id in workshop_ids
    )
    return DownloadBatchResult(command_result, snapshots)


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


def upload_modpack(
    client: SteamCmdClient,
    build_output: Path,
    credentials: SteamCredentials,
    change_note: str,
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
    config = WorkshopUploadConfig(
        published_file_id=str(manifest.get("workshop_id", "0")),
        content_folder=build_output / "Contents",
        preview_file=build_output / "preview.png",
        visibility=int(manifest.get("visibility", 2)),
        title=str(manifest.get("name", "Project Zomboid Modpack")),
        description=str(manifest.get("description", "Built with PZ Modpack Builder")),
        change_note=change_note,
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
