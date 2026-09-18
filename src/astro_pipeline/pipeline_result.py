"""`PipelineResult`, the return type shared by `run_lrgb` and `run_narrowband`.

Split into its own module (mirroring `filter_constants.py`'s precedent) so
`lrgb_orchestrator.py` and `pipeline.py` (which still hosts `run_narrowband`)
can both depend on it without depending on each other -- defining it in
either orchestration module and importing it into the other would create a
two-way import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .checkpoints import Checkpoint
from .export_image import ExportResult


@dataclass
class PipelineResult:
    masters: dict[str, Path] = field(default_factory=dict)
    composite_path: Path | None = None
    export_result: ExportResult | None = None
    checkpoints: list[Checkpoint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
