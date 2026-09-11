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

Slice 3 of plan-flats-v3.md (2026-09) asked, and answered by measurement,
whether flat correction changes this picture -- built TWO real, registered,
stacked, plate-solved T21 Luminance masters (build_master()'s real output,
not a single calibrated sub) with pedestal=0.0 (i.e. the exact state this
mechanism exists to guard against), one flat-corrected (Slice 2's real
30-frame Luminance flat) and one not, plus reconstructed the pre-pedestal
state of a real, much-larger-N T24 Luminance master (17 stacked subs, no
flat ever built for T24) by subtracting the already-baked-in
DEFAULT_PEDESTAL=0.1 -- valid because that master has 0% exact-zero pixels,
i.e. no clipping had already occurred there to lose information:

    T21, NO flat, pedestal=0.0 (2 subs):   min=0.000 (clipped), 0.00053%
                                            exact-zero, p5=0.041
    T21, WITH flat, pedestal=0.0 (2 subs): min=0.000 (clipped), 0.00062%
                                            exact-zero, p5=0.052
    T24, no flat, pedestal=0.1 (17 subs),
    pre-pedestal reconstructed:            min=-0.0005, p5=0.0021

All three real cases still clip (or come within a hair of clipping) at
pedestal=0.0 -- flat correction does not remove the underlying need for a
pedestal, and does not meaningfully worsen it either: the flat-corrected
T21 master's margin was slightly *safer* (higher p5) than the non-flat one,
the opposite of what a single-sub probe (see build_master_flat's docstring,
fact 9) had flagged as a possible concern -- consistent with sigma-rejection
during stacking smoothing out the single-outlier-pixel effect that probe
saw. T24's much larger N did not put it further from zero either; its
reconstructed margin (p5=0.0021) was in fact the tightest of the three,
a reminder that "more subs" doesn't uniformly mean "safer" for this metric.
DEFAULT_PEDESTAL=0.1 comfortably covers every measured case (no case came
close to needing more, none showed 0.1 to be excessive) and is left
unchanged by this slice.
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


class CalibrationMode(str, Enum):
    """Per-telescope choice of how a light group reaches registration+
    stacking. RAW_LOCAL (today's only behaviour, unconditionally the
    default) locally bias/dark/flat-calibrates "raw"-provenance lights via
    run_calibration()/calibrate_lights(). PRECALIBRATED skips local
    calibration entirely and stages "calibrated"-provenance lights
    directly via stage_precalibrated_lights() -- for a delivery where
    iTelescope already did bias(+flat) server-side and no local Dark
    frames exist at all (real case: NGC 3628/T73, Feb 2025, CALSTAT='BF'
    confirmed on real headers -- see plan-precalibrated-path.md).
    """

    RAW_LOCAL = "raw_local"
    PRECALIBRATED = "precalibrated"


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


