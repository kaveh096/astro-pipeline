"""What does staleness mean for a contributor, and how do we clear it --
extracted from pipeline.py (Task 5 refactor, 2026-09-16). Deliberately
separated from master_builder.py/colour_contributor.py: this conceptually
belongs with run_signature.py's domain, not with construction. Zero
dependency on Siril/GraXpert/SPCC (it only hashes filenames and deletes
files), so every function here is trivially fast-unit-testable without
any monkeypatching.
"""

from __future__ import annotations

from pathlib import Path

from .calibration import CalibrationMode
from .filter_constants import OSC_FILTER, RGB_FILTERS
from .logging_utils import log as _log
from .master_builder import resolve_lights
from .run_signature import frame_identity_hash
from .workspace import pipeline_dir


def _delete_if_exists(path: Path, reason: str, notes: list[str]) -> None:
    """Delete one stage-output file so usable()'s existing skip-if-present
    gate naturally regenerates it -- the actual mechanism run-signature
    invalidation uses (see run_signature.py's module docstring: no second,
    parallel gating system)."""
    path = Path(path)
    if path.exists():
        _log(f"[run ] {path.name}: {reason} -- deleting to force regeneration", notes)
        path.unlink()


def _colour_contributor_frame_hash(report, telescope: str, target: str, binning: int) -> str:
    """One hash for a colour contributor's entire R+G+B light set -- a
    change to ANY of its three filters' subs (not just one) must be
    caught, since all three feed the one rgbcomp'd composite."""
    names: list[str] = []
    for filter_name in RGB_FILTERS:
        lights, _ = resolve_lights(report, telescope, target, filter_name, binning)
        names.extend(f.path.name for f in lights)
    return frame_identity_hash(names)


def _osc_contributor_frame_hash(
    report, telescope: str, target: str, binning: int, calibration_mode: CalibrationMode
) -> str:
    """OSC counterpart of _colour_contributor_frame_hash() (RGB-only/OSC
    plan, 2026-09) -- one filter (OSC_FILTER) instead of three, and
    calibration_mode must be threaded through explicitly (unlike the mono
    helper's RAW_LOCAL default) since every real OSC contributor is
    PRECALIBRATED and resolve_lights() looks up a different provenance
    view for that mode."""
    lights, _ = resolve_lights(report, telescope, target, OSC_FILTER, binning, calibration_mode=calibration_mode)
    return frame_identity_hash([f.path.name for f in lights])


def _flat_frame_hash(flat_frames: list) -> str:
    """Slice 2.3's flat-set hashing rule: "" for an EMPTY matched flat set,
    not `frame_identity_hash([])`'s own real (non-empty) hash-of-an-empty-
    string constant.

    This matters concretely, not just cosmetically: T24 has zero flats of
    any kind in this delivery, structurally, permanently (see plan-flats-
    v3.md's Context section) -- a persisted `run_signature.json` predating
    `flat_frame_hash` reads it back as "" (the field's own default, see
    ContributorSignature). If a freshly-computed "no flats matched" value
    were instead `frame_identity_hash([])` (a real, fixed hash string, NOT
    ""), it would mismatch that persisted "" and mark T24's entire colour
    contributor stale -- forcing a full rebuild of its raw R/G/B masters,
    GraXpert, and SPCC -- on literally every run's FIRST comparison after
    this field was added, even though T24's actual flat situation never
    changed at all. That is a materially bigger, unintended one-time cost
    than the plan's own Slice 2.3 section describes (which names only the
    real T21_bin1 Luminance entry as the expected one-time rebuild).
    Keeping "no flats" as a stable "" on both sides avoids it, while a
    genuinely non-empty matched flat set still goes through the real
    `frame_identity_hash` mechanism -- an actual flat-set CHANGE (a
    re-shot flat, added/removed frames) is still caught exactly as before.
    """
    if not flat_frames:
        return ""
    return frame_identity_hash([f.path.name for f in flat_frames])


def _colour_contributor_flat_frame_hash(report, telescope: str, binning: int) -> str:
    """Slice 2.3's flat-aware sibling of _colour_contributor_frame_hash
    above: one hash for a colour contributor's entire matched flat set
    across R+G+B -- a change to ANY of the three filters' matched flats
    (a flat re-shot, a new filter's flats added) must be caught, since all
    three feed the one rgbcomp'd composite exactly like the light set
    does. See _flat_frame_hash for why an entirely empty matched flat set
    (T24's real, permanent situation) hashes to "" rather than
    frame_identity_hash([])'s own non-empty constant."""
    names: list[str] = []
    for filter_name in RGB_FILTERS:
        flats = report.flat_index().get((telescope, binning, filter_name), [])
        names.extend(f.path.name for f in flats)
    if not names:
        return ""
    return frame_identity_hash(names)


def _clear_colour_contributor_products(
    contrib_dir: Path,
    project_dir: Path,
    report,
    telescope: str,
    target: str,
    binning: int,
    calibration_mode: CalibrationMode = CalibrationMode.RAW_LOCAL,
) -> list[Path]:
    """Delete one colour contributor's raw R/G/B masters AND its own
    build products (rgb_native.fit onward), so _build_colour_contributor
    rebuilds it cleanly from scratch rather than mixing freshly-rebuilt
    masters with stale downstream products from the old ones. Returns the
    paths actually deleted, for logging by the caller (which knows the
    human-readable BIN{n} label this function doesn't).

    `calibration_mode` mirrors _build_colour_contributor's own parameter
    (precalibrated-path plan) so the group_name/master_path computed here
    matches whichever provenance _build_colour_contributor will actually
    rebuild from."""
    deleted: list[Path] = []
    for name in (
        "rgb_native.fit", "red.fit", "green.fit", "blue.fit",
        "rgb_native_bg.fits", "rgb_colour_calibrated.fit",
    ):
        candidate = contrib_dir / name
        if candidate.exists():
            candidate.unlink()
            deleted.append(candidate)
    for filter_name in RGB_FILTERS:
        _, group_name = resolve_lights(
            report, telescope, target, filter_name, binning, calibration_mode=calibration_mode
        )
        master_path = pipeline_dir(project_dir) / group_name / "lights" / f"master_{filter_name.lower()}.fit"
        if master_path.exists():
            master_path.unlink()
            deleted.append(master_path)
    return deleted


def _clear_osc_contributor_products(
    contrib_dir: Path,
    project_dir: Path,
    report,
    telescope: str,
    target: str,
    binning: int,
    calibration_mode: CalibrationMode,
) -> list[Path]:
    """OSC counterpart of _clear_colour_contributor_products() (RGB-only/
    OSC plan, 2026-09) -- no rgb_native.fit/red.fit/green.fit/blue.fit
    intermediates exist for OSC (debayer+stack produces the RGB composite
    directly), so only the background-extracted and colour-calibrated
    products, plus the one OSC master, need clearing."""
    deleted: list[Path] = []
    for name in ("rgb_native_bg.fits", "rgb_colour_calibrated.fit"):
        candidate = contrib_dir / name
        if candidate.exists():
            candidate.unlink()
            deleted.append(candidate)
    _, group_name = resolve_lights(
        report, telescope, target, OSC_FILTER, binning, calibration_mode=calibration_mode
    )
    master_path = pipeline_dir(project_dir) / group_name / "lights" / f"master_{OSC_FILTER.lower()}.fit"
    if master_path.exists():
        master_path.unlink()
        deleted.append(master_path)
    return deleted
