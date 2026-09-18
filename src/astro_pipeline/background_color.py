"""Stages 6-7: color calibration (SPCC) and background extraction (GraXpert).

THE ACTUAL RULE, arrived at over three wrong turns: **colour calibration
requires NaN-free input.** Everything else about ordering follows from
that, and nothing else about ordering matters. This was discovered using
PCC (Siril's older, broadband method, since removed from this codebase in
favour of SPCC -- see docs/colour-calibration-catalogues.md for that
history) but the constraint is in Siril's star-photometry engine, which
SPCC also depends on, so it applies just the same.

The wrong turns are worth recording, because each looked convincing:

  1. "Colour calibration must run before GraXpert" -- concluded when it
     failed on GraXpert's output. The real cause was that GraXpert's
     output was 100% NaN (from a background clipped to exact zero
     upstream), and photometry was correctly refusing to run on garbage.
  2. "Order does not matter" -- concluded after fixing that, when it then
     succeeded in both orders. True for that data, but only because it
     happened to be NaN-free by then.
  3. Both were symptoms of the same underlying constraint, which only
     became visible when NaN was reintroduced deliberately: it dies with
     "Error computing FWHM for photometry settings adjustment" the moment
     any NaN is present, at a fraction as small as 0.27%.

Siril tolerates NaN perfectly well in stretching and compositing. It is
specifically star photometry that cannot. So the invariant to preserve is
that whatever reaches SPCC has no NaN in it -- see the nan-filling in
run_graxpert_background_extraction, and why `restore_nan` defaults to off.
The root cause behind wrong turn #1 was actually upstream (see
calibration.py's `pedestal` parameter): Siril's stack output was clipping
a slightly-negative-on-average background to exact 0.0, leaving >99.9% of
the master exactly zero with only star peaks nonzero. GraXpert's
background model silently produced 100% NaN output on that degenerate
input (no error, no warning beyond a "divide by zero" that misleadingly
also appears on healthy runs) -- and colour calibration then, correctly,
failed to compute photometry on NaN garbage. Once the pedestal fix
resolved the root cause, it succeeded both before AND after GraXpert.
Background-extraction-first is kept as the *default* order here only
because it's the more conventional practice (cleaner background for star
photometry), not because the other order is broken.

Split into two modules (Task 5 Step 16), since colour calibration and
background extraction are operationally unrelated (different binaries,
different failure modes): `color_calibration.py` (SPCC -- InstrumentProfile/
OSCInstrumentProfile registries, run_spcc) and `background_extraction.py`
(GraXpert -- run_graxpert_background_extraction, run_graxpert_denoise, the
shared NaN-fill/corruption-check helpers). This module now only keeps
`calibrate_color_and_background()`, which orchestrates both, plus re-exports
of every name external callers (skill/run_post_process.py,
colour_contributor.py, lrgb_orchestrator.py, tests) import from
`background_color` by this module's name today.
"""

from __future__ import annotations

from pathlib import Path

from .background_extraction import (
    BackgroundExtractionError,
    DenoiseError,
    find_graxpert,
    run_graxpert_background_extraction,
    run_graxpert_denoise,
)
from .color_calibration import (
    INSTRUMENT_PROFILES,
    OSC_INSTRUMENT_PROFILES,
    CatalogueUnavailableError,
    ColorCalibrationError,
    InstrumentProfile,
    OSCInstrumentProfile,
    SPCCResult,
    T02_OSC_PROFILE,
    T24_PROFILE,
    T59_PROFILE,
    T73_PROFILE,
    UnknownInstrumentError,
    _is_catalogue_unavailable,
    _parse_spcc_result,
    resolve_instrument_profile,
    resolve_osc_instrument_profile,
    run_spcc,
)

__all__ = [
    "BackgroundExtractionError",
    "CatalogueUnavailableError",
    "ColorCalibrationError",
    "DenoiseError",
    "INSTRUMENT_PROFILES",
    "InstrumentProfile",
    "OSC_INSTRUMENT_PROFILES",
    "OSCInstrumentProfile",
    "SPCCResult",
    "T02_OSC_PROFILE",
    "T24_PROFILE",
    "T59_PROFILE",
    "T73_PROFILE",
    "UnknownInstrumentError",
    "calibrate_color_and_background",
    "find_graxpert",
    "resolve_instrument_profile",
    "resolve_osc_instrument_profile",
    "run_graxpert_background_extraction",
    "run_graxpert_denoise",
    "run_spcc",
]


def calibrate_color_and_background(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    profile: InstrumentProfile = T24_PROFILE,
    siril_cli: Path | None = None,
    graxpert_exe: Path | None = None,
) -> tuple[SPCCResult, Path]:
    """Orchestrates Stages 6-7: GraXpert background extraction first, then
    SPCC on the result. Both orders are verified to work for colour
    calibration in general (see module docstring); background-first is
    used here as the conventional default (cleaner background for star
    photometry), not because calibration-first is broken -- swap freely if
    there's a reason to.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)

    bg_output = run_graxpert_background_extraction(
        rgb_composite_path,
        output_stem=f"{rgb_composite_path.stem}_bg",
        graxpert_exe=graxpert_exe,
    )

    spcc_result = run_spcc(bg_output, work_dir, profile=profile, siril_cli=siril_cli)

    return spcc_result, bg_output
