"""Stages 6-7: color calibration (PCC) and background extraction (GraXpert).

THE ACTUAL RULE, arrived at over three wrong turns: **PCC requires
NaN-free input.** Everything else about ordering follows from that, and
nothing else about ordering matters.

The wrong turns are worth recording, because each looked convincing:

  1. "PCC must run before GraXpert" -- concluded when PCC failed on
     GraXpert's output. The real cause was that GraXpert's output was 100%
     NaN (from a background clipped to exact zero upstream), and PCC was
     correctly refusing to do photometry on garbage.
  2. "Order does not matter" -- concluded after fixing that, when PCC then
     succeeded in both orders. True for that data, but only because it
     happened to be NaN-free by then.
  3. Both were symptoms of the same underlying constraint, which only
     became visible when NaN was reintroduced deliberately: PCC dies with
     "Error computing FWHM for photometry settings adjustment" the moment
     any NaN is present, at a fraction as small as 0.27%.

Siril tolerates NaN perfectly well in stretching and compositing. It is
specifically star photometry that cannot. So the invariant to preserve is
that whatever reaches PCC has no NaN in it -- see the nan-filling in
run_graxpert_background_extraction, and why `restore_nan` defaults to off.
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

from .siril_driver import SirilError, SirilResult, run_load_process_save

DEFAULT_GRAXPERT_CANDIDATES = [
    Path.home() / "AppData" / "Local" / "Programs" / "GraXpert" / "GraXpert.exe",
]


class ColorCalibrationError(RuntimeError):
    def __init__(self, message: str, result: SirilResult) -> None:
        super().__init__(message)
        self.result = result


class CatalogueUnavailableError(ColorCalibrationError):
    """PCC's online star catalogue could not be reached.

    Distinguished from a genuine colour-calibration failure because the fix
    is completely different: nothing is wrong with the data, the pipeline,
    or the parameters. Siril queries VizieR over the network for reference
    star photometry, and that server can be down, or can rate-limit a burst
    of requests -- seen for real as HTTP 403 after several pipeline reruns
    in quick succession. Retrying later usually works; installing Siril's
    local Gaia extract removes the dependency for good.
    """


_CATALOGUE_FAILURE_MARKERS = (
    "server unreachable",
    "unable to retrieve the remote catalogue",
    "catalog error, no stars identified",
    "cannot create catalogue file",
)


def _is_catalogue_unavailable(log_text: str) -> bool:
    lowered = log_text.lower()
    return any(marker in lowered for marker in _CATALOGUE_FAILURE_MARKERS)


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

    try:
        # Temp-save + replace rather than saving onto the loaded stem --
        # see run_load_process_save's docstring for why in-place saving is
        # unreliable in Siril.
        result = run_load_process_save(
            rgb_composite_path, ["pcc"], work_dir, siril_cli=siril_cli
        )
    except SirilError as exc:
        log_text = "\n".join(exc.result.log_lines) if exc.result else ""
        if _is_catalogue_unavailable(log_text):
            raise CatalogueUnavailableError(
                "PCC could not reach its online star catalogue (VizieR returned an "
                "error or was unreachable). This is an external outage or rate limit, "
                "not a problem with the data or the pipeline -- retrying later usually "
                "works. To remove the dependency entirely, install Siril's local Gaia "
                "extract via its Catalog_Installer.py script, which also enables SPCC.",
                exc.result,
            ) from exc
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
    restore_nan: bool = False,
) -> Path:
    """Run GraXpert's AI background extraction. GraXpert always appends
    '.fits' to whatever -output value is given -- output_stem must be a
    bare stem (no extension); the real output path is '<output_stem>.fits'.
    """
    fits_path = Path(fits_path)
    exe = graxpert_exe or find_graxpert()
    output_dir = fits_path.parent
    output_path = output_dir / f"{output_stem}.fits"

    # GraXpert cannot tolerate ANY NaN in its input: feeding it a frame that
    # was 0.27% NaN (harmless edge slivers left by reprojecting one channel
    # onto another's grid) produced 100% NaN output, silently, exit code 0.
    # So NaN is filled with the frame's own median before the call and
    # restored afterwards -- the filled pixels carry no real data either
    # way, but keeping them NaN downstream preserves the honest "no data"
    # marker instead of inventing background there.
    source_data = fits.getdata(fits_path, memmap=False)
    nan_mask = ~np.isfinite(source_data)
    filled_path = fits_path
    if nan_mask.any():
        data, header = fits.getdata(fits_path, header=True, memmap=False)
        fill_value = float(np.nanmedian(data))
        filled_path = output_dir / f"{fits_path.stem}__nanfilled.fit"
        fits.writeto(
            filled_path,
            np.where(nan_mask, fill_value, data).astype(np.float32),
            header=header,
            overwrite=True,
        )

    proc = subprocess.run(
        [
            str(exe),
            "-cli", "-cmd", "background-extraction",
            "-output", output_stem,
            "-smoothing", str(smoothing),
            "-correction", correction,
            "-gpu", "true" if gpu else "false",
            str(filled_path),
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        # Explicit encoding -- text=True alone uses the Windows locale
        # codec and can raise UnicodeDecodeError mid-run (verified real).
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if filled_path != fits_path:
        filled_path.unlink(missing_ok=True)

    if not output_path.exists():
        raise BackgroundExtractionError(
            f"GraXpert did not produce {output_path.name} (exit code {proc.returncode}). "
            "stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-20:])
        )

    # File existing is not sufficient -- verified real, twice, from two
    # different causes: GraXpert exits 0 and writes a fully-formed FITS file
    # that is 100% NaN, with no error beyond a "divide by zero" warning that
    # also appears on healthy runs. (Causes seen so far: a background
    # clipped to exact zero upstream, see calibration.py's pedestal; and any
    # NaN at all in the input, handled above.)
    output_data = fits.getdata(output_path, memmap=False)
    nan_fraction = float(np.isnan(output_data).sum()) / output_data.size
    if nan_fraction > 0.5:
        raise BackgroundExtractionError(
            f"GraXpert produced {output_path.name} but {nan_fraction:.1%} of pixels are NaN "
            "-- treating this as a failure, not a degraded success."
        )

    # Optionally put the input's no-data regions back. Off by default,
    # because the immediate downstream consumer is PCC, and Siril's star
    # photometry cannot handle NaN either -- restoring it here made PCC fail
    # with "Error computing FWHM for photometry settings adjustment", the
    # same symptom that was previously (and wrongly) read as evidence that
    # background extraction had to run *after* colour calibration.
    #
    # Siril tolerates NaN fine in stretching and compositing; it is
    # specifically photometry that cannot. So the no-data slivers stay
    # filled with background through PCC, and genuine NaN reappears later
    # when the colour image is reprojected onto L's grid.
    if restore_nan and nan_mask.any():
        data, header = fits.getdata(output_path, header=True, memmap=False)
        data = np.where(nan_mask, np.nan, data).astype(np.float32)
        fits.writeto(output_path, data, header=header, overwrite=True)

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
