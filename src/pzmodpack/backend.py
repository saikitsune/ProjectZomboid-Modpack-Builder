from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredMod:
    source_root: Path
    mod_directory: Path
    folder_name: str
    mod_ids: tuple[str, ...]
    workshop_id: str | None


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str


def validate_mods(mods: Iterable[DiscoveredMod]) -> list[ValidationIssue]:
    mod_list = list(mods)
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
    bundled_ids = set(owners)
    for mod in mod_list:
        for required in sorted(_mod_requirements(mod) - bundled_ids):
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="external_dependency",
                    message=(
                        f"{mod.folder_name} requires {required!r}, which is not included "
                        "in this bundle"
                    ),
                )
            )
    seen_conflicts: set[tuple[str, str]] = set()
    for mod in mod_list:
        declaring_id = mod.mod_ids[-1]
        for incompatible in sorted(_mod_references(mod, "incompatible") & bundled_ids):
            pair = tuple(sorted((declaring_id, incompatible)))
            if pair in seen_conflicts:
                continue
            seen_conflicts.add(pair)
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="bundled_incompatibility",
                    message=(
                        f"Bundled Mod ID {declaring_id!r} declares bundled Mod ID "
                        f"{incompatible!r} incompatible"
                    ),
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


@dataclass(frozen=True)
class BuildReport:
    output: Path
    mod_count: int
    mapping: dict[str, str]
    warnings: tuple[str, ...]
    warning_details: tuple[dict[str, object], ...] = ()


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
        original_id = str(specification["original_id"])
        if original_id not in mapping:
            raise BuildError(
                f"Compatibility patch {specification['name']} cannot resolve Mod ID "
                f"{original_id!r}"
            )
        relative_file = str(specification["relative_file"])
        path = packed_folder / relative_file
        if not path.is_file():
            raise BuildError(
                f"Compatibility patch {specification['name']} expected file: {path}"
            )
        text = path.read_text(encoding="utf-8-sig")
        expected = str(specification["expected"])
        replacement = expected.replace(original_id, mapping[original_id])
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
        found = [
            original
            for original in mapping
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(original)}(?![A-Za-z0-9_])",
                text,
            )
        ]
        if found:
            relative = path.relative_to(mods_root).as_posix()
            categorized: dict[str, list[str]] = {}
            for original in found:
                quoted_id = rf"['\"]\\*{re.escape(original)}['\"]"
                if re.search(
                    rf"getModFileReader\s*\(\s*{quoted_id}",
                    text,
                ):
                    category = "mod_file_access"
                elif re.search(
                    rf"(?:getModInfoByID\s*\(|(?:getActivatedMods\(\)|[A-Za-z_]\w*)"
                    rf"\s*:\s*contains\s*\()\s*{quoted_id}",
                    text,
                ):
                    category = "runtime_mod_lookup"
                elif re.search(
                    rf"(?:local\s+{re.escape(original)}\b|function\s+{re.escape(original)}[.:]"
                    rf"|\b{re.escape(original)}[.:]|ModOptions:create\s*\(\s*{quoted_id})",
                    text,
                ):
                    category = "content_namespace"
                else:
                    category = "ambiguous_string"
                categorized.setdefault(category, []).append(original)
            for category, identifiers in categorized.items():
                message = (
                    f"{relative} contains original Mod ID reference(s): "
                    f"{', '.join(identifiers)}"
                )
                details.append(
                    {
                        "category": category,
                        "file": relative,
                        "original_ids": identifiers,
                        "message": message,
                    }
                )
    return details


def _mod_references(mod: DiscoveredMod, field: str) -> set[str]:
    references: set[str] = set()
    for mod_info in mod.mod_directory.rglob("mod.info"):
        for raw_line in mod_info.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = raw_line.partition("=")
            if separator and key.strip().lower() == field.lower():
                references.update(
                    item.strip().lstrip("\\")
                    for item in re.split(r"[;,]", value)
                    if item.strip().lstrip("\\")
                )
    return references


def _mod_requirements(mod: DiscoveredMod) -> set[str]:
    return _mod_references(mod, "require")


def _dependency_order(mods: list[DiscoveredMod]) -> list[DiscoveredMod]:
    owner = {mod_id: mod.folder_name for mod in mods for mod_id in mod.mod_ids}
    by_name = {mod.folder_name: mod for mod in mods}
    dependencies = {
        mod.folder_name: {
            owner[required]
            for required in _mod_requirements(mod)
            if required in owner and owner[required] != mod.folder_name
        }
        for mod in mods
    }
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


