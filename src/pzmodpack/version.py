"""Package version shared by the GUI, builder, and upload metadata."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("pz-modpack-builder")
except PackageNotFoundError:
    __version__ = "0.6.1-dev"
