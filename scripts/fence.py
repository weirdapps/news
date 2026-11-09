"""Keep a path given on a command line inside the one folder it belongs to.

These scripts are driven by agents and launchd as well as by hand, so a path
argument is treated as untrusted: it must resolve inside its folder, or it is
refused before anything opens, writes or ships it.
"""

from pathlib import Path


def within(path: str | Path, base: Path) -> Path:
    """The resolved path, or ValueError when it falls outside ``base``."""
    root = base.resolve()
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{path} is outside {root}")
    return resolved