def build_modpack(
    config: BuildConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> BuildReport:
    _emit_progress(progress, 0, "Starting build")
    prefix = _safe_namespace(config.namespace)
    if config.visibility not in {0, 1, 2, 3}:
        raise BuildError("Workshop visibility must be 0, 1, 2, or 3")
    _emit_progress(progress, 5, "Discovering mod sources")
    mods = discover_mods(config.sources)
    if not mods:
        raise BuildError("No mods were discovered in the selected sources")
    _emit_progress(progress, 10, f"Validating {len(mods)} mod folders")
    issues = validate_mods(mods)
    errors = [issue.message for issue in issues if issue.severity == "error"]
    if errors:
        raise BuildError("\n".join(errors))
    validation_warning_details = [
        {"category": "metadata", "message": issue.message}
        for issue in issues
        if issue.severity == "warning"
    ]
    mods = _dependency_order(mods)
    mods_by_folder = {mod.folder_name: mod for mod in mods}
    for folder, active_id in config.active_mod_ids.items():
        if folder not in mods_by_folder:
            raise BuildError(f"Active Mod ID override references unknown folder: {folder}")
        if active_id not in mods_by_folder[folder].mod_ids:
            raise BuildError(
                f"Active Mod ID {active_id!r} does not belong to folder {folder!r}"
            )
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
    output = Path(config.output).resolve()
    preview = Path(config.preview).resolve() if config.preview is not None else None
    if preview is not None and not preview.is_file():
        raise BuildError(f"Preview image not found: {preview}")
    _emit_progress(progress, 15, "Preparing output directory")
    _prepare_output(output)
    if preview is not None:
        shutil.copy2(preview, output / "preview.png")
    mods_root = output / "Contents" / "mods"
    mods_root.mkdir(parents=True)
    manifest_mods: list[dict[str, object]] = []
    for index, mod in enumerate(mods, start=1):
        copy_progress = 15 + int((index - 1) * 60 / len(mods))
        _emit_progress(
            progress,
            copy_progress,
            f"Copying {index}/{len(mods)}: {mod.folder_name}",
        )
        destination = mods_root / f"{prefix}{mod.folder_name}"
        shutil.copytree(mod.mod_directory, destination)
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
                "packed_folder": destination.name,
                "source_workshop_id": mod.workshop_id,
                "original_mod_ids": list(mod.mod_ids),
                "packed_mod_ids": [mapping[item] for item in mod.mod_ids],
                "sha256": _tree_hash(mod.mod_directory),
            }
        )
    _emit_progress(progress, 78, "Rewriting runtime Mod ID lookups")
    compatibility_patches = _rewrite_known_id_checks(mods_root, reference_mapping)
    compatibility_patches.extend(
        _apply_known_compatibility_patches(mods_root, prefix, reference_mapping)
    )
    active_ids = [
        mapping[config.active_mod_ids.get(mod.folder_name, mod.mod_ids[-1])]
        for mod in mods
    ]
    _emit_progress(progress, 86, "Auditing unresolved Mod ID references")
    hardcoded_warning_details = _hardcoded_reference_details(mods_root, mapping)
    warning_details = validation_warning_details + hardcoded_warning_details
    warnings = [str(detail["message"]) for detail in warning_details]
    manifest = {
        "format_version": 1,
        "name": config.name,
        "description": config.description,
        "namespace": prefix.rstrip("_"),
        "workshop_id": config.workshop_id,
        "visibility": config.visibility,
        "preview": str(Path(config.preview).resolve()) if config.preview else None,
        "mapping": mapping,
        "active_mod_ids": active_ids,
        "active_mod_id_overrides": config.active_mod_ids,
        "compatibility_patches": compatibility_patches,
        "mods": manifest_mods,
        "warning_details": warning_details,
        "warnings": warnings,
    }
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
    _emit_progress(progress, 100, "Build complete")
    return BuildReport(
        output=output,
        mod_count=len(mods),
        mapping=mapping,
        warnings=tuple(warnings),
        warning_details=tuple(warning_details),
    )


def _read_id(mod_info: Path) -> str | None:
    for raw_line in mod_info.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip().lower() == "id":
            return value.strip().lstrip("\\") or None
    return None


def _workshop_id(path: Path) -> str | None:
    for candidate in (path, *path.parents):
        metadata = candidate / "snapshot.json"
        if metadata.is_file():
            try:
                workshop_id = str(json.loads(metadata.read_text(encoding="utf-8"))["workshop_id"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                workshop_id = ""
            if workshop_id.isdigit():
                return workshop_id
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
    for source in (Path(item).resolve() for item in source_roots):
        mods_root = source / "mods" if (source / "mods").is_dir() else source
        candidates = [path for path in sorted(mods_root.iterdir()) if path.is_dir()]
        if (mods_root / "mod.info").is_file():
            candidates = [mods_root]
        for mod_directory in candidates:
            ids = tuple(
                dict.fromkeys(
                    mod_id
                    for info in sorted(
                        mod_directory.rglob("mod.info"),
                        key=lambda path: (len(path.relative_to(mod_directory).parts), str(path)),
                    )
                    if (mod_id := _read_id(info))
                )
            )
            if ids:
                discovered.append(
                    DiscoveredMod(
                        source_root=source,
                        mod_directory=mod_directory,
                        folder_name=mod_directory.name,
                        mod_ids=ids,
                        workshop_id=_workshop_id(source),
                    )
                )
    return discovered
