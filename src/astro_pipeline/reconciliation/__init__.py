"""Stage 5: reconcile masters shot at different pixel scales/instruments/
users onto one common pixel grid via WCS-based reprojection.

Multi-instrument and multi-user combining both happen here, at the master
level: each instrument/user's data is independently calibrated, registered,
and stacked first (Stages 2-4), and only the resulting masters get
reprojected together -- never raw subs across instruments/users.

Split into a package (Task 5 Step 15) along three genuinely independent
algorithms that never call into each other, aside from all three sharing
`ReprojectionError`: reprojection (`reproject.py`), coverage cropping
(`coverage.py`), and gain/offset matching + weighted combine
(`gain_offset.py`). This module re-exports everything so no external
caller (`skill/run_narrowband_boost.py`, `colour_contributor.py`,
`lrgb_orchestrator.py`, `tests/test_reconciliation.py`, etc.) needs to
change its own import -- see `docs/task5-oop-refactor-plan.md` Section 2.
"""

from .coverage import crop_to_common_coverage
from .gain_offset import GainOffsetFit, combine_same_grid, fit_gain, match_gain_offset
from .reproject import (
    ReconciliationResult,
    ReprojectionError,
    pick_finest_reference,
    pixel_scale_deg,
    reconcile_masters,
    reproject_to_reference,
)

__all__ = [
    "ReconciliationResult",
    "ReprojectionError",
    "GainOffsetFit",
    "combine_same_grid",
    "crop_to_common_coverage",
    "fit_gain",
    "match_gain_offset",
    "pick_finest_reference",
    "pixel_scale_deg",
    "reconcile_masters",
    "reproject_to_reference",
]
