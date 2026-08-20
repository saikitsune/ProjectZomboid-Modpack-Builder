from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .version import __version__


@dataclass(frozen=True)
class DiscoveredMod:
    source_root: Path
    mod_directory: Path
    folder_name: str
    mod_ids: tuple[str, ...]
    workshop_id: str | None
    display_name: str = ""
    description: str = ""
    snapshot_sha256: str | None = None
    snapshot_created_at_utc: str | None = None
    workshop_updated_at_utc: str | None = None
    workshop_manifest_id: str | None = None


@dataclass(frozen=True)
class WorkshopSnapshotRevision:
    workshop_id: str
    revision_key: str
    source_root: Path
    sha256: str | None
    snapshot_created_at_utc: str | None
    workshop_updated_at_utc: str | None
    workshop_manifest_id: str | None
    mods: tuple[DiscoveredMod, ...]
    source_order: int = 0


@dataclass(frozen=True)
class BundledModConflict:
    declaring_mod: DiscoveredMod
    declaring_mod_id: str
    incompatible_mod: DiscoveredMod
    incompatible_mod_id: str

    @property
    def message(self) -> str:
        return (
            f"Bundled Mod ID {self.declaring_mod_id!r} declares bundled Mod ID "
            f"{self.incompatible_mod_id!r} incompatible"
        )


@dataclass(frozen=True)
class BundledModRequirement:
    declaring_mod: DiscoveredMod
    declaring_mod_id: str
    required_mod_id: str
    providers: tuple[DiscoveredMod, ...]

    @property
    def is_missing(self) -> bool:
        return not self.providers


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str


def validate_mods(
    mods: Iterable[DiscoveredMod],
    active_mod_ids: dict[str, str] | None = None,
) -> list[ValidationIssue]:
    """Validate a bundle in which every supplied mod folder is selected."""
    mod_list = list(mods)
    return validate_mod_selection(mod_list, mod_list, active_mod_ids)


def validate_mod_selection(
    available_mods: Iterable[DiscoveredMod],
    selected_mods: Iterable[DiscoveredMod],
    active_mod_ids: dict[str, str] | None = None,
) -> list[ValidationIssue]:
    """Validate selected folders against the complete discovered source inventory."""
    available_list = list(available_mods)
    mod_list = list(selected_mods)
    active_mod_ids = active_mod_ids or {}
    owners: dict[str, list[str]] = {}
    for mod in mod_list:
        for mod_id in mod.mod_ids:
            owners.setdefault(mod_id, []).append(mod.folder_name)
    issues = [
        ValidationIssue(
            severity="error",
            code="duplicate_mod_id",
            message=f"Mod ID {mod_id!r} is provided by: {', '.join(folders)}",
        )
        for mod_id, folders in sorted(owners.items())
        if len(folders) > 1
    ]
    selected = set(mod_list)
    for requirement in find_mod_requirements(available_list):
        mod = requirement.declaring_mod
        if mod not in selected or requirement.declaring_mod_id != _active_mod_id(
            mod, active_mod_ids
        ):
            continue
        selected_providers = [
            provider for provider in requirement.providers if provider in selected
        ]
        if selected_providers:
            continue
        if requirement.providers:
            provider_names = ", ".join(
                provider.display_name or provider.folder_name
                for provider in requirement.providers
            )
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="excluded_required_mod",
                    message=(
                        f"{mod.display_name or mod.folder_name} "
                        f"({requirement.declaring_mod_id}) requires Mod ID "
                        f"{requirement.required_mod_id!r}, provided by excluded bundled "
                        f"mod {provider_names}. Include a provider before building."
                    ),
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="missing_required_mod",
                    message=(
                        f"{mod.display_name or mod.folder_name} "
                        f"({requirement.declaring_mod_id}) requires Mod ID "
                        f"{requirement.required_mod_id!r}, but no discovered bundled mod "
                        "provides it. Add its Workshop item or source before building."
                    ),
                )
            )
    for conflict in find_bundled_conflicts(mod_list):
        issues.append(
            ValidationIssue(
                severity="error",
                code="bundled_incompatibility",
                message=conflict.message,
            )
        )
    return issues


@dataclass(frozen=True)
class BuildConfig:
    name: str
    namespace: str
    sources: tuple[Path, ...]
    output: Path
    workshop_id: str = "0"
    description: str = "Built with PZ Modpack Builder"
    preview: Path | None = None
    visibility: int = 2
    active_mod_ids: dict[str, str] = field(default_factory=dict)
    included_mod_ids: tuple[str, ...] | None = None
    version_bump: str = "patch"
    snapshot_selections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildReport:
    output: Path
    mod_count: int
    mapping: dict[str, str]
    warnings: tuple[str, ...]
    warning_details: tuple[dict[str, object], ...] = ()
    pack_version: str = "1.0.0"
    previous_pack_version: str | None = None
    change_note: str = ""
    archived_output: Path | None = None


class BuildError(RuntimeError):
    pass


