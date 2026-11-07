"""PicnicPlace core: a network-first pick-and-place machine controller."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("picnicplace")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
