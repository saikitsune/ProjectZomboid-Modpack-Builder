"""Project Zomboid modpack builder."""

from importlib.metadata import PackageNotFoundError, version

from .backend import DiscoveredMod, discover_mods

try:
    __version__ = version("pz-modpack-builder")
except PackageNotFoundError:
    __version__ = "0.4.2-dev"

__all__ = ["DiscoveredMod", "__version__", "discover_mods"]
