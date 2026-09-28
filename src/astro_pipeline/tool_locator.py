"""Shared tool-location logic for this pipeline's external CLI
dependencies (Siril, ASTAP, GraXpert, StarNet2).

Resolution order: an `ASTRO_PIPELINE_*` environment variable naming the
exact executable (if set but the file doesn't exist, that's a loud error,
not a silent fall-through to a different location -- an explicit override
that's wrong should say so) -> a list of known default install locations
-> `shutil.which` on PATH.

Each tool's own module (siril_driver.py, solving.py,
background_extraction.py, star_removal.py) keeps its own `find_X()`
function and `DEFAULT_X_CANDIDATES` module-level list (read fresh on every
call, not captured at import) -- this module only holds the shared
resolution logic those thin wrappers call into.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class ToolNotFoundError(FileNotFoundError):
    """A required external tool's executable could not be located."""


def resolve_tool(
    display_name: str,
    env_var: str,
    candidates: list[Path],
    which_names: list[str],
    install_hint: str,
) -> Path:
    """Resolve `display_name`'s executable.

    `display_name` is used in error messages (and, for StarNet2, matched
    by an existing test -- keep it as the exact executable filename, e.g.
    "starnet2.exe", not a human-friendly product name).
    """
    env_val = os.environ.get(env_var)
    if env_val:
        env_path = Path(env_val)
        if env_path.exists():
            return env_path
        raise ToolNotFoundError(
            f"{env_var} is set to {env_val!r}, but no file exists there. "
            f"Fix or unset {env_var}. {install_hint}"
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    for name in which_names:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise ToolNotFoundError(
        f"{display_name} not found in known locations "
        f"({[str(c) for c in candidates]}) or on PATH. {install_hint} "
        f"Or set {env_var} to its exact path."
    )
