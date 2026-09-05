"""Project-folder layout conventions.

Kaveh's own working convention (adopted here rather than inventing a new
one): everything for a target lives together under the target's project
folder, with generated/intermediate data in a subfolder alongside the raw
delivery folders. So for a project folder like

    M51 - Whirlpool galaxy - T24 & T21 - Jan 2025/
        Calibrations/                     <- raw, from iTelescope
        Uncalibrated Lights - Jan 2025/   <- raw, from iTelescope
        _index.json                       <- generated (Stage 1)
        _pipeline/                        <- generated (everything else)

the pipeline writes only into `_pipeline/` and never mutates the raw
delivery folders. The leading underscore keeps generated content sorted
to the top and visually distinct from iTelescope's own folders.

This also matters for test durability: an earlier version of this project
kept intermediates in the OS temp directory, which got cleaned up between
sessions -- silently turning every real-data test into a skip while the
suite still reported green. Intermediates living next to the raw data
survive, so real-data tests keep actually running.
"""

from __future__ import annotations

from pathlib import Path

PIPELINE_DIRNAME = "_pipeline"
INDEX_FILENAME = "_index.json"


def pipeline_dir(project_dir: str | Path, create: bool = True) -> Path:
    """The generated-data directory for a target's project folder."""
    path = Path(project_dir) / PIPELINE_DIRNAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def index_path(project_dir: str | Path) -> Path:
    """Where Stage 1's whole-tree index lives for this project."""
    return Path(project_dir) / INDEX_FILENAME


def group_dir(project_dir: str | Path, group_name: str, create: bool = True) -> Path:
    """Working directory for one logical group (e.g. a filter's stack).

    `group_name` should be filesystem-safe and descriptive, e.g.
    "T24-kaveh096-M51-Luminance-bin1".
    """
    path = pipeline_dir(project_dir, create=create) / group_name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def group_name_for(telescope: str, user: str, target: str, filter_name: str, binning: int) -> str:
    """Stable, filesystem-safe name for a light group -- mirrors
    IngestReport.light_groups()'s key so a directory maps 1:1 to a group.
    """
    return f"{telescope}-{user}-{target}-{filter_name}-bin{binning}"
