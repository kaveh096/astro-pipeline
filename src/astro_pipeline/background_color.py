"""Stages 6-7: color calibration (PCC) and background extraction (GraXpert).

CORRECTED FINDING (supersedes an earlier, wrong conclusion from this same
investigation): **order does not matter**. PCC and GraXpert were both
re-tested, in both orders, on real data -- all four combinations succeed.
An earlier test run concluded "PCC must run before GraXpert" because PCC
failed on GraXpert's output, but that failure's real cause was a upstream
data-corruption bug (see calibration.py's `pedestal` parameter): Siril's
stack output was clipping a slightly-negative-on-average background to
exact 0.0, leaving >99.9% of the master exactly zero with only star peaks
nonzero. GraXpert's background model silently produced 100% NaN output on
that degenerate input (no error, no warning beyond a "divide by zero" that
misleadingly also appears on healthy runs) -- and PCC then, correctly,
failed to compute photometry on NaN garbage. Once the pedestal fix
resolved the root cause, PCC succeeded both before AND after GraXpert
(226 vs 225 stars used on the same real M51 composite -- also a large
quality improvement over the 19 stars PCC could find on the old
zero-clipped data). Background-extraction-first is kept as the *default*
order here only because it's the more conventional practice (cleaner
background for star photometry), not because the other order is broken.

SPCC vs PCC: SPCC (Gaia DR3 spectrophotometric) is Siril's more accurate
method, but needs a local Gaia photometric catalog (~20GB, chunked,
normally installed via Siril's GUI download manager). Without it, SPCC
falls back to an online catalog query that crashed outright (access
violation, 0xC0000005 -- reproduced twice, deterministically) partway
through aperture photometry on real data. PCC (NOMAD-based) works
reliably online with no large local catalog. **PCC is the default for
v1**; SPCC can be revisited if a local Gaia catalog is ever installed
(tracked as a backlog item -- Kaveh doesn't consider color accuracy a
priority for a hobby, so this is low urgency).

GraXpert quirk verified empirically: it always appends '.fits' to
whatever -output value is given, regardless of any extension already
present (e.g. '-output foo.fit' produces 'foo.fit.fits'). Always pass a
bare stem and expect '<stem>.fits'.

GraXpert silent-NaN-corruption quirk: GraXpert can exit 0 and write a
fully-formed, WCS-intact FITS file that is 100% (or partially) NaN, with
no error. run_graxpert_background_extraction() checks for this and raises
BackgroundExtractionError rather than reporting a false success -- this
class of bug is exactly why "file exists" was never a sufficient success
check (see the design review's original B3 finding, now proven, not just
theoretical).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from .siril_driver import SirilError, SirilResult, run_script

DEFAULT_GRAXPERT_CANDIDATES = [
    Path.home() / "AppData" / "Local" / "Programs" / "GraXpert" / "GraXpert.exe",
]


class ColorCalibrationError(RuntimeError):
    def __init__(self, message: str, result: SirilResult) -> None:
        super().__init__(message)
        self.result = result


class BackgroundExtractionError(RuntimeError):
    pass


@dataclass
class PCCResult:
    white_balance: tuple[float, float, float] | None
    stars_used: int | None
    log: SirilResult


def find_graxpert() -> Path:
    for candidate in DEFAULT_GRAXPERT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"GraXpert.exe not found in known locations: {[str(c) for c in DEFAULT_GRAXPERT_CANDIDATES]}"
    )


def _parse_pcc_result(result: SirilResult) -> PCCResult:
    text = "\n".join(result.log_lines)
    k_values: dict[int, float] = {}
    for match in re.finditer(r"^K(\d): ([\d.]+)", text, re.MULTILINE):
        k_values[int(match.group(1))] = float(match.group(2))
    white_balance = (
        (k_values[0], k_values[1], k_values[2]) if all(i in k_values for i in (0, 1, 2)) else None
    )

    stars_match = re.search(r"Found a solution for color calibration using (\d+) stars", text)
    stars_used = int(stars_match.group(1)) if stars_match else None

    return PCCResult(white_balance=white_balance, stars_used=stars_used, log=result)


def run_pcc(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    siril_cli: Path | None = None,
) -> PCCResult:
    """Run Siril's PCC on an RGB composite (already plate-solved). Verified
    to work correctly whether called before or after GraXpert background
    extraction on the same composite -- see module docstring. Raises
    ColorCalibrationError if Siril's script fails (e.g. too few usable
    stars) rather than silently reporting a null result.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)
    stem = rgb_composite_path.stem

    try:
        result = run_script(
            [f"load {stem}", "pcc", f"save {stem}"],
            workdir=work_dir,
            siril_cli=siril_cli,
        )
    except SirilError as exc:
        raise ColorCalibrationError(
            f"PCC failed on {rgb_composite_path.name}: {exc}", exc.result
        ) from exc

    return _parse_pcc_result(result)


def run_graxpert_background_extraction(
    fits_path: str | Path,
    output_stem: str,
    graxpert_exe: Path | None = None,
    smoothing: float = 0.0,
    correction: str = "Subtraction",
    gpu: bool = True,
    timeout: float | None = 300,
) -> Path:
    """Run GraXpert's AI background extraction. GraXpert always appends
    '.fits' to whatever -output value is given -- output_stem must be a
    bare stem (no extension); the real output path is '<output_stem>.fits'.
    """
    fits_path = Path(fits_path)
    exe = graxpert_exe or find_graxpert()
    output_dir = fits_path.parent
    output_path = output_dir / f"{output_stem}.fits"

    proc = subprocess.run(
        [
            str(exe),
            "-cli", "-cmd", "background-extraction",
            "-output", output_stem,
            "-smoothing", str(smoothing),
            "-correction", correction,
            "-gpu", "true" if gpu else "false",
            str(fits_path),
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if not output_path.exists():
        raise BackgroundExtractionError(
            f"GraXpert did not produce {output_path.name} (exit code {proc.returncode}). "
            "stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-20:])
        )

    # File existing is not sufficient -- verified real: GraXpert can exit 0
    # and write a fully-formed FITS file that is 100% NaN, with no error
    # beyond a "divide by zero" warning that also appears on healthy runs.
    # Root cause was upstream (calibration-stage background clipped to
    # exact zero by Siril's stack output, see calibration.py's pedestal
    # parameter) but this check exists so ANY future cause of the same
    # failure mode is caught here, not discovered accidentally three
    # stages later.
    output_data = fits.getdata(output_path)
    nan_fraction = float(np.isnan(output_data).sum()) / output_data.size
    if nan_fraction > 0.5:
        raise BackgroundExtractionError(
            f"GraXpert produced {output_path.name} but {nan_fraction:.1%} of pixels are NaN "
            "-- treating this as a failure, not a degraded success. Common cause: input "
            "background clipped to exact zero upstream (see calibration.py pedestal)."
        )
    return output_path


def calibrate_color_and_background(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    siril_cli: Path | None = None,
    graxpert_exe: Path | None = None,
) -> tuple[PCCResult, Path]:
    """Orchestrates Stages 6-7: GraXpert background extraction first, then
    PCC on the result. Both orders are verified to work (see module
    docstring); background-first is used here as the conventional default
    (cleaner background for star photometry), not because PCC-first is
    broken -- swap freely if there's a reason to.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)

    bg_output = run_graxpert_background_extraction(
        rgb_composite_path,
        output_stem=f"{rgb_composite_path.stem}_bg",
        graxpert_exe=graxpert_exe,
    )

    pcc_result = run_pcc(bg_output, work_dir, siril_cli=siril_cli)

    return pcc_result, bg_output