def stage_precalibrated_lights(
    light_frames: list[LightFrame],
    work_dir: str | Path,
    basename: str = "lights",
    pedestal: float = DEFAULT_PEDESTAL,
    notes: list[str] | None = None,
    debayer: bool = False,
    bayer_pattern: int = 0,
) -> list[Path]:
    """Stage already-calibrated (provenance="calibrated") lights directly
    into a "pp_"-prefixed Siril sequence, bypassing bias/dark/flat
    calibration entirely -- CalibrationMode.PRECALIBRATED's counterpart to
    calibrate_lights(). iTelescope's own server-side calibration already
    did bias+flat (never dark -- verified CALSTAT="BF" on real T73 NGC
    3628 data, see plan-precalibrated-path.md); re-running local flat
    correction here would double-correct, so this function has no
    flat_frames parameter at all, not an optional/None one -- there is
    structurally no path to flat logic to accidentally take.

    Converts directly under the "pp_<basename>" name (not "<basename>"
    then Siril-prefixed "pp_" the way calibrate_lights() does), so the
    resulting sequence is sequence_name(f"pp_{basename}") ==
    "pp_<basename>_" -- byte-identical to what pipeline.build_master()'s
    hardcoded register_and_stack("pp_lights_", ...) already expects when
    basename="lights". No change needed downstream of this function.

    MEASURED, not assumed (real T73 calibrated- Red frame, real Siril
    1.4.4, `convert` only -- no `calibrate` step): dtype survives as
    float32 end to end ("Saving FITS: ... 32 bits" in Siril's own log,
    independently confirmed via astropy as dtype('>f4')). Siril's
    `convert` normalizes pixel values into its internal [0, 1] float
    range regardless of input format ("Normalizing input data to our
    float range [0, 1]") -- this is NOT a risk specific to this function:
    calibrate_lights()'s own first step is the identical `convert
    <basename>` call (this module, ~line 546) before its `calibrate` step
    operates in that same normalized space, so both paths already share
    this behaviour. Real converted-output stats: min 0.00187, mean
    0.00278, max 1.011, exact-zero fraction 0.0 -- no clipping observed.

    Applies the same `pedestal` as calibrate_lights() (default 0.1), for
    the same reason: register_and_stack()'s underlying Siril `stack`
    clips a negative-average background to exact 0.0, a risk that depends
    on the STACK output, not on how the inputs were calibrated. Real T73
    samples have comfortably positive per-frame minimums, so clipping is
    unlikely, but stacked+averaged output was not verified before this
    function was written -- verify against a real multi-frame group before
    trusting a specific target's output. Kept as one pedestal mechanism
    for both paths rather than two, deliberately: a reader auditing "does
    pedestal apply here" should not have to check which calibration mode
    was used.

    `debayer`/`bayer_pattern` (RGB-only/OSC plan, 2026-09): for a one-shot-
    colour (OSC) delivery, each light is genuine undemosaiced Bayer-mosaic
    sensor data, not a mono frame -- verified on real T02 Abell 6 and HFG1
    data (2x2-phase periodicity test, corroborated by a leftover
    DeepSkyStacker config confirming the same RGGB pattern). MEASURED
    against the real installed Siril 1.4.4: `set debayer.use_bayer_header
    =false` + `set debayer.pattern={bayer_pattern}` + `convert ...
    -debayer` correctly demosaics a real frame into a genuine 3-plane
    (3, ny, nx) float32 FITS with plausible, non-degenerate per-channel
    stats. `use_bayer_header=false` is necessary because this data has no
    BAYERPAT header key at all (checked directly) -- the header-read
    default would find nothing to use. `bayer_pattern` is exposed as a
    parameter, not hardcoded to RGGB (0), mirroring this project's
    established never-guess-when-data-dependent convention (see
    resolve_instrument_profile's docstring philosophy) -- a future OSC
    telescope with a different real Bayer pattern only needs a different
    value passed in.
    """
    if not light_frames:
        raise CalibrationFramesMissingError("No light frames provided to stage.")
    stage_dir = Path(work_dir) / basename
    stage_frames([f.path for f in light_frames], stage_dir)
    convert_basename = f"pp_{basename}"
    seq = sequence_name(convert_basename)
    commands = []
    if debayer:
        commands += [
            "set debayer.use_bayer_header=false",
            f"set debayer.pattern={bayer_pattern}",
        ]
    commands.append(f"convert {convert_basename}" + (" -debayer" if debayer else ""))
    run_script(commands, workdir=stage_dir, script_name="convert.ssf")
    staged = sorted(stage_dir.glob(f"{seq}*.fit*"))
    if not staged:
        raise RuntimeError(f"Siril reported success but no '{seq}*' files were found in {stage_dir}.")
    if pedestal:
        _apply_pedestal(staged, pedestal)
    _log(f"       staged {len(staged)} precalibrated lights directly (no bias/dark/flat)", notes)
    return staged


def _calibrate_command(
    seq: str,
    dark_stem: str | None,
    bias_stem: str | None,
    flat_stem: str | None,
    dark_optimize: bool,
    prefix: str = "pp_",
) -> str:
    """Pure command-string builder, split out from calibrate_lights so the
    "nothing changes for existing T24 data" claim is checkable without
    invoking Siril: with dark_stem=<stem>, bias_stem=None, flat_stem=None,
    dark_optimize=False (today's only real path for lights), this must
    produce byte-identical output to before this function existed.

    Three real, distinct states of `dark_stem`/`dark_optimize` -- NOT two,
    with a bolted-on "dark_stem is optional" -- because a naive optional-
    dark_stem change alone still appends `-cc=dark` (it fires whenever
    `dark_optimize=False`, independent of whether a dark is even present)
    and would produce `calibrate <seq> -cc=dark -bias=<bias> -prefix=pp_`
    for a bias-only flat-calibration call, which is wrong (fact 8 of
    plan-flats-v3.md verified the real, working command has neither
    `-cc=dark` nor `-dark=`) and untested/undefined Siril behaviour besides:

    1. dark_stem set, dark_optimize=False (today's only real light-
       calibration path): `-dark=<stem> -cc=dark`.
    2. dark_stem set, dark_optimize=True (T21's scaled-dark path):
       `-dark=<stem> -opt=exp` (no `-cc=dark` -- its hot/cold-pixel
       thresholds are fit to the dark's own exposure time, meaningless on
       the scaled path).
    3. dark_stem=None ("no dark at all" -- build_master_flat's bias-only
       flat calibration): omits `-dark=` AND `-cc=dark` AND `-opt=exp`
       entirely, regardless of `dark_optimize` -- this state cannot be
       reached by varying `dark_optimize` alone, it is a genuine third
       branch. Verified real against Siril 1.4.4 (fact 8): `calibrate
       <seq> -bias=<bias_stem> -prefix=bc_` is exactly the command this
       state produces when bias_stem is given and prefix="bc_".

    `prefix` defaults to "pp_" (every existing caller's behaviour is
    unchanged unless it passes something else) -- build_master_flat passes
    prefix="bc_" so its own follow-on `stack bc_<seq>...` command actually
    finds the files this step just produced.
    """
    if dark_stem is not None:
        command = f"calibrate {seq} -dark={dark_stem}"
        if not dark_optimize:
            # -cc=dark's hot/cold-pixel thresholds are fit to the dark's own
            # exposure time -- meaningless (and not requested) on the scaled
            # path.
            command += " -cc=dark"
    else:
        command = f"calibrate {seq}"
    if bias_stem is not None:
        command += f" -bias={bias_stem}"
    if dark_stem is not None and dark_optimize:
        command += " -opt=exp"
    if flat_stem is not None:
        command += f" -flat={flat_stem}"
    command += f" -prefix={prefix}"
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


