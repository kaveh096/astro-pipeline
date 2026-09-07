"""Stage 2: build master calibration frames and calibrate light frames.

Command syntax below is verified against the real installed Siril 1.4.3
CLI (`help convert`, `help stack`, `help calibrate`), not assumed from
docs. One non-obvious, empirically-confirmed detail: `convert <basename>`
creates a sequence literally named `<basename>_` (trailing underscore) --
`stack`/`calibrate` must reference that exact name, not the bare basename.

Bias and dark are treated as required: there is no reasonable degraded
mode without them. Flats are optional and policy-controlled -- a real
iTelescope delivery (M51/T24) arrived with bias+dark but zero flats, which
is apparently a normal, recoverable situation (flats can be added and
recalibration re-run later), not something to hard-block on.

CRITICAL finding, verified empirically, that motivates the `pedestal`
parameter on calibrate_lights(): bias+dark-only calibration (no flat)
routinely leaves the background slightly negative on average (verified:
individual calibrated frames had means/mins down to -0.096, which is
normal and expected without a flat to correct the zero point). Siril's
`stack` output then clips negative averaged pixels to exactly 0.0 --
found by direct inspection, not documented anywhere -- which destroys the
background's continuous noise texture and leaves a master that is >99.9%
exact zero with only star peaks nonzero. That degenerate "mostly exactly
zero" image is what silently broke GraXpert's background-extraction (see
background_color.py) with 100% NaN output and no error -- not a GraXpert
bug in isolation, a real consequence of this calibration-stage clipping.
Fix: add a small pedestal (default 0.1, comfortably above the observed
worst-case ~-0.1 minimum) to calibrated lights before registration/
stacking, so the averaged background never goes negative and never gets
clipped. Verified: with the pedestal, the resulting master had zero exact-
zero pixels (min 0.031, real continuous background) and GraXpert produced
valid (0% NaN) output.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np
from astropy.io import fits

from .ingest import CalibrationFrame, LightFrame
from .siril_driver import SirilError, SirilResult, run_script
from .staging import stage_frames

DEFAULT_PEDESTAL = 0.1


class FlatPolicy(str, Enum):
    REQUIRE = "require"  # missing flats -> CalibrationFramesMissingError
    SKIP_IF_MISSING = "skip_if_missing"  # proceed without flat correction


class CalibrationFramesMissingError(RuntimeError):
    """Bias/dark are missing, or flats are missing under FlatPolicy.REQUIRE.

    Must be raised, not silently worked around -- calibrating without bias
    or dark correction is not a reasonable degraded mode the way skipping
    flats can be.
    """


def _log(message: str, notes: list[str] | None) -> None:
    print(message, flush=True)
    if notes is not None:
        notes.append(message)


@dataclass
class DarkSelection:
    frames: list[CalibrationFrame]
    exptime: float
    scaled: bool


def select_dark(
    cal_index: dict[tuple[str, str, int, float], list[CalibrationFrame]],
    telescope: str,
    binning: int,
    light_exptimes: set[float],
) -> DarkSelection:
    """Pick which dark master to calibrate a contributor group's lights
    against, following the policy verified against real T21/T24 data and
    the real installed Siril 1.4.4 binary (`help calibrate`: `-opt`
    "requires the supply of bias and dark masters"; confirmed empirically
    that `-opt=exp` computes a per-image coefficient k0 = light exptime /
    dark exptime, e.g. T21's real 600s/300s lights against its real 900s
    dark measured k0=0.667/0.333 exactly):

    1. An exact-exptime dark exists for this (telescope, binning) and the
       group is single-exptime -> use it as-is, no scaling. This is
       today's only path, and every existing real T24 group hits it --
       it must not change.
    2. No exact match, but a dark exists at this binning for some other
       exptime -> scale it via `calibrate -opt=exp`, choosing whichever
       available exptime is closest from above (minimizes the scaling
       extrapolation) among those at least as long as every light's
       exptime -- scaling a dark DOWN is the safe direction; scaling one
       UP is refused below. NEVER falls back to a different binning: a
       different binning is a different pixel dimension, not just a
       different exposure time, and Siril's `calibrate` hard-fails
       (verified real: exit 1, "Images must have same dimensions") if fed
       mismatched-dimension frames, so a cross-binning fallback would only
       ever break loudly, not silently -- but it's also just physically
       wrong, so it is not attempted at all.
    3. No dark at this binning at all -> CalibrationFramesMissingError,
       naming the exposure time(s) that have nothing to calibrate against.
    4. Every available dark at this binning is SHORTER than the longest
       light exptime -> CalibrationFramesMissingError (refuses to scale a
       dark UP, the unsafe direction).
    """
    if len(light_exptimes) == 1:
        exptime = next(iter(light_exptimes))
        exact = cal_index.get((telescope, "Dark", binning, exptime))
        if exact:
            return DarkSelection(exact, exptime, scaled=False)

    available = {
        e: frames
        for (t, ftype, b, e), frames in cal_index.items()
        if t == telescope and ftype == "Dark" and b == binning and frames
    }
    if not available:
        exptimes_str = ", ".join(f"{e:.0f}s" for e in sorted(light_exptimes))
        raise CalibrationFramesMissingError(
            f"No Dark frames at any exposure time found for {telescope} BIN{binning} "
            f"(needed for lights at {exptimes_str})."
        )

    max_light_exptime = max(light_exptimes)
    safe = {e: frames for e, frames in available.items() if max_light_exptime / e <= 1}
    if not safe:
        longest = max(available)
        raise CalibrationFramesMissingError(
            f"No Dark at {telescope} BIN{binning} is as long as the longest light "
            f"exptime ({max_light_exptime:.0f}s); longest available dark is "
            f"{longest:.0f}s. Scaling a dark UP is the unsafe direction and is refused."
        )
    chosen_exptime = min(safe)
    return DarkSelection(safe[chosen_exptime], chosen_exptime, scaled=True)


_NOT_USING_DARK_RE = re.compile(r"NOT USING DARK:.*", re.IGNORECASE)
_NEGATIVE_PIXELS_RE = re.compile(r"contains many negative pixels.*", re.IGNORECASE)
_K0_RE = re.compile(r"Dark optimization of image \d+: k0=[-\d.]+")


def _check_calibration_log(result: SirilResult) -> None:
    """Exit code 0 alone is not proof calibration actually worked. Both
    strings below are real, verified-present strings in the installed
    Siril 1.4.4 binary (`siril-cli.exe`), found by direct inspection, not
    documented anywhere obvious:

        "NOT USING DARK: image dimensions are different" (and siblings --
        cannot open the file, could not parse the expression, etc.)
        "After dark subtraction, the image contains many negative pixels
        (%d%%), calibration frames are probably incorrect"

    Both mean the calibrate command reported success while the result is
    not what was asked for -- escalate rather than trust the exit code.
    Verified NOT present in a real T24 calibrate run (dark-only path), so
    this cannot regress existing behaviour.
    """
    problems = [
        line
        for line in result.log_lines
        if _NOT_USING_DARK_RE.search(line) or _NEGATIVE_PIXELS_RE.search(line)
    ]
    if problems:
        raise SirilError(
            "calibrate reported success (exit 0) but its own log flags a problem "
            "that would otherwise pass silently: " + " | ".join(problems),
            result,
        )


def _log_dark_optimization(result: SirilResult, notes: list[str] | None) -> None:
    for line in result.log_lines:
        if _K0_RE.search(line):
            _log(f"       {line}", notes)


def _dark_light_gap_note(master_dark: Path, light_frames: list[LightFrame]) -> str | None:
    """Informational only, not a blocker: how far apart in time are the
    dark and the lights being calibrated against it? Calibration validity
    is scoped to what was delivered/organized together, not to a single
    capture night (see ingest.py's module docstring), and SET-TEMP
    matching is what actually determines a dark's validity -- but a
    multi-month gap (T21's real case: darks from 2024-06, lights from
    2025-01) is still worth a human seeing rather than silently buried in
    a "calibrate succeeded" log line.
    """
    try:
        dark_header = fits.getheader(master_dark)
        dark_date = datetime.fromisoformat(str(dark_header["DATE-OBS"])[:19])
        dark_set_temp = dark_header.get("SET-TEMP")
    except Exception:
        return None

    light_dates = []
    for frame in light_frames:
        try:
            light_dates.append(datetime.strptime(frame.date, "%Y%m%d"))
        except Exception:
            continue
    if not light_dates:
        return None

    gap_days = abs((min(light_dates) - dark_date).days)
    if gap_days < 14:
        return None
    months = gap_days / 30.44
    temp_note = f"SET-TEMP={dark_set_temp}" if dark_set_temp is not None else "SET-TEMP unknown"
    return (
        f"[info] dark master's reference frame is {gap_days} days (~{months:.1f} months) "
        f"before the lights being calibrated against it ({temp_note} -- check this "
        f"matches the lights' own SET-TEMP; not a blocker by itself, just worth surfacing)."
    )


@dataclass
class CalibrationResult:
    calibrated_lights: list[Path]
    master_bias: Path
    master_dark: Path
    master_flat: Path | None
    flat_corrected: bool
    dark_scaled: bool = False


def sequence_name(basename: str) -> str:
    """Siril's `convert <basename>` creates a sequence named `<basename>_`
    (verified against the real CLI) -- callers referencing the sequence in
    later commands must use this, not the bare basename."""
    return f"{basename}_"


def _calibrate_command(
    seq: str,
    dark_stem: str,
    bias_stem: str | None,
    flat_stem: str | None,
    dark_optimize: bool,
) -> str:
    """Pure command-string builder, split out from calibrate_lights so the
    "nothing changes for existing T24 data" claim is checkable without
    invoking Siril: with bias_stem=None, flat_stem=None, dark_optimize=
    False (today's only real path), this must produce byte-identical
    output to before this function existed.
    """
    command = f"calibrate {seq} -dark={dark_stem}"
    if not dark_optimize:
        # -cc=dark's hot/cold-pixel thresholds are fit to the dark's own
        # exposure time -- meaningless (and not requested) on the scaled
        # path.
        command += " -cc=dark"
    if bias_stem is not None:
        command += f" -bias={bias_stem}"
    if dark_optimize:
        command += " -opt=exp"
    if flat_stem is not None:
        command += f" -flat={flat_stem}"
    command += " -prefix=pp_"
    return command


def build_master(
    frame_paths: list[Path],
    basename: str,
    work_dir: str | Path,
    rejection: str = "rej",
    sigma_low: float = 3.0,
    sigma_high: float = 3.0,
) -> Path:
    """Stage frames, convert to a Siril sequence, stack with rejection.
    Returns the path to the resulting master FITS."""
    if not frame_paths:
        raise CalibrationFramesMissingError(f"No frames provided to build master '{basename}'.")

    stage_dir = Path(work_dir) / basename
    stage_frames(frame_paths, stage_dir)

    seq = sequence_name(basename)
    run_script(
        [
            f"convert {basename}",
            f"stack {seq} {rejection} {sigma_low} {sigma_high} -out=master",
        ],
        workdir=stage_dir,
    )
    master_path = stage_dir / "master.fit"
    if not master_path.exists():
        raise RuntimeError(f"Siril reported success but {master_path} was not created.")
    return master_path


def build_master_bias(bias_frames: list[CalibrationFrame], work_dir: str | Path) -> Path:
    return build_master([f.path for f in bias_frames], "bias", work_dir)


def build_master_dark(dark_frames: list[CalibrationFrame], work_dir: str | Path) -> Path:
    return build_master([f.path for f in dark_frames], "dark", work_dir)


def build_master_flat(flat_frames: list[CalibrationFrame], work_dir: str | Path) -> Path:
    """UNTESTED against real data -- no flat sample has been available yet.
    Follows the same convert/stack pattern as bias/dark; revisit the
    rejection parameters once a real flat set is on hand."""
    return build_master([f.path for f in flat_frames], "flat", work_dir)


def _apply_pedestal(paths: list[Path], pedestal: float) -> None:
    """Add a constant offset to each FITS file in place. See module
    docstring for why this is necessary: without it, backgrounds that
    average slightly negative get clipped to exact zero by Siril's stack
    output, which silently breaks GraXpert's background extraction later.
    """
    for path in paths:
        # memmap=False is required on Windows: getdata's default memory-map
        # keeps a file handle open, and writing back to the same path while
        # that handle is alive fails with PermissionError (verified real).
        data, header = fits.getdata(path, header=True, memmap=False)
        shifted = (data + pedestal).astype(data.dtype)
        fits.writeto(path, shifted, header=header, overwrite=True)


def calibrate_lights(
    light_frames: list[LightFrame],
    master_bias: Path,
    master_dark: Path,
    work_dir: str | Path,
    master_flat: Path | None = None,
    basename: str = "lights",
    pedestal: float = DEFAULT_PEDESTAL,
    subtract_bias: bool = False,
    dark_optimize: bool = False,
    notes: list[str] | None = None,
) -> tuple[list[Path], SirilResult]:
    """Convert light_frames to a sequence and run Siril's `calibrate`
    against the given masters. Returns (calibrated file paths, sorted,
    prefixed "pp_"; the calibrate command's SirilResult).

    Adds `pedestal` to every calibrated frame before returning (default
    0.1) -- see module docstring. Pass pedestal=0.0 to disable, but be
    aware downstream background extraction may then silently produce
    garbage rather than erroring (verified: no exception, no warning
    beyond a "divide by zero" that also appears on healthy runs).

    `subtract_bias` defaults to False, i.e. lights are calibrated against
    the dark alone. A dark of matching exposure and temperature already
    contains the bias signal, so also subtracting a separately-stacked bias
    master is redundant -- and actively harmful when that master is built
    from few frames, because its own read noise and fixed-column pattern
    get imprinted onto every light. Siril's own bundled scripts pass -bias
    only when calibrating flats, never lights.

    Measured on the real T24 data (5 bias, 5 dark, 13 lights), using
    column-to-column scatter normalised by the frame's pixel noise, where
    ~1 would mean no fixed-column structure:

        raw light                2.03
        bias + dark calibrated   3.82   <- calibration nearly doubled it
        dark only calibrated     3.34

    Both calibrated numbers exceed the raw frame because a 5-frame master
    retains ~45% of a single frame's noise; the real fix is more
    calibration frames (T21's delivery has 82 bias / 25 darks per binning,
    T24's has 5), which is a data limitation rather than a code one.
    Dropping the redundant bias subtraction is the part that is fixable
    here. Siril's `fixbanding` was also tried and moved the metric only
    3.24 -> 3.21, so it is not used.

    `dark_optimize=True` is the path for a dark whose exposure time does
    not match the lights' (see `select_dark`): passes `-opt=exp` instead
    of `-cc=dark` (the hot/cold-pixel thresholds `-cc=dark` fits are tuned
    to the dark's own exposure time, not a scaled one) and forces
    `subtract_bias=True`, since `-opt` requires a bias master (confirmed
    in the real installed Siril 1.4.4's own `help calibrate` text). Verified
    end-to-end against T21's real 600s/300s Luminance lights and 900s dark:
    Siril computes a per-image coefficient (`k0 = light exptime / dark
    exptime`) and applies it individually, exactly as documented.
    """
    if not light_frames:
        raise CalibrationFramesMissingError("No light frames provided to calibrate.")

    if dark_optimize:
        subtract_bias = True

    stage_dir = Path(work_dir) / basename
    stage_frames([f.path for f in light_frames], stage_dir)

    # Masters are COPIED into the sequence's own directory and referenced by
    # bare name rather than by absolute path. Siril's .ssf parser splits
    # arguments on whitespace, so any absolute path containing a space is
    # truncated at the first one -- verified real, and not hypothetical: the
    # project folder this pipeline runs against is named
    # "M51 - Whirlpool galaxy - T24 & T21 - Jan 2025", which broke
    # `-bias=<abs path>` with "C:\Users\Kaveh\Desktop\M51.[any_allowed_
    # extension] not found". Staging also dodges the ampersand in that same
    # folder name.
    #
    # CRITICAL ordering: `convert` ingests *every* supported image in the
    # working directory, so the masters must NOT be present when it runs --
    # otherwise they are swept into the light sequence itself. Verified
    # real: staging them up-front made frame #1 of the sequence a
    # calibrated bias frame, and registration died with "Found 0 stars in
    # reference". Hence two separate Siril invocations: build the sequence
    # first, stage the masters second, calibrate third.
    #
    # The extension is stripped in the arguments: Siril appends its own, and
    # passing one yields "<path>.fit.[any_allowed_extension] not found".
    seq = sequence_name(basename)
    run_script([f"convert {basename}"], workdir=stage_dir, script_name="convert.ssf")

    staged_dark = stage_dir / "masterdark.fit"
    shutil.copy2(master_dark, staged_dark)
    staged_bias = stage_dir / "masterbias.fit"
    if subtract_bias:
        shutil.copy2(master_bias, staged_bias)
    staged_flat = stage_dir / "masterflat.fit"
    if master_flat is not None:
        shutil.copy2(master_flat, staged_flat)

    command = _calibrate_command(
        seq, staged_dark.stem,
        staged_bias.stem if subtract_bias else None,
        staged_flat.stem if master_flat is not None else None,
        dark_optimize=dark_optimize,
    )

    result = run_script([command], workdir=stage_dir, script_name="calibrate.ssf")
    _check_calibration_log(result)
    if dark_optimize:
        _log_dark_optimization(result, notes)
        gap_note = _dark_light_gap_note(master_dark, light_frames)
        if gap_note:
            _log(gap_note, notes)

    calibrated = sorted(stage_dir.glob(f"pp_{seq}*.fit*"))
    if not calibrated:
        raise RuntimeError(
            f"Siril reported success but no 'pp_{seq}*' calibrated files were found in {stage_dir}."
        )

    if pedestal:
        _apply_pedestal(calibrated, pedestal)

    return calibrated, result


def run_calibration(
    light_frames: list[LightFrame],
    bias_frames: list[CalibrationFrame],
    dark_frames: list[CalibrationFrame],
    work_dir: str | Path,
    flat_frames: list[CalibrationFrame] | None = None,
    flat_policy: FlatPolicy = FlatPolicy.SKIP_IF_MISSING,
    pedestal: float = DEFAULT_PEDESTAL,
    subtract_bias: bool = False,
    dark_scaled: bool = False,
    notes: list[str] | None = None,
) -> CalibrationResult:
    """Orchestrate one (telescope, binning[, exptime]) calibration group.

    Bias/dark missing is always a hard error. Flats missing is governed by
    flat_policy: REQUIRE raises, SKIP_IF_MISSING proceeds without flat
    correction -- but flat_corrected on the result always says which
    actually happened, so it's never silently ambiguous downstream.

    Bias frames are still required (and a master bias is still built, since
    it is the correct thing to calibrate flats against), but by default they
    are NOT subtracted from the lights -- see calibrate_lights' docstring
    for the measurements behind that.

    `dark_scaled=True` is passed by the caller when `dark_frames` was
    selected (via `select_dark`) at an exposure time that doesn't match
    the lights' -- it forces `calibrate_lights`' `-opt=exp` dark-scaling
    path (which in turn forces bias subtraction, since `-opt` requires
    it) instead of today's dark-only path.
    """
    if not bias_frames:
        raise CalibrationFramesMissingError("No bias frames available; cannot calibrate.")
    if not dark_frames:
        raise CalibrationFramesMissingError("No dark frames available; cannot calibrate.")

    if not flat_frames and flat_policy == FlatPolicy.REQUIRE:
        raise CalibrationFramesMissingError(
            "No flat frames available and flat_policy=REQUIRE; "
            "pass flat_policy=SKIP_IF_MISSING to proceed without flat correction."
        )

    work_dir = Path(work_dir)
    master_bias = build_master_bias(bias_frames, work_dir)
    master_dark = build_master_dark(dark_frames, work_dir)

    master_flat: Path | None = None
    if flat_frames:
        master_flat = build_master_flat(flat_frames, work_dir)

    calibrated, _result = calibrate_lights(
        light_frames,
        master_bias,
        master_dark,
        work_dir,
        master_flat=master_flat,
        pedestal=pedestal,
        subtract_bias=subtract_bias,
        dark_optimize=dark_scaled,
        notes=notes,
    )

    return CalibrationResult(
        calibrated_lights=calibrated,
        master_bias=master_bias,
        master_dark=master_dark,
        master_flat=master_flat,
        flat_corrected=master_flat is not None,
        dark_scaled=dark_scaled,
    )
