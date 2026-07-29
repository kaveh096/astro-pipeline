"""Materialize a logical frame group (from ingest.py) into its own clean
directory so each Siril `convert` operates on an isolated, homogeneous
folder -- decoupled from however the raw delivery happens to be laid out
on disk (which varies: Kaveh's own descriptive subfolders here, a flat
dump elsewhere, etc).

Copies rather than symlinks: Windows symlink creation needs elevated
privileges or Developer Mode, which isn't something to depend on silently.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def stage_frames(frame_paths: list[Path], dest_dir: str | Path) -> Path:
    """Copy each frame into dest_dir (created fresh, cleared if it already
    exists) using its original filename. Filenames within one logical group
    are already unique (verified against real deliveries), so no renaming
    is needed.
    """
    dest_dir = Path(dest_dir)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    for src in frame_paths:
        shutil.copy2(src, dest_dir / src.name)
    return dest_dir
