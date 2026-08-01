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

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
from astropy.io import fits

from .ingest import CalibrationFrame, LightFrame
from .siril_driver import run_script
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


@dataclass
class CalibrationResult:
    calibrated_lights: list[Path]
    master_bias: Path
    master_dark: Path
    master_flat: Path | None
    flat_corrected: bool


def sequence_name(basename: str) -> str:
    """Siril's `convert <basename>` creates a sequence named `<basename>_`
    (verified against the real CLI) -- callers referencing the sequence in
    later commands must use this, not the bare basename."""
    return f"{basename}_"


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
) -> list[Path]:
    """Convert light_frames to a sequence and run Siril's `calibrate`
    against the given masters. Returns calibrated file paths (prefix
    "pp_"), sorted.

    Adds `pedestal` to every calibrated frame before returning (default
    0.1) -- see module docstring. Pass pedestal=0.0 to disable, but be
    aware downstream background extraction may then silently produce
    garbage rather than erroring (verified: no exception, no warning
    beyond a "divide by zero" that also appears on healthy runs).
    """
    if not light_frames:
        raise CalibrationFramesMissingError("No light frames provided to calibrate.")

    stage_dir = Path(work_dir) / basename
    stage_frames([f.path for f in light_frames], stage_dir)

    # Siril's -bias=/-dark=/-flat= want the path WITHOUT extension (it
    # searches for "<path>.[any_allowed_extension]" itself, same convention
    # as stack's -out=) -- verified empirically, confirmed by the exact
    # error message Siril gives when the extension is included.
    seq = sequence_name(basename)
    command = (
        f"calibrate {seq} -bias={master_bias.with_suffix('')} "
        f"-dark={master_dark.with_suffix('')} -cc=dark"
    )
    if master_flat is not None:
        command += f" -flat={master_flat.with_suffix('')}"
    command += " -prefix=pp_"

    run_script([f"convert {basename}", command], workdir=stage_dir)

    calibrated = sorted(stage_dir.glob(f"pp_{seq}*.fit*"))
    if not calibrated:
        raise RuntimeError(
            f"Siril reported success but no 'pp_{seq}*' calibrated files were found in {stage_dir}."
        )

    if pedestal:
        _apply_pedestal(calibrated, pedestal)

    return calibrated


def run_calibration(
    light_frames: list[LightFrame],
    bias_frames: list[CalibrationFrame],
    dark_frames: list[CalibrationFrame],
    work_dir: str | Path,
    flat_frames: list[CalibrationFrame] | None = None,
    flat_policy: FlatPolicy = FlatPolicy.SKIP_IF_MISSING,
    pedestal: float = DEFAULT_PEDESTAL,
) -> CalibrationResult:
    """Orchestrate one (telescope, binning[, exptime]) calibration group.

    Bias/dark missing is always a hard error. Flats missing is governed by
    flat_policy: REQUIRE raises, SKIP_IF_MISSING proceeds without flat
    correction -- but flat_corrected on the result always says which
    actually happened, so it's never silently ambiguous downstream.
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

    calibrated = calibrate_lights(
        light_frames, master_bias, master_dark, work_dir, master_flat=master_flat, pedestal=pedestal
    )

    return CalibrationResult(
        calibrated_lights=calibrated,
        master_bias=master_bias,
        master_dark=master_dark,
        master_flat=master_flat,
        flat_corrected=master_flat is not None,
    )
