from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProjectSettings:
    name: str
    namespace: str
    workshop_id: str
    description: str
    output: Path
    sources: tuple[Path, ...]
    steamcmd: Path
    steam_library: Path
    snapshot_root: Path
    workshop_items: tuple[str, ...] = ()
    preview: Path | None = None
    visibility: int = 2
    active_mod_ids: dict[str, str] = field(default_factory=dict)
    included_mod_ids: tuple[str, ...] | None = None
    version_bump: str = "patch"
    snapshot_selections: dict[str, str] = field(default_factory=dict)


def save_project(path: Path, settings: ProjectSettings) -> None:
    path = Path(path)
    payload = asdict(settings)
    payload["format_version"] = 1
    payload["output"] = str(settings.output)
    payload["sources"] = [str(item) for item in settings.sources]
    payload["steamcmd"] = str(settings.steamcmd)
    payload["steam_library"] = str(settings.steam_library)
    payload["snapshot_root"] = str(settings.snapshot_root)
    payload["workshop_items"] = list(settings.workshop_items)
    payload["preview"] = str(settings.preview) if settings.preview is not None else None
    payload["visibility"] = settings.visibility
    payload["included_mod_ids"] = (
        list(settings.included_mod_ids)
        if settings.included_mod_ids is not None
        else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_project(path: Path) -> ProjectSettings:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("format_version") != 1:
        raise ValueError("Unsupported project file format")
    return ProjectSettings(
        name=str(payload["name"]),
        namespace=str(payload["namespace"]),
        workshop_id=str(payload["workshop_id"]),
        description=str(payload["description"]),
        output=Path(payload["output"]),
        sources=tuple(Path(item) for item in payload.get("sources", [])),
        steamcmd=Path(payload["steamcmd"]),
        steam_library=Path(payload["steam_library"]),
        snapshot_root=Path(payload["snapshot_root"]),
        workshop_items=tuple(str(item) for item in payload.get("workshop_items", [])),
        preview=Path(payload["preview"]) if payload.get("preview") else None,
        visibility=int(payload.get("visibility", 2)),
        active_mod_ids={
            str(folder): str(mod_id)
            for folder, mod_id in payload.get("active_mod_ids", {}).items()
        },
        included_mod_ids=(
            tuple(str(mod_id) for mod_id in payload["included_mod_ids"])
            if payload.get("included_mod_ids") is not None
            else None
        ),
        version_bump=str(payload.get("version_bump", "patch")),
        snapshot_selections={
            str(workshop_id): str(revision)
            for workshop_id, revision in payload.get(
                "snapshot_selections",
                {},
            ).items()
        },
    )
