from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from getpass import getpass
from pathlib import Path

from .backend import BuildConfig, BuildError, build_modpack, discover_mods
from .steamcmd import (
    SteamCmdClient,
    SteamCredentials,
    download_and_snapshot,
    install_steamcmd,
    parse_workshop_ids,
    upload_modpack,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pzmodpack",
        description="Build namespaced Project Zomboid Workshop modpacks.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Inspect one or more mod source directories")
    scan.add_argument("sources", nargs="+", type=Path)
    scan.add_argument("--json", action="store_true", dest="as_json")
    build = commands.add_parser("build", help="Build a namespaced Workshop upload directory")
    build.add_argument("--name", required=True)
    build.add_argument("--namespace", required=True)
    build.add_argument("--source", action="append", required=True, type=Path, dest="sources")
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--workshop-id", default="0")
    build.add_argument("--description", default="Built with PZ Modpack Builder")
    build.add_argument("--preview", type=Path)
    build.add_argument("--visibility", type=int, choices=(0, 1, 2, 3), default=2)
    build.add_argument(
        "--active-id",
        action="append",
        default=[],
        metavar="FOLDER=MODID",
        help="Select the active ID for a versioned mod folder",
    )
    build.add_argument(
        "--include-mod-id",
        action="append",
        default=None,
        metavar="MODID",
        help=(
            "Include only mod folders declaring one of these IDs; repeat for multiple "
            "bundled mods"
        ),
    )

    steam_install = commands.add_parser("steam-install", help="Install SteamCMD from Valve")
    steam_install.add_argument("--destination", required=True, type=Path)

    steam_login = commands.add_parser("steam-login", help="Test a SteamCMD login")
    steam_login.add_argument("--steamcmd", required=True, type=Path)
    steam_login.add_argument("--library", required=True, type=Path)
    steam_login.add_argument("--anonymous", action="store_true")
    steam_login.add_argument("--username")

    steam_download = commands.add_parser(
        "steam-download",
        help="Download Project Zomboid Workshop items and create immutable snapshots",
    )
    steam_download.add_argument("items", nargs="+")
    steam_download.add_argument("--steamcmd", required=True, type=Path)
    steam_download.add_argument("--library", required=True, type=Path)
    steam_download.add_argument("--snapshots", required=True, type=Path)
    steam_download.add_argument("--anonymous", action="store_true")
    steam_download.add_argument("--username")

    steam_upload = commands.add_parser(
        "steam-upload",
        help="Create or update a Workshop item from a generated modpack",
    )
    steam_upload.add_argument("--steamcmd", required=True, type=Path)
    steam_upload.add_argument("--library", required=True, type=Path)
    steam_upload.add_argument("--output", required=True, type=Path)
    steam_upload.add_argument("--username", required=True)
    steam_upload.add_argument("--cached-login", action="store_true")
    steam_upload.add_argument("--change-note", required=True)
    steam_upload.add_argument("--confirm-permissions", action="store_true")
    return parser


def _credentials(
    anonymous: bool,
    username: str | None,
    cached_login: bool = False,
) -> SteamCredentials:
    if anonymous:
        return SteamCredentials.anonymous()
    if not username:
        raise ValueError("Use --anonymous or provide --username")
    if cached_login:
        return SteamCredentials(username=username)
    password = getpass("Steam password: ")
    guard_code = getpass("Steam Guard code (blank if not required): ").strip() or None
    return SteamCredentials(username=username, password=password, guard_code=guard_code)


def _active_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        folder, separator, mod_id = value.partition("=")
        if not separator or not folder.strip() or not mod_id.strip():
            raise ValueError(f"Invalid --active-id value {value!r}; use FOLDER=MODID")
        overrides[folder.strip()] = mod_id.strip()
    return overrides


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "scan":
        mods = discover_mods(args.sources)
        payload = [
            {
                "source_root": str(mod.source_root),
                "folder_name": mod.folder_name,
                "mod_ids": list(mod.mod_ids),
                "workshop_id": mod.workshop_id,
            }
            for mod in mods
        ]
        if args.as_json:
            print(json.dumps(payload, indent=2))
        else:
            for mod in payload:
                print(f"{mod['folder_name']}: {';'.join(mod['mod_ids'])}")
        return 0
    if args.command == "build":
        try:
            report = build_modpack(
                BuildConfig(
                    name=args.name,
                    namespace=args.namespace,
                    sources=tuple(args.sources),
                    output=args.output,
                    workshop_id=args.workshop_id,
                    description=args.description,
                    preview=args.preview,
                    visibility=args.visibility,
                    active_mod_ids=_active_overrides(args.active_id),
                    included_mod_ids=(
                        tuple(args.include_mod_id)
                        if args.include_mod_id is not None
                        else None
                    ),
                )
            )
        except (BuildError, ValueError) as error:
            print(f"Build failed: {error}", file=sys.stderr)
            return 2
        print(f"Built {report.mod_count} mod(s) at {report.output}")
        for warning in report.warnings:
            print(f"WARNING: {warning}")
        return 0
    if args.command == "steam-install":
        try:
            executable = install_steamcmd(args.destination)
        except (OSError, ValueError) as error:
            print(f"SteamCMD installation failed: {error}", file=sys.stderr)
            return 2
        print(f"SteamCMD installed at {executable}")
        return 0
    if args.command == "steam-login":
        try:
            credentials = _credentials(args.anonymous, args.username)
            result = SteamCmdClient(args.steamcmd, args.library).test_login(credentials)
        except (OSError, ValueError) as error:
            print(f"Steam login failed: {error}", file=sys.stderr)
            return 2
        print(result.output.strip())
        print("Steam login succeeded." if result.success else "Steam login failed.")
        return 0 if result.success else 2
    if args.command == "steam-download":
        try:
            workshop_ids = parse_workshop_ids(args.items)
            credentials = _credentials(args.anonymous, args.username)
            batch = download_and_snapshot(
                SteamCmdClient(args.steamcmd, args.library),
                workshop_ids,
                credentials,
                args.snapshots,
            )
        except (OSError, ValueError) as error:
            print(f"Workshop download failed: {error}", file=sys.stderr)
            return 2
        print(batch.command_result.output.strip())
        if not batch.command_result.success:
            return 2
        for snapshot in batch.snapshots:
            print(
                f"Snapshot {snapshot.workshop_id}: {snapshot.sha256[:16]} at {snapshot.path}"
            )
        return 0
    if args.command == "steam-upload":
        if not args.confirm_permissions:
            print(
                "Workshop upload refused: pass --confirm-permissions after verifying redistribution rights.",
                file=sys.stderr,
            )
            return 2
        try:
            credentials = _credentials(False, args.username, args.cached_login)
            result = upload_modpack(
                SteamCmdClient(args.steamcmd, args.library),
                args.output.resolve(),
                credentials,
                args.change_note,
            )
        except (OSError, ValueError) as error:
            print(f"Workshop upload failed: {error}", file=sys.stderr)
            return 2
        print(result.command_result.output.strip())
        if not result.command_result.success:
            return 2
        print(f"Published file ID: {result.published_file_id}")
        return 0
    return 2