def build_master_flat(
    flat_frames: list[CalibrationFrame], master_bias: Path, work_dir: str | Path
) -> Path:
    """Bias-subtract each flat, then stack with rejection -- grounded in
    real measurements against T21's real 30-frame-per-filter sky flats
    (plan-flats-v3.md facts 7-9), not the plain build_master()-reuse path
    (which stacks raw flats with no calibration at all and structurally
    cannot bias-subtract).

    Bias subtraction is real and worth doing, even though T21's real bias
    level (~8.7% of the raw flat's own signal) is a small fraction here:
    measured on 10 real T21 Luminance BIN1 flats, bias-subtracting before
    stacking shifted the master's own corner/center vignetting ratio from
    0.7737 (not bias-subtracted, matches the un-bias-subtracted 30-frame
    number almost exactly) to 0.7565 -- ~1.7 percentage points (~7.5%
    relative), real, free (one extra `calibrate -bias=` call, no new
    machinery), and the correction will matter more for a shorter flat
    exposure or a noisier sensor, so it is not deferred.

    `calibrate <seq> -bias=<bias_stem> -prefix=bc_` (no `-dark=`, no
    `-cc=`) was verified real against the installed Siril 1.4.4: succeeded,
    "Sequence processing succeeded", and did a plain per-pixel subtraction
    -- a 10-frame bias-cal'd master's mean (0.3191) matched raw-mean-minus-
    bias-mean (0.3496 raw - 0.0305 bias = 0.3191) almost exactly, no
    surprise scaling. Stacked with `rej 3.0 3.0`, the same sigma-rejection
    parameters bias/dark masters already use -- this does not introduce a
    new, unreviewed stacking policy.

    CRITICAL ordering (the exact trap calibrate_lights() already hit once
    and documents in its own body -- see there): `convert` ingests every
    image present in the working directory, so `master_bias` must NOT be
    staged into the flat sequence's directory until AFTER `convert` has
    already ingested the flats -- staging it first would sweep it into the
    flat sequence itself as an extra "flat" frame (previously broke a
    LIGHT sequence exactly this way, with "Found 0 stars in reference").
    Hence three separate steps here, in this order: convert the flats,
    THEN stage the bias copy, THEN calibrate+stack.
    """
    if not flat_frames:
        raise CalibrationFramesMissingError("No frames provided to build master 'flat'.")

    basename = "flat"
    stage_dir = Path(work_dir) / basename
    stage_frames([f.path for f in flat_frames], stage_dir)

    seq = sequence_name(basename)
    run_script([f"convert {basename}"], workdir=stage_dir, script_name="convert.ssf")

    staged_bias = stage_dir / "masterbias.fit"
    shutil.copy2(master_bias, staged_bias)

    command = _calibrate_command(
        seq,
        dark_stem=None,
        bias_stem=staged_bias.stem,
        flat_stem=None,
        dark_optimize=False,
        prefix="bc_",
    )
    bias_calibrated_seq = f"bc_{seq}"
    run_script(
        [command, f"stack {bias_calibrated_seq} rej 3.0 3.0 -out=master"],
        workdir=stage_dir,
        script_name="calibrate.ssf",
    )
    master_path = stage_dir / "master.fit"
    if not master_path.exists():
        raise RuntimeError(f"Siril reported success but {master_path} was not created.")
    return master_path


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
        master_flat = build_master_flat(flat_frames, master_bias, work_dir)

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
