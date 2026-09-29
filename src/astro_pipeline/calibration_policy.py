"""Per-telescope calibration policy inference, extracted from pipeline.py
(Task 5 refactor, 2026-09-16). Same shape for both functions: given a
report and a telescope, infer a policy enum from what the data actually
has, never hardcode by name.
"""

from __future__ import annotations

from .calibration import CalibrationMode, FlatPolicy


def infer_flat_policy(report, telescope: str) -> FlatPolicy:
    """Slice 2.2's default `FlatPolicy` for `telescope`, inferred from
    whether it ships ANY flat at all -- not hardcoded by telescope name.

    Deliberately mirrors this project's existing preference
    (resolve_instrument_profile, above) for refusing to special-case a
    telescope by literal name where the data itself already says what's
    needed -- though this is a distinct mechanism (inferred from presence
    in `report.flat_index()`, not a lookup into a curated registry like
    `INSTRUMENT_PROFILES`; stated plainly so the two aren't conflated).

    `FlatPolicy.REQUIRE` if `flat_index()` has any entry at all for this
    telescope (at any binning/filter): a telescope that ships flats for
    SOME filters but is then missing one a light group actually needs is a
    real, surfaceable gap (raises `CalibrationFramesMissingError` via
    `run_calibration`), not something to silently skip.
    `FlatPolicy.SKIP_IF_MISSING` if this telescope has zero flats of any
    kind -- T24's permanent, real situation for this delivery (see
    plan-flats-v3.md's "Context" section: T24 structurally never ships
    flats here).

    Overridable uniformly across every telescope in a run via
    `run_lrgb`'s own `flat_policy` parameter -- see there; this function
    only computes the per-telescope DEFAULT, before any override is
    applied.
    """
    has_any_flat = any(key[0] == telescope for key in report.flat_index())
    return FlatPolicy.REQUIRE if has_any_flat else FlatPolicy.SKIP_IF_MISSING


def infer_calibration_mode(report, telescope: str) -> CalibrationMode:
    """Precalibrated-path plan's default `CalibrationMode` for `telescope`,
    mirroring `infer_flat_policy`'s own pattern: inferred from what the
    data actually has, not hardcoded by telescope name, with an explicit
    per-telescope override escape hatch in `run_lrgb`.

    `CalibrationMode.PRECALIBRATED` if `telescope` is missing local Bias
    or Dark frames (either one, not necessarily both -- raw+local
    calibration structurally cannot work without both, and
    `run_calibration` would raise `CalibrationFramesMissingError` before
    ever reaching registration) AND has at least one "calibrated"-
    provenance light group (there is something to fall back to).

    `CalibrationMode.RAW_LOCAL` otherwise -- unconditionally the default
    for every telescope with real Bias+Dark (T24, T21), unchanged from
    before this function existed. A delivery with neither local
    bias/dark NOR any calibrated-provenance lights also resolves
    RAW_LOCAL, deliberately: that combination has nothing this function
    can safely infer its way out of, so it stays on the path that fails
    loudly (`CalibrationFramesMissingError`) instead of silently
    resolving to a mode with no real data behind it either.

    Step 4b/G5, Decision Q2 ("Keep", permanent): counts only
    `source == "filename"` bias/dark frames -- header-recognised bias/dark
    (`CalibrationFrame.source == "header"`, opt-in via
    `calibration_header_fallback`) never flips this default for ANY
    telescope, ever. Read via `getattr(f, "source", "filename")`, not
    `f.source` directly, so the ~20 pre-existing test fakes that return
    plain strings (not `CalibrationFrame` objects) from
    `calibration_index()` keep working unchanged -- a plain string has no
    `.source` attribute, so the default applies and it counts as
    filename-sourced, exactly today's behaviour.
    """
    cal_index = report.calibration_index()

    def _has_filename_sourced(frame_type: str) -> bool:
        return any(
            key[0] == telescope and key[1] == frame_type
            and any(getattr(f, "source", "filename") == "filename" for f in frames)
            for key, frames in cal_index.items()
        )

    has_bias = _has_filename_sourced("Bias")
    has_dark = _has_filename_sourced("Dark")
    has_calibrated_lights = any(
        key[0] == telescope for key in report.calibrated_instrument_groups()
    )
    if (not has_bias or not has_dark) and has_calibrated_lights:
        return CalibrationMode.PRECALIBRATED
    return CalibrationMode.RAW_LOCAL
