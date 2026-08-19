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
    display_name: str = ""
    description: str = ""


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


def build_modpack(
    config: BuildConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> BuildReport:
    _emit_progress(progress, 0, "Starting build")
    prefix = _safe_namespace(config.namespace)
    if config.visibility not in {0, 1, 2, 3}:
        raise BuildError("Workshop visibility must be 0, 1, 2, or 3")
    _emit_progress(progress, 5, "Discovering mod sources")
    discovered_mods = discover_mods(config.sources)
    if not discovered_mods:
        raise BuildError("No mods were discovered in the selected sources")
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
                "original_mod_ids": list(mod.mod_ids),
                "packed_mod_ids": [mapping[item] for item in mod.mod_ids],
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
    manifest = {
        "format_version": 1,
        "name": config.name,
        "description": config.description,
        "namespace": prefix.rstrip("_"),
        "workshop_id": config.workshop_id,
        "visibility": config.visibility,
        "preview": str(Path(config.preview).resolve()) if config.preview else None,
        "mapping": mapping,
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
                        workshop_id=_workshop_id(source),
                        display_name=(
                            _read_mod_info_value(display_info, "name")
                            or mod_directory.name
                        ),
                        description=(
                            _read_mod_info_value(display_info, "description") or ""
                        ),
                    )
                )
    return discovered