def _safe_namespace(namespace: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", namespace.strip())
    if not cleaned or cleaned[0].isdigit():
        raise BuildError("Namespace must start with a letter or underscore")
    return cleaned.rstrip("_") + "_"


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _prepare_output(output: Path) -> None:
    if output.exists():
        marker = output / ".pzmodpack-output"
        if not marker.is_file():
            raise BuildError(f"Refusing to replace unmarked output directory: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / ".pzmodpack-output").write_text("generated\n", encoding="utf-8")


_PACK_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_VERSION_BUMPS = {"major", "minor", "patch"}


def _parse_pack_version(value: object, *, legacy_default: bool = False) -> tuple[int, int, int]:
    if value is None and legacy_default:
        return (1, 0, 0)
    match = _PACK_VERSION_PATTERN.fullmatch(str(value or "").strip())
    if match is None:
        raise BuildError(
            f"Existing pack has an invalid version {value!r}; expected MAJOR.MINOR.PATCH"
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _format_pack_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _next_pack_version(previous: object | None, bump: str) -> str:
    normalized_bump = bump.strip().lower()
    if normalized_bump not in _VERSION_BUMPS:
        raise BuildError("Version bump must be major, minor, or patch")
    if previous is None:
        return "1.0.0"
    major, minor, patch = _parse_pack_version(previous, legacy_default=True)
    if normalized_bump == "major":
        return f"{major + 1}.0.0"
    if normalized_bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _read_existing_build_manifest(output: Path) -> dict[str, object] | None:
    if not output.exists():
        return None
    if not output.is_dir() or not (output / ".pzmodpack-output").is_file():
        raise BuildError(f"Refusing to replace unmarked output directory: {output}")
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise BuildError(f"Existing generated output is missing manifest.json: {output}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise BuildError(f"Could not read the existing build manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise BuildError("Existing build manifest must contain a JSON object")
    _parse_pack_version(manifest.get("pack_version"), legacy_default=True)
    return manifest


def _history_root(output: Path) -> Path:
    return output.with_name(f"{output.name}.versions")


def _validate_history_destination(output: Path, previous_version: str | None) -> Path | None:
    history = _history_root(output)
    if previous_version is None:
        if history.exists():
            raise BuildError(
                f"Version history exists but the current output is missing: {history}. "
                "Restore the latest archived version before rebuilding."
            )
        return None
    if history.exists():
        if not history.is_dir() or not (history / ".pzmodpack-history").is_file():
            raise BuildError(f"Refusing to use unmarked version history directory: {history}")
    archive = history / f"v{previous_version}"
    if archive.exists():
        raise BuildError(
            f"Version history already contains v{previous_version}: {archive}"
        )
    return archive


def _commit_versioned_output(
    output: Path,
    staged_output: Path,
    previous_version: str | None,
) -> Path | None:
    archive = _validate_history_destination(output, previous_version)
    if archive is None:
        shutil.move(str(staged_output), str(output))
        return None

    history = archive.parent
    history.mkdir(parents=True, exist_ok=True)
    (history / ".pzmodpack-history").write_text(
        "generated version history\n",
        encoding="utf-8",
    )
    shutil.move(str(output), str(archive))
    try:
        shutil.move(str(staged_output), str(output))
    except Exception:
        if not output.exists() and archive.exists():
            shutil.move(str(archive), str(output))
        raise
    return archive


def _manifest_mods(manifest: dict[str, object]) -> list[dict[str, object]]:
    mods = manifest.get("mods", [])
    if not isinstance(mods, list):
        return []
    return [mod for mod in mods if isinstance(mod, dict)]


def _mod_identity(mod: dict[str, object]) -> str:
    workshop_id = str(mod.get("source_workshop_id") or "").strip()
    source_identity = workshop_id or str(mod.get("source") or "local").casefold()
    folder = str(mod.get("source_folder") or mod.get("packed_folder") or "").casefold()
    return f"{source_identity}:{folder}"


def _mod_change_record(mod: dict[str, object]) -> dict[str, object]:
    return {
        "source_folder": str(mod.get("source_folder") or ""),
        "display_name": str(
            mod.get("display_name") or mod.get("source_folder") or "Unknown mod"
        ),
        "source_workshop_id": mod.get("source_workshop_id"),
        "original_mod_ids": list(mod.get("original_mod_ids", [])),
        "sha256": str(mod.get("sha256") or ""),
    }


def _active_source_mod_id(
    manifest: dict[str, object],
    mod: dict[str, object],
) -> str:
    explicit = str(mod.get("active_source_mod_id") or "").strip()
    if explicit:
        return explicit
    overrides = manifest.get("active_mod_id_overrides", {})
    folder = str(mod.get("source_folder") or "")
    if isinstance(overrides, dict) and folder in overrides:
        return str(overrides[folder])
    mod_ids = mod.get("original_mod_ids", [])
    if isinstance(mod_ids, list) and mod_ids:
        return str(mod_ids[-1])
    return ""


def _detect_build_changes(
    previous: dict[str, object] | None,
    current: dict[str, object],
) -> dict[str, object]:
    if previous is None:
        return {
            "initial_build": True,
            "legacy_baseline": False,
            "added_mods": [_mod_change_record(mod) for mod in _manifest_mods(current)],
            "removed_mods": [],
            "updated_mods": [],
            "active_mod_id_changes": [],
            "settings_changes": [],
        }

    old_mods = {_mod_identity(mod): mod for mod in _manifest_mods(previous)}
    new_mods = {_mod_identity(mod): mod for mod in _manifest_mods(current)}
    added = [
        _mod_change_record(new_mods[key]) for key in sorted(new_mods.keys() - old_mods.keys())
    ]
    removed = [
        _mod_change_record(old_mods[key]) for key in sorted(old_mods.keys() - new_mods.keys())
    ]
    updated: list[dict[str, object]] = []
    active_changes: list[dict[str, object]] = []
    for key in sorted(old_mods.keys() & new_mods.keys()):
        old_mod = old_mods[key]
        new_mod = new_mods[key]
        old_hash = str(old_mod.get("sha256") or "")
        new_hash = str(new_mod.get("sha256") or "")
        if old_hash != new_hash:
            record = _mod_change_record(new_mod)
            record["previous_sha256"] = old_hash
            updated.append(record)
        old_active = _active_source_mod_id(previous, old_mod)
        new_active = _active_source_mod_id(current, new_mod)
        if old_active != new_active:
            active_changes.append(
                {
                    "source_folder": str(new_mod.get("source_folder") or ""),
                    "display_name": str(
                        new_mod.get("display_name")
                        or new_mod.get("source_folder")
                        or "Unknown mod"
                    ),
                    "previous_mod_id": old_active,
                    "mod_id": new_active,
                }
            )

    settings_changes = []
    for key, label in (
        ("name", "Pack name"),
        ("namespace", "Namespace"),
        ("description", "Description"),
        ("visibility", "Workshop visibility"),
    ):
        if previous.get(key) != current.get(key):
            settings_changes.append(
                {
                    "setting": key,
                    "label": label,
                    "previous": previous.get(key),
                    "value": current.get(key),
                }
            )

    previous_builder = str(previous.get("builder_version") or "").strip()
    current_builder = str(current.get("builder_version") or "").strip()
    return {
        "initial_build": False,
        "legacy_baseline": "pack_version" not in previous,
        "added_mods": added,
        "removed_mods": removed,
        "updated_mods": updated,
        "active_mod_id_changes": active_changes,
        "settings_changes": settings_changes,
        "builder_version_change": (
            {"previous": previous_builder, "value": current_builder}
            if previous_builder and previous_builder != current_builder
            else None
        ),
    }


def _change_mod_label(mod: dict[str, object]) -> str:
    label = str(mod.get("display_name") or mod.get("source_folder") or "Unknown mod")
    workshop_id = str(mod.get("source_workshop_id") or "").strip()
    return f"{label} (Workshop {workshop_id})" if workshop_id else label


def _build_change_note(
    name: str,
    pack_version: str,
    changes: dict[str, object],
) -> str:
    lines = [f"{name} v{pack_version}"]
    if changes.get("initial_build"):
        added = changes.get("added_mods", [])
        count = len(added) if isinstance(added, list) else 0
        lines.extend(("", f"Initial modpack build with {count} bundled mod(s)."))
        return "\n".join(lines)

    if changes.get("legacy_baseline"):
        lines.extend(("", "Version tracking enabled from the existing build."))

    sections = (
        ("Added mods", "added_mods"),
        ("Removed mods", "removed_mods"),
        ("Updated mods", "updated_mods"),
    )
    has_content_change = False
    for heading, key in sections:
        records = changes.get(key, [])
        if not isinstance(records, list) or not records:
            continue
        has_content_change = True
        lines.extend(("", f"{heading}:"))
        lines.extend(f"- {_change_mod_label(record)}" for record in records)

    active_changes = changes.get("active_mod_id_changes", [])
    if isinstance(active_changes, list) and active_changes:
        has_content_change = True
        lines.extend(("", "Changed active mod versions:"))
        for record in active_changes:
            lines.append(
                f"- {record['display_name']}: {record['previous_mod_id']} -> "
                f"{record['mod_id']}"
            )

    settings_changes = changes.get("settings_changes", [])
    if isinstance(settings_changes, list) and settings_changes:
        lines.extend(("", "Changed pack settings:"))
        for record in settings_changes:
            if record["setting"] == "description":
                lines.append("- Workshop description updated")
            else:
                lines.append(
                    f"- {record['label']}: {record['previous']} -> {record['value']}"
                )

    builder_change = changes.get("builder_version_change")
    if isinstance(builder_change, dict):
        lines.extend(
            (
                "",
                "Builder updated: "
                f"{builder_change['previous']} -> {builder_change['value']}",
            )
        )

    if len(lines) == 1 or (
        not has_content_change
        and not settings_changes
        and not isinstance(builder_change, dict)
        and not changes.get("legacy_baseline")
    ):
        lines.extend(("", "Rebuilt with no bundled mod content changes detected."))
    return "\n".join(lines)


def _rewrite_known_id_checks(
    mods_root: Path,
    mapping: dict[str, str],
) -> list[dict[str, object]]:
    pattern = re.compile(
        r"(?P<prefix>(?P<receiver>getActivatedMods\(\)|[A-Za-z_]\w*)\s*:\s*contains\(\s*)"
        r"(?P<quote>['\"])(?P<slashes>\\*)(?P<identifier>[^'\"]+)(?P=quote)(?P<suffix>\s*\))"
    )
    records: list[dict[str, object]] = []
    for path in sorted(mods_root.rglob("*.lua")):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        aliases = set(
            re.findall(
                r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*getActivatedMods\(\)",
                text,
            )
        )
        replacement_count = 0

        def replace(
            match: re.Match[str],
            aliases_for_file: set[str] = aliases,
        ) -> str:
            nonlocal replacement_count
            receiver = match.group("receiver")
            if (
                receiver != "getActivatedMods()"
                and receiver not in aliases_for_file
            ):
                return match.group(0)
            identifier = match.group("identifier")
            packed = mapping.get(identifier)
            if packed is None:
                return match.group(0)
            replacement_count += 1
            return (
                match.group("prefix")
                + match.group("quote")
                + match.group("slashes")
                + packed
                + match.group("quote")
                + match.group("suffix")
            )

        rewritten = pattern.sub(replace, text)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            records.append(
                {
                    "strategy": "activated_mod_lookup",
                    "file": path.relative_to(mods_root).as_posix(),
                    "replacements": replacement_count,
                }
            )
    return records


_KNOWN_COMPATIBILITY_PATCHES = (
    {
        "name": "new-music-mod-file-reader-id",
        "source_folder": "Talis New Music",
        "relative_file": "42/media/lua/shared/core/NMTranslations.lua",
        "original_id": "NewMusic",
        "expected": 'local MOD_ID = "NewMusic"',
    },
    {
        "name": "new-music-target-mod-id",
        "source_folder": "Talis New Music",
        "relative_file": "42/media/lua/shared/loot/NMManagedSpawnCatalog.lua",
        "original_id": "NewMusic",
        "expected": 'return lower == "newmusic" or lower == "talisnewmusic"',
        "replacement": (
            'return lower == "newmusic" or lower == "talisnewmusic" '
            'or lower == "{packed_id_lower}"'
        ),
    },
    {
        "name": "new-music-child-requirement-id",
        "source_folder": "Talis New Music",
        "relative_file": "42/media/lua/shared/loot/NMManagedSpawnCatalog.lua",
        "original_id": "NewMusic",
        "expected": 'valueContainsRequiredMod(value, "NewMusic")',
    },
    {
        "name": "simple-status-load-config-id",
        "source_folder": "SimpleStatus",
        "relative_file": "media/lua/client/ss.main.lua",
        "original_id": "simpleStatus",
        "expected": 'utils.fn.loadConfig("simpleStatus", playerNum)',
    },
    {
        "name": "simple-status-bar-config-id",
        "source_folder": "SimpleStatus",
        "relative_file": "media/lua/client/ss.main.lua",
        "original_id": "simpleStatus",
        "expected": 'SSBar:new(playerObj, cfg, "simpleStatus")',
    },
    {
        "name": "simple-status-log-writer-id",
        "source_folder": "SimpleStatus",
        "relative_file": "media/lua/client/ss.utils.lua",
        "original_id": "simpleStatus",
        "expected": 'getModFileWriter("simpleStatus", "log.txt", true, true)',
    },
    {
        "name": "error-magnifier-self-id",
        "source_folder": "errorMagnifier",
        "relative_file": "media/lua/client/fingerPrint_Main.lua",
        "original_id": "errorMagnifier",
        "expected": 'modID~="errorMagnifier"',
    },
    {
        "name": "shelter-hold-base-self-import-42",
        "source_folder": "ShelterHold_Beehive",
        "relative_file": "42/media/scripts/generated/Hold_evolvedrecipes.txt",
        "expected": "    imports\n    {\n        Base\n    }\n\n",
        "replacement": "",
    },
    {
        "name": "shelter-hold-base-self-import-42-13",
        "source_folder": "ShelterHold_Beehive",
        "relative_file": "42.13/media/scripts/generated/Hold_evolvedrecipes.txt",
        "expected": "    imports\n    {\n        Base\n    }\n\n",
        "replacement": "",
    },
)


_KNOWN_LAYOUT_COMPATIBILITY_PATCHES = (
    {
        "name": "deceased-anthros-build-42-layout",
        "source_folder": "Anthro_Deceased",
        "kind": "legacy_root_to_version",
        "original_id": "DeceasedAnthros",
        "target_directory": "42",
        "required_entries": ("mod.info", "media", "Poster.png"),
        "poster_metadata": "poster.png",
        "poster_source": "Poster.png",
    },
    {
        "name": "new-music-server-loot-module",
        "source_folder": "Talis New Music",
        "kind": "relocate_file",
        "source_file": "42/media/lua/shared/loot/NMLootResolvedPools.lua",
        "destination_file": "42/media/lua/server/loot/NMLootResolvedPools.lua",
        "expected_prefix": (
            'require "loot/NMManagedSpawnCatalog"\n'
            'require "loot/NMLootPolicySnapshot"\n'
            'require "loot/NMLootRealizationAuthority"\n'
        ),
    },
)


def _known_layout_specs(source_folder: str) -> list[dict[str, object]]:
    return [
        specification
        for specification in _KNOWN_LAYOUT_COMPATIBILITY_PATCHES
        if specification["source_folder"] == source_folder
    ]


def _validate_known_layout_compatibility_patches(mod: DiscoveredMod) -> None:
    for specification in _known_layout_specs(mod.folder_name):
        name = str(specification["name"])
        kind = str(specification["kind"])
        if kind == "legacy_root_to_version":
            root_info = mod.mod_directory / "mod.info"
            original_id = str(specification["original_id"])
            if not root_info.is_file() or _read_id(root_info) != original_id:
                raise BuildError(
                    f"Compatibility patch {name} expected root Mod ID "
                    f"{original_id!r}: {root_info}"
                )
            target = mod.mod_directory / str(specification["target_directory"])
            if target.exists() or (mod.mod_directory / "common").exists():
                raise BuildError(
                    f"Compatibility patch {name} expected a root-only legacy layout: "
                    f"{mod.mod_directory}"
                )
            missing = [
                entry
                for entry in specification["required_entries"]
                if not (mod.mod_directory / str(entry)).exists()
            ]
            if missing:
                raise BuildError(
                    f"Compatibility patch {name} missing expected entries in "
                    f"{mod.mod_directory}: {', '.join(str(item) for item in missing)}"
                )
            poster_metadata = str(specification["poster_metadata"])
            metadata_text = root_info.read_text(encoding="utf-8-sig")
            if f"poster={poster_metadata}" not in metadata_text:
                raise BuildError(
                    f"Compatibility patch {name} expected poster={poster_metadata} in "
                    f"{root_info}"
                )
        elif kind == "relocate_file":
            source = mod.mod_directory / str(specification["source_file"])
            destination = mod.mod_directory / str(specification["destination_file"])
            if not source.is_file() or destination.exists():
                raise BuildError(
                    f"Compatibility patch {name} expected source {source} and no "
                    f"destination {destination}"
                )
            text = source.read_text(encoding="utf-8-sig")
            if not text.startswith(str(specification["expected_prefix"])):
                raise BuildError(
                    f"Compatibility patch {name} found unexpected content in {source}"
                )
        else:
            raise BuildError(f"Unknown compatibility layout patch kind: {kind}")


def _apply_known_layout_compatibility_patches(
    mods_root: Path,
    destination: Path,
    source_folder: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for specification in _known_layout_specs(source_folder):
        name = str(specification["name"])
        kind = str(specification["kind"])
        if kind == "legacy_root_to_version":
            target = destination / str(specification["target_directory"])
            entries = sorted(destination.iterdir(), key=lambda path: path.name.lower())
            target.mkdir()
            for entry in entries:
                shutil.move(str(entry), str(target / entry.name))
            poster_source = target / str(specification["poster_source"])
            poster_destination = target / str(specification["poster_metadata"])
            temporary_poster = target / ".pzmodpack-poster-case.tmp"
            shutil.move(str(poster_source), str(temporary_poster))
            shutil.move(str(temporary_poster), str(poster_destination))
            records.append(
                {
                    "name": name,
                    "strategy": "known_layout_context",
                    "file": destination.relative_to(mods_root).as_posix(),
                    "destination": target.relative_to(mods_root).as_posix(),
                    "entries_moved": len(entries),
                }
            )
        elif kind == "relocate_file":
            source = destination / str(specification["source_file"])
            relocated = destination / str(specification["destination_file"])
            relocated.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(relocated))
            records.append(
                {
                    "name": name,
                    "strategy": "known_file_relocation",
                    "file": relocated.relative_to(mods_root).as_posix(),
                    "source_file": source.relative_to(mods_root).as_posix(),
                    "files_moved": 1,
                }
            )
        else:
            raise BuildError(f"Unknown compatibility layout patch kind: {kind}")
    return records


def _mod_id_locations(mod: DiscoveredMod, mod_id: str) -> list[Path]:
    return [
        path.relative_to(mod.mod_directory)
        for path in mod.mod_directory.rglob("mod.info")
        if _read_id(path) == mod_id
    ]


def _is_build_42_location(path: Path) -> bool:
    return len(path.parts) > 1 and bool(
        re.fullmatch(r"42(?:\..+)?", path.parts[0])
    )


def _validate_active_mod_layouts(
    mods: list[DiscoveredMod],
    active_mod_ids: dict[str, str],
) -> None:
    selected_locations = {
        mod.folder_name: _mod_id_locations(
            mod,
            active_mod_ids.get(mod.folder_name, mod.mod_ids[-1]),
        )
        for mod in mods
    }
    targets_build_42 = any(
        any(_is_build_42_location(path) for path in locations)
        and Path("mod.info") not in locations
        for locations in selected_locations.values()
    )
    if not targets_build_42:
        return
    for mod in mods:
        locations = selected_locations[mod.folder_name]
        if any(
            path.parts[0] == "common" or _is_build_42_location(path)
            for path in locations
        ):
            continue
        has_legacy_layout_patch = any(
            specification["kind"] == "legacy_root_to_version"
            for specification in _known_layout_specs(mod.folder_name)
        )
        if has_legacy_layout_patch:
            continue
        active_id = active_mod_ids.get(mod.folder_name, mod.mod_ids[-1])
        raise BuildError(
            f"Active Mod ID {active_id!r} in folder {mod.folder_name!r} exists only "
            "in a legacy root mod.info, but the selected IDs indicate a Build 42 "
            "pack. Add a common/42 layout or a fail-closed compatibility rule."
        )


def _apply_known_compatibility_patches(
    mods_root: Path,
    prefix: str,
    mapping: dict[str, str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for specification in _KNOWN_COMPATIBILITY_PATCHES:
        source_folder = str(specification["source_folder"])
        packed_folder = mods_root / f"{prefix}{source_folder}"
        if not packed_folder.is_dir():
            continue
        original_value = specification.get("original_id")
        original_id = str(original_value) if original_value is not None else None
        packed_id = ""
        if original_id is not None:
            if original_id not in mapping:
                raise BuildError(
                    f"Compatibility patch {specification['name']} cannot resolve "
                    f"Mod ID {original_id!r}"
                )
            packed_id = mapping[original_id]
        relative_file = str(specification["relative_file"])
        path = packed_folder / relative_file
        if not path.is_file():
            raise BuildError(
                f"Compatibility patch {specification['name']} expected file: {path}"
            )
        text = path.read_text(encoding="utf-8-sig")
        expected = str(specification["expected"])
        if "replacement" in specification:
            replacement = (
                str(specification["replacement"])
                .replace("{packed_id_lower}", packed_id.lower())
                .replace("{packed_id}", packed_id)
            )
        elif original_id is not None:
            replacement = expected.replace(original_id, packed_id)
        else:
            raise BuildError(
                f"Compatibility patch {specification['name']} has no replacement"
            )
        count = text.count(expected)
        if count != 1:
            raise BuildError(
                f"Compatibility patch {specification['name']} expected 1 match in "
                f"{path}, found {count}"
            )
        path.write_text(text.replace(expected, replacement, 1), encoding="utf-8")
        records.append(
            {
                "name": specification["name"],
                "strategy": "known_file_context",
                "file": path.relative_to(mods_root).as_posix(),
                "replacements": 1,
            }
        )
    return records


def _hardcoded_reference_details(
    mods_root: Path,
    mapping: dict[str, str],
) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    text_suffixes = {".lua", ".txt", ".ini", ".json", ".xml"}
    for path in sorted(item for item in mods_root.rglob("*") if item.is_file()):
        if path.name == "mod.info" or path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        aliases = set(
            re.findall(
                r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*getActivatedMods\(\)",
                text,
            )
        )
        runtime_id_variables = set(
            re.findall(
                r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*[^\n]*"
                r"(?::\s*get(?:Id|ID|ModID)\s*\(\)|getModInfoByID\s*\()",
                text,
            )
        )
        categorized: dict[tuple[str, str], set[int]] = {}
        for original in mapping:
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(original)}(?![A-Za-z0-9_])"
            )
            for match in pattern.finditer(text):
                category = _reference_occurrence_category(
                    text,
                    match,
                    aliases,
                    runtime_id_variables,
                )
                line_number = text.count("\n", 0, match.start()) + 1
                categorized.setdefault((category, original), set()).add(line_number)
        relative = path.relative_to(mods_root).as_posix()
        for (category, original), line_numbers in categorized.items():
            ordered_lines = sorted(line_numbers)
            line_summary = ", ".join(str(item) for item in ordered_lines)
            message = (
                f"{relative}:{line_summary} contains original Mod ID reference: "
                f"{original}"
            )
            details.append(
                {
                    "category": category,
                    "file": relative,
                    "line_numbers": ordered_lines,
                    "original_ids": [original],
                    "message": message,
                }
            )
    return details


def _reference_occurrence_category(
    text: str,
    match: re.Match[str],
    activated_mod_aliases: set[str],
    runtime_id_variables: set[str],
) -> str:
    before = text[max(0, match.start() - 500) : match.start()]
    after = text[match.end() : min(len(text), match.end() + 200)]
    quote = re.search(r"(?P<quote>['\"])\\*$", before)
    quoted = quote is not None and after.startswith(quote.group("quote"))
    before_quote = before[: quote.start()] if quote is not None else before
    after_quote = after[1:] if quoted else after

    if quoted and re.search(
        r"\bgetModFile(?:Reader|Writer)\s*\(\s*$",
        before_quote,
    ):
        return "mod_file_access"

    receivers = {"getActivatedMods()", *activated_mod_aliases}
    receiver_pattern = "|".join(
        re.escape(receiver) for receiver in sorted(receivers, key=len, reverse=True)
    )
    if quoted and (
        re.search(r"\bgetModInfoByID\s*\(\s*$", before_quote)
        or re.search(
            rf"(?:{receiver_pattern})\s*:\s*contains\s*\(\s*$",
            before_quote,
        )
    ):
        return "runtime_mod_lookup"

    if quoted and runtime_id_variables:
        variable_pattern = "|".join(
            re.escape(variable)
            for variable in sorted(runtime_id_variables, key=len, reverse=True)
        )
        if re.search(
            rf"\b(?:{variable_pattern})\s*(?:==|~=)\s*$",
            before_quote,
        ) or re.match(
            rf"\s*(?:==|~=)\s*(?:{variable_pattern})\b",
            after_quote,
        ):
            return "runtime_mod_lookup"

    if (
        re.search(r"(?:\blocal|\bfunction)\s+$", before)
        or before.endswith((".", ":"))
        or after.startswith((".", ":"))
        or (
            quoted
            and re.search(
                r"\bModOptions(?::|\.)\s*(?:create|getOptions)\s*\(\s*$",
                before_quote,
            )
        )
    ):
        return "content_namespace"

    return "ambiguous_string"


def _mod_references(mod: DiscoveredMod, field: str) -> set[str]:
    references: set[str] = set()
    for mod_info in mod.mod_directory.rglob("mod.info"):
        references.update(_mod_info_references(mod_info, field))
    return references


def _mod_info_references(mod_info: Path, field: str) -> set[str]:
    references: set[str] = set()
    for raw_line in mod_info.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip().lower() == field.lower():
            references.update(
                item.strip().lstrip("\\")
                for item in re.split(r"[;,]", value)
                if item.strip().lstrip("\\")
            )
    return references


def find_bundled_conflicts(
    mods: Iterable[DiscoveredMod],
) -> tuple[BundledModConflict, ...]:
    """Return unique metadata conflicts between discovered bundled mod folders."""
    mod_list = list(mods)
    owners: dict[str, list[DiscoveredMod]] = {}
    for mod in mod_list:
        for mod_id in mod.mod_ids:
            owners.setdefault(mod_id, []).append(mod)

    conflicts: list[BundledModConflict] = []
    seen: set[tuple[tuple[str, str, str], tuple[str, str, str]]] = set()
    for mod in mod_list:
        for mod_info in sorted(mod.mod_directory.rglob("mod.info")):
            declaring_id = _read_id(mod_info)
            if declaring_id is None:
                continue
            for incompatible_id in sorted(
                _mod_info_references(mod_info, "incompatible")
            ):
                for incompatible_mod in owners.get(incompatible_id, []):
                    if incompatible_mod == mod:
                        # One source folder is the selection unit. Its versioned
                        # directories are resolved by Project Zomboid at runtime.
                        continue
                    declaring_endpoint = (
                        str(mod.source_root),
                        mod.folder_name,
                        declaring_id,
                    )
                    incompatible_endpoint = (
                        str(incompatible_mod.source_root),
                        incompatible_mod.folder_name,
                        incompatible_id,
                    )
                    pair = tuple(sorted((declaring_endpoint, incompatible_endpoint)))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    conflicts.append(
                        BundledModConflict(
                            declaring_mod=mod,
                            declaring_mod_id=declaring_id,
                            incompatible_mod=incompatible_mod,
                            incompatible_mod_id=incompatible_id,
                        )
                    )
    return tuple(conflicts)


def find_mod_requirements(
    mods: Iterable[DiscoveredMod],
) -> tuple[BundledModRequirement, ...]:
    """Return exact ``require=`` relationships declared by bundled mod metadata."""
    mod_list = list(mods)
    owners: dict[str, list[DiscoveredMod]] = {}
    for mod in mod_list:
        for mod_id in mod.mod_ids:
            owners.setdefault(mod_id, []).append(mod)

    requirements: list[BundledModRequirement] = []
    seen: set[tuple[str, str, str, str]] = set()
    for mod in mod_list:
        for mod_info in sorted(mod.mod_directory.rglob("mod.info")):
            declaring_id = _read_id(mod_info)
            if declaring_id is None:
                continue
            for required_id in sorted(_mod_info_references(mod_info, "require")):
                # A folder is the selection unit, so a requirement provided by
                # another version directory in the same folder is already present.
                if required_id in mod.mod_ids:
                    continue
                key = (
                    str(mod.source_root),
                    mod.folder_name,
                    declaring_id,
                    required_id,
                )
                if key in seen:
                    continue
                seen.add(key)
                requirements.append(
                    BundledModRequirement(
                        declaring_mod=mod,
                        declaring_mod_id=declaring_id,
                        required_mod_id=required_id,
                        providers=tuple(
                            provider
                            for provider in owners.get(required_id, [])
                            if provider != mod
                        ),
                    )
                )
    return tuple(requirements)


def _snapshot_timestamp(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _snapshot_revision_key(mod: DiscoveredMod) -> str:
    if mod.snapshot_sha256:
        return mod.snapshot_sha256
    return f"path:{mod.source_root.as_posix()}"


def _snapshot_revision_sort_key(
    revision: WorkshopSnapshotRevision,
) -> tuple[float, float, int, str]:
    updated = _snapshot_timestamp(revision.workshop_updated_at_utc)
    captured = _snapshot_timestamp(revision.snapshot_created_at_utc)
    effective_revision_time = updated if updated != float("-inf") else captured
    return (
        effective_revision_time,
        captured,
        revision.source_order,
        revision.revision_key,
    )


def workshop_snapshot_groups(
    mods: Iterable[DiscoveredMod],
) -> dict[str, tuple[WorkshopSnapshotRevision, ...]]:
    """Group all discovered mod folders into ordered revisions per Workshop item."""
    grouped: dict[tuple[str, Path], list[DiscoveredMod]] = {}
    source_order: dict[tuple[str, Path], int] = {}
    for index, mod in enumerate(mods):
        if mod.workshop_id is None:
            continue
        key = (mod.workshop_id, mod.source_root)
        grouped.setdefault(key, []).append(mod)
        source_order.setdefault(key, index)

    revisions: dict[str, list[WorkshopSnapshotRevision]] = {}
    for (workshop_id, source_root), revision_mods in grouped.items():
        representative = revision_mods[0]
        revisions.setdefault(workshop_id, []).append(
            WorkshopSnapshotRevision(
                workshop_id=workshop_id,
                revision_key=_snapshot_revision_key(representative),
                source_root=source_root,
                sha256=representative.snapshot_sha256,
                snapshot_created_at_utc=representative.snapshot_created_at_utc,
                workshop_updated_at_utc=representative.workshop_updated_at_utc,
                workshop_manifest_id=representative.workshop_manifest_id,
                mods=tuple(revision_mods),
                source_order=source_order[(workshop_id, source_root)],
            )
        )

    return {
        workshop_id: tuple(
            sorted(
                item_revisions,
                key=_snapshot_revision_sort_key,
                reverse=True,
            )
        )
        for workshop_id, item_revisions in sorted(revisions.items())
    }


def resolve_workshop_snapshots(
    mods: Iterable[DiscoveredMod],
    selections: dict[str, str] | None = None,
) -> tuple[list[DiscoveredMod], dict[str, str]]:
    """Choose exactly one revision per Workshop item, defaulting to the latest."""
    mod_list = list(mods)
    requested = selections or {}
    groups = workshop_snapshot_groups(mod_list)
    unknown_workshop_ids = sorted(set(requested) - set(groups))
    if unknown_workshop_ids:
        raise BuildError(
            "Snapshot selection references Workshop item(s) not present in the "
            f"configured sources: {', '.join(unknown_workshop_ids)}"
        )
    chosen_sources: dict[str, Path] = {}
    effective: dict[str, str] = {}
    for workshop_id, revisions in groups.items():
        selected_key = requested.get(workshop_id)
        if selected_key is None:
            selected = revisions[0]
        else:
            selected = next(
                (
                    revision
                    for revision in revisions
                    if revision.revision_key == selected_key
                    or revision.sha256 == selected_key
                ),
                None,
            )
            if selected is None:
                raise BuildError(
                    f"Selected snapshot {selected_key!r} for Workshop item "
                    f"{workshop_id} is not in the configured sources"
                )
        chosen_sources[workshop_id] = selected.source_root
        effective[workshop_id] = selected.revision_key

    selected_mods = [
        mod
        for mod in mod_list
        if mod.workshop_id is None
        or mod.source_root == chosen_sources.get(mod.workshop_id)
    ]
    return selected_mods, effective


def workshop_snapshot_manifest(
    mods: Iterable[DiscoveredMod],
    selections: dict[str, str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for workshop_id, revisions in workshop_snapshot_groups(mods).items():
        selected_key = selections[workshop_id]
        selected = next(
            revision for revision in revisions if revision.revision_key == selected_key
        )
        records.append(
            {
                "workshop_id": workshop_id,
                "selected_revision": selected.revision_key,
                "selected_source": str(selected.source_root),
                "selected_sha256": selected.sha256,
                "snapshot_created_at_utc": selected.snapshot_created_at_utc,
                "workshop_updated_at_utc": selected.workshop_updated_at_utc,
                "workshop_manifest_id": selected.workshop_manifest_id,
                "selected_is_latest": selected is revisions[0],
                "available_revisions": [
                    {
                        "revision": revision.revision_key,
                        "source": str(revision.source_root),
                        "sha256": revision.sha256,
                        "snapshot_created_at_utc": revision.snapshot_created_at_utc,
                        "workshop_updated_at_utc": revision.workshop_updated_at_utc,
                        "workshop_manifest_id": revision.workshop_manifest_id,
                        "is_latest": revision is revisions[0],
                    }
                    for revision in revisions
                ],
            }
        )
    return records


def select_mods(
    mods: Iterable[DiscoveredMod],
    included_mod_ids: tuple[str, ...] | None,
) -> list[DiscoveredMod]:
    """Select whole mod folders by any Mod ID declared inside each folder."""
    mod_list = list(mods)
    if included_mod_ids is None:
        return mod_list
    included = set(included_mod_ids)
    return [mod for mod in mod_list if included.intersection(mod.mod_ids)]


def _mod_requirements(mod: DiscoveredMod) -> set[str]:
    return _mod_references(mod, "require")


def _active_mod_id(
    mod: DiscoveredMod,
    active_mod_ids: dict[str, str],
) -> str:
    selected = active_mod_ids.get(mod.folder_name, mod.mod_ids[-1])
    return selected if selected in mod.mod_ids else mod.mod_ids[-1]


def _dependency_order(
    mods: list[DiscoveredMod],
    active_mod_ids: dict[str, str],
) -> list[DiscoveredMod]:
    by_name = {mod.folder_name: mod for mod in mods}
    dependencies = {mod.folder_name: set() for mod in mods}
    for requirement in find_mod_requirements(mods):
        mod = requirement.declaring_mod
        if requirement.declaring_mod_id != _active_mod_id(mod, active_mod_ids):
            continue
        dependencies[mod.folder_name].update(
            provider.folder_name
            for provider in requirement.providers
            if provider.folder_name != mod.folder_name
        )
    ordered: list[DiscoveredMod] = []
    remaining = list(mods)
    while remaining:
        ready = [
            mod
            for mod in remaining
            if dependencies[mod.folder_name].issubset(
                {item.folder_name for item in ordered}
            )
        ]
        if not ready:
            cycle = ", ".join(mod.folder_name for mod in remaining)
            raise BuildError(f"Dependency cycle among bundled mods: {cycle}")
        for mod in ready:
            ordered.append(by_name[mod.folder_name])
            remaining.remove(mod)
    return ordered


def _emit_progress(
    callback: Callable[[int, int, str], None] | None,
    current: int,
    message: str,
) -> None:
    if callback is not None:
        callback(current, 100, message)


def _build_modpack(
    config: BuildConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> BuildReport:
    _emit_progress(progress, 0, "Starting build")
    prefix = _safe_namespace(config.namespace)
    if config.visibility not in {0, 1, 2, 3}:
        raise BuildError("Workshop visibility must be 0, 1, 2, or 3")
    _emit_progress(progress, 5, "Discovering mod sources")
    all_discovered_mods = discover_mods(config.sources)
    if not all_discovered_mods:
        raise BuildError("No mods were discovered in the selected sources")
    discovered_mods, effective_snapshot_selections = resolve_workshop_snapshots(
        all_discovered_mods,
        config.snapshot_selections,
    )
    if config.included_mod_ids is not None:
        available_mod_ids = {
            mod_id for mod in discovered_mods for mod_id in mod.mod_ids
        }
        missing_selected_ids = sorted(
            set(config.included_mod_ids) - available_mod_ids
        )
        if missing_selected_ids:
            raise BuildError(
                "Selected snapshot revisions no longer provide bundled Mod ID(s): "
                f"{', '.join(missing_selected_ids)}. Reopen bundled mod selection."
            )
    mods = select_mods(discovered_mods, config.included_mod_ids)
    if not mods:
        raise BuildError("No bundled mod folders are selected for this build")
    mods_by_folder = {mod.folder_name: mod for mod in mods}
    discovered_by_folder = {mod.folder_name: mod for mod in discovered_mods}
    for folder, active_id in config.active_mod_ids.items():
        if folder not in mods_by_folder:
            if folder in discovered_by_folder:
                raise BuildError(
                    f"Active Mod ID override references excluded folder: {folder}"
                )
            raise BuildError(f"Active Mod ID override references unknown folder: {folder}")
        if active_id not in mods_by_folder[folder].mod_ids:
            raise BuildError(
                f"Active Mod ID {active_id!r} does not belong to folder {folder!r}"
            )
    _emit_progress(progress, 10, f"Validating {len(mods)} mod folders")
    issues = validate_mod_selection(
        discovered_mods,
        mods,
        config.active_mod_ids,
    )
    errors = [issue.message for issue in issues if issue.severity == "error"]
    if errors:
        raise BuildError("\n".join(errors))
    validation_warning_details = [
        {"category": "metadata", "message": issue.message}
        for issue in issues
        if issue.severity == "warning"
    ]
    mods = _dependency_order(mods, config.active_mod_ids)
    mods_by_folder = {mod.folder_name: mod for mod in mods}
    for mod in mods:
        _validate_known_layout_compatibility_patches(mod)
    _validate_active_mod_layouts(mods, config.active_mod_ids)
    mapping = {
        mod_id: prefix + mod_id
        for mod in mods
        for mod_id in mod.mod_ids
    }
    reference_mapping = dict(mapping)
    for mod in mods:
        active_source_id = config.active_mod_ids.get(mod.folder_name)
        if active_source_id is None:
            continue
        active_packed_id = mapping[active_source_id]
        for mod_id in mod.mod_ids:
            reference_mapping[mod_id] = active_packed_id
    final_output = Path(config.output).resolve()
    previous_manifest = _read_existing_build_manifest(final_output)
    previous_version = (
        _format_pack_version(
            _parse_pack_version(
                previous_manifest.get("pack_version"),
                legacy_default=True,
            )
        )
        if previous_manifest is not None
        else None
    )
    pack_version = _next_pack_version(previous_version, config.version_bump)
    _validate_history_destination(final_output, previous_version)
    preview = Path(config.preview).resolve() if config.preview is not None else None
    if preview is not None and not preview.is_file():
        raise BuildError(f"Preview image not found: {preview}")
    output = final_output.with_name(f".{final_output.name}.pzmodpack-building")
    _emit_progress(progress, 15, f"Preparing modpack v{pack_version}")
    _prepare_output(output)
    if preview is not None:
        shutil.copy2(preview, output / "preview.png")
    mods_root = output / "Contents" / "mods"
    mods_root.mkdir(parents=True)
    manifest_mods: list[dict[str, object]] = []
    compatibility_patches: list[dict[str, object]] = []
    for index, mod in enumerate(mods, start=1):
        copy_progress = 15 + int((index - 1) * 60 / len(mods))
        _emit_progress(
            progress,
            copy_progress,
            f"Copying {index}/{len(mods)}: {mod.folder_name}",
        )
        destination = mods_root / f"{prefix}{mod.folder_name}"
        shutil.copytree(mod.mod_directory, destination)
        compatibility_patches.extend(
            _apply_known_layout_compatibility_patches(
                mods_root,
                destination,
                mod.folder_name,
            )
        )
        for mod_info in destination.rglob("mod.info"):
            original = mod_info.read_text(encoding="utf-8-sig")
            mod_info.write_text(
                rewrite_mod_info(original, mapping, reference_mapping),
                encoding="utf-8",
            )
        manifest_mods.append(
            {
                "source": str(mod.source_root),
                "source_folder": mod.folder_name,
                "display_name": mod.display_name or mod.folder_name,
                "packed_folder": destination.name,
                "source_workshop_id": mod.workshop_id,
                "source_snapshot_sha256": mod.snapshot_sha256,
                "snapshot_created_at_utc": mod.snapshot_created_at_utc,
                "workshop_updated_at_utc": mod.workshop_updated_at_utc,
                "workshop_manifest_id": mod.workshop_manifest_id,
                "original_mod_ids": list(mod.mod_ids),
                "packed_mod_ids": [mapping[item] for item in mod.mod_ids],
                "active_source_mod_id": config.active_mod_ids.get(
                    mod.folder_name,
                    mod.mod_ids[-1],
                ),
                "active_packed_mod_id": mapping[
                    config.active_mod_ids.get(mod.folder_name, mod.mod_ids[-1])
                ],
                "sha256": _tree_hash(mod.mod_directory),
            }
        )
    _emit_progress(progress, 78, "Rewriting runtime Mod ID lookups")
    compatibility_patches.extend(
        _rewrite_known_id_checks(mods_root, reference_mapping)
    )
    compatibility_patches.extend(
        _apply_known_compatibility_patches(mods_root, prefix, reference_mapping)
    )
    active_ids = [
        mapping[config.active_mod_ids.get(mod.folder_name, mod.mod_ids[-1])]
        for mod in mods
    ]
    manifest_requirements = [
        {
            "source_folder": requirement.declaring_mod.folder_name,
            "declaring_mod_id": requirement.declaring_mod_id,
            "packed_declaring_mod_id": mapping[requirement.declaring_mod_id],
            "required_mod_id": requirement.required_mod_id,
            "packed_required_mod_id": reference_mapping[requirement.required_mod_id],
            "provider_folders": [
                provider.folder_name for provider in requirement.providers
            ],
        }
        for requirement in find_mod_requirements(mods)
        if requirement.declaring_mod_id
        == _active_mod_id(requirement.declaring_mod, config.active_mod_ids)
    ]
    _emit_progress(progress, 86, "Auditing unresolved Mod ID references")
    hardcoded_warning_details = _hardcoded_reference_details(mods_root, mapping)
    warning_details = validation_warning_details + hardcoded_warning_details
    warnings = [str(detail["message"]) for detail in warning_details]
    manifest: dict[str, object] = {
        "format_version": 2,
        "pack_version": pack_version,
        "previous_pack_version": previous_version,
        "version_bump": config.version_bump.strip().lower(),
        "builder_version": __version__,
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "history_directory": _history_root(final_output).name,
        "name": config.name,
        "description": config.description,
        "namespace": prefix.rstrip("_"),
        "workshop_id": config.workshop_id,
        "visibility": config.visibility,
        "preview": str(Path(config.preview).resolve()) if config.preview else None,
        "mapping": mapping,
        "snapshot_selection": workshop_snapshot_manifest(
            all_discovered_mods,
            effective_snapshot_selections,
        ),
        "mod_selection": {
            "included_mod_ids": sorted(mapping),
            "excluded_mods": [
                {
                    "source_folder": mod.folder_name,
                    "source_workshop_id": mod.workshop_id,
                    "mod_ids": list(mod.mod_ids),
                }
                for mod in discovered_mods
                if mod not in mods
            ],
        },
        "active_mod_ids": active_ids,
        "active_mod_id_overrides": config.active_mod_ids,
        "requirements": manifest_requirements,
        "compatibility_patches": compatibility_patches,
        "mods": manifest_mods,
        "warning_details": warning_details,
        "warnings": warnings,
    }
    changes = _detect_build_changes(previous_manifest, manifest)
    change_note = _build_change_note(config.name, pack_version, changes)
    manifest["changes"] = changes
    manifest["generated_change_note"] = change_note
    _emit_progress(progress, 94, "Writing manifest and server configuration")
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "amp-config.txt").write_text(
        f"WorkshopItems={config.workshop_id};\nMods={';'.join(active_ids)};\n",
        encoding="utf-8",
    )
    (output / "workshop.txt").write_text(
        "\n".join(
            (
                "version=1",
                f"id={config.workshop_id}",
                f"title={config.name}",
                f"description={config.description}",
                "tags=Build 42;Multiplayer",
                f"visibility={config.visibility}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (output / "change-notes.txt").write_text(change_note + "\n", encoding="utf-8")
    (output / ".pzmodpack-output").write_text(
        f"generated v{pack_version}\n",
        encoding="utf-8",
    )
    _emit_progress(progress, 98, f"Saving modpack v{pack_version}")
    archived_output = _commit_versioned_output(
        final_output,
        output,
        previous_version,
    )
    _emit_progress(progress, 100, "Build complete")
    return BuildReport(
        output=final_output,
        mod_count=len(mods),
        mapping=mapping,
        warnings=tuple(warnings),
        warning_details=tuple(warning_details),
        pack_version=pack_version,
        previous_pack_version=previous_version,
        change_note=change_note,
        archived_output=archived_output,
    )


def build_modpack(
    config: BuildConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> BuildReport:
    """Build a versioned pack while preserving the last successful output on failure."""
    final_output = Path(config.output).resolve()
    staged_output = final_output.with_name(
        f".{final_output.name}.pzmodpack-building"
    )
    try:
        return _build_modpack(config, progress)
    except Exception:
        if (
            final_output.exists()
            and staged_output.is_dir()
            and (staged_output / ".pzmodpack-output").is_file()
        ):
            shutil.rmtree(staged_output)
        raise


def _read_id(mod_info: Path) -> str | None:
    return _read_mod_info_value(mod_info, "id", strip_leading_slash=True)


def _read_mod_info_value(
    mod_info: Path,
    field: str,
    *,
    strip_leading_slash: bool = False,
) -> str | None:
    for raw_line in mod_info.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip().lower() == field.lower():
            cleaned = value.strip()
            if strip_leading_slash:
                cleaned = cleaned.lstrip("\\")
            return cleaned or None
    return None


def _snapshot_metadata(path: Path) -> dict[str, object]:
    for candidate in (path, *path.parents):
        metadata = candidate / "snapshot.json"
        if metadata.is_file():
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if not payload.get("snapshot_created_at_utc") and not payload.get(
                "created_at_utc"
            ):
                try:
                    payload["snapshot_created_at_utc"] = datetime.fromtimestamp(
                        metadata.stat().st_mtime,
                        UTC,
                    ).isoformat(timespec="seconds")
                except OSError:
                    pass
            return payload
    return {}


def _workshop_id(path: Path, snapshot_metadata: dict[str, object]) -> str | None:
    workshop_id = str(snapshot_metadata.get("workshop_id") or "")
    if workshop_id.isdigit():
        return workshop_id
    for candidate in (path, *path.parents):
        if (
            (candidate / "snapshot.json").is_file()
            and candidate.parent.name.isdigit()
        ):
            return candidate.parent.name
        if candidate.name.isdigit() and candidate.parent.name == "108600":
            return candidate.name
    return None


_REFERENCE_KEYS = {"require", "loadmodafter", "loadmodbefore", "incompatible"}


def _rewrite_reference(value: str, mapping: dict[str, str]) -> str:
    pieces = re.split(r"([;,])", value)
    rewritten: list[str] = []
    for piece in pieces:
        if piece in {";", ","}:
            rewritten.append(piece)
            continue
        stripped = piece.strip()
        if not stripped:
            rewritten.append(piece)
            continue
        leading = piece[: len(piece) - len(piece.lstrip())]
        trailing = piece[len(piece.rstrip()) :]
        prefix = "\\" if stripped.startswith("\\") else ""
        identifier = stripped.lstrip("\\")
        rewritten.append(
            leading + prefix + mapping.get(identifier, identifier) + trailing
        )
    return "".join(rewritten)


def rewrite_mod_info(
    text: str,
    mapping: dict[str, str],
    reference_mapping: dict[str, str] | None = None,
) -> str:
    references = reference_mapping or mapping
    lines = text.splitlines()
    original_id: str | None = None
    incompatible_index: int | None = None
    for index, raw_line in enumerate(lines):
        key, separator, value = raw_line.partition("=")
        if not separator:
            continue
        lowered = key.strip().lower()
        if lowered == "id":
            original_id = value.strip().lstrip("\\")
            lines[index] = f"{key}={mapping.get(original_id, original_id)}"
        elif lowered in _REFERENCE_KEYS:
            lines[index] = f"{key}={_rewrite_reference(value, references)}"
            if lowered == "incompatible":
                incompatible_index = index
    if original_id and original_id in mapping:
        if incompatible_index is None:
            lines.append(f"incompatible={original_id}")
        else:
            key, _, value = lines[incompatible_index].partition("=")
            values = [item for item in value.split(";") if item]
            if original_id not in (item.lstrip("\\") for item in values):
                values.append(original_id)
            lines[incompatible_index] = f"{key}={';'.join(values)}"
    return "\n".join(lines) + ("\n" if text.endswith(("\n", "\r")) else "")


def discover_mods(source_roots: Iterable[Path]) -> list[DiscoveredMod]:
    discovered: list[DiscoveredMod] = []
    seen_sources: set[Path] = set()
    for source in (Path(item).resolve() for item in source_roots):
        if source in seen_sources:
            continue
        seen_sources.add(source)
        snapshot_metadata = _snapshot_metadata(source)
        workshop_id = _workshop_id(source, snapshot_metadata)
        mods_root = source / "mods" if (source / "mods").is_dir() else source
        candidates = [path for path in sorted(mods_root.iterdir()) if path.is_dir()]
        if (mods_root / "mod.info").is_file():
            candidates = [mods_root]
        for mod_directory in candidates:
            mod_info_files = sorted(
                mod_directory.rglob("mod.info"),
                key=lambda path: (
                    len(path.relative_to(mod_directory).parts),
                    str(path),
                ),
            )
            ids = tuple(
                dict.fromkeys(
                    mod_id
                    for info in mod_info_files
                    if (mod_id := _read_id(info))
                )
            )
            if ids:
                display_info = mod_info_files[-1]
                discovered.append(
                    DiscoveredMod(
                        source_root=source,
                        mod_directory=mod_directory,
                        folder_name=mod_directory.name,
                        mod_ids=ids,
                        workshop_id=workshop_id,
                        display_name=(
                            _read_mod_info_value(display_info, "name")
                            or mod_directory.name
                        ),
                        description=(
                            _read_mod_info_value(display_info, "description") or ""
                        ),
                        snapshot_sha256=(
                            str(snapshot_metadata.get("sha256"))
                            if snapshot_metadata.get("sha256")
                            else None
                        ),
                        snapshot_created_at_utc=(
                            str(
                                snapshot_metadata.get("snapshot_created_at_utc")
                                or snapshot_metadata.get("created_at_utc")
                            )
                            if snapshot_metadata.get("snapshot_created_at_utc")
                            or snapshot_metadata.get("created_at_utc")
                            else None
                        ),
                        workshop_updated_at_utc=(
                            str(snapshot_metadata.get("workshop_updated_at_utc"))
                            if snapshot_metadata.get("workshop_updated_at_utc")
                            else None
                        ),
                        workshop_manifest_id=(
                            str(snapshot_metadata.get("workshop_manifest_id"))
                            if snapshot_metadata.get("workshop_manifest_id")
                            else None
                        ),
                    )
                )
    return discovered
