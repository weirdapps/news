"""Where the /video-brief scripts may read and write.

The skill that drives them is a model, so it chooses every path these scripts are
given. A path is therefore accepted only inside one folder, ~/Downloads by default
(VIDEO_BRIEF_DIR overrides it, for tests), and the mail signature is read from its one
known place rather than from an argument.
"""

import os
from pathlib import Path

from scripts.fence import within

SIGNATURE = Path.home() / ".outlook-cli" / "signature.html"


def base_dir() -> Path:
    return Path(os.environ.get("VIDEO_BRIEF_DIR") or Path.home() / "Downloads").resolve()


def inside(path: str) -> Path:
    """The resolved path, or ValueError when it falls outside the brief folder."""
    return within(path, base_dir())
