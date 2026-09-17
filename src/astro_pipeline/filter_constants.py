"""Filter-name constants shared across pipeline.py and its extracted
modules (Task 5 refactor, 2026-09-16). Kept zero-dependency so lower-level
modules (master_builder.py, calibration_policy.py, luminance_selection.py,
contributor_staleness.py, ...) never have to import back from pipeline.py
just to get these -- that would recreate the import-cycle risk the
module split is meant to avoid.
"""

from __future__ import annotations

RGB_FILTERS = ("Red", "Green", "Blue")
LUMINANCE_FILTER = "Luminance"
# One-shot-colour (OSC) filter identity (RGB-only/OSC plan, 2026-09). A
# genuinely different build shape from RGB_FILTERS -- one already-Bayer-
# mosaic filter debayered into a 3-channel composite directly, not three
# separate mono masters combined via rgbcomp. Deliberately NOT folded
# into RGB_FILTERS -- mixing the two build shapes into one constant would
# blur them. Only "Color" is special-cased; any other unrecognized filter
# keeps falling through to the existing "missing R/G/B, skip this
# contributor" behaviour, not silently treated as OSC.
OSC_FILTER = "Color"
