from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SavedSteamSession:
    username: str
    steamcmd: Path
    steam_library: Path


def save_steam_session(path: Path, session: SavedSteamSession) -> None:
    path = Path(path)
    payload = asdict(session)
    payload["format_version"] = 1
    payload["steamcmd"] = str(session.steamcmd)
    payload["steam_library"] = str(session.steam_library)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_steam_session(path: Path) -> SavedSteamSession | None:
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format_version") != 1:
        return None
    username = str(payload.get("username", "")).strip()
    if not username:
        return None
    return SavedSteamSession(
        username=username,
        steamcmd=Path(payload["steamcmd"]),
        steam_library=Path(payload["steam_library"]),
    )


def clear_steam_session(path: Path) -> None:
    Path(path).unlink(missing_ok=True)
