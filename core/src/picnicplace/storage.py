"""Small helpers for files in the data directory."""

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and rename it, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
