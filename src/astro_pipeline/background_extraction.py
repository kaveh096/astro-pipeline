"""Stage 7 (and optional stage D1): GraXpert AI background extraction and
denoising, split out of background_color.py (Task 5 Step 16) -- see that
module's own docstring for the full background-extraction-vs-colour-
calibration ordering history and the NaN-tolerance invariant this module's
NaN-fill/corruption-check pattern exists to preserve. This half owns the
GraXpert binary and its two CLI commands; `color_calibration.py` owns the
separate SPCC half.

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

import subprocess
from pathlib import Path

import numpy as np
from astropy.io import fits

DEFAULT_GRAXPERT_CANDIDATES = [
    Path.home() / "AppData" / "Local" / "Programs" / "GraXpert" / "GraXpert.exe",
]


class BackgroundExtractionError(RuntimeError):
    pass


def find_graxpert() -> Path:
    for candidate in DEFAULT_GRAXPERT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"GraXpert.exe not found in known locations: {[str(c) for c in DEFAULT_GRAXPERT_CANDIDATES]}"
    )


def _nan_fill_for_graxpert(fits_path: Path, output_dir: Path) -> tuple[Path, np.ndarray]:
    """Shared NaN-fill safety step required before ANY GraXpert call
    (background-extraction and denoising both -- verified real for
    background-extraction: a frame that was 0.27% NaN, harmless edge
    slivers left by reprojecting one channel onto another's grid, produced
    100% NaN output, silently, exit code 0). Fills with the frame's own
    median; the filled pixels carry no real data either way, but keeping
    them NaN downstream preserves the honest "no data" marker instead of
    inventing background/detail there. Returns (path GraXpert should read,
    the original NaN mask) -- caller deletes the returned path if it
    differs from the input, and passes the mask to `restore_nan` handling.
    """
    source_data = fits.getdata(fits_path, memmap=False)
    nan_mask = ~np.isfinite(source_data)
    if not nan_mask.any():
        return fits_path, nan_mask
    data, header = fits.getdata(fits_path, header=True, memmap=False)
    fill_value = float(np.nanmedian(data))
    filled_path = output_dir / f"{fits_path.stem}__nanfilled.fit"
    fits.writeto(
        filled_path,
        np.where(nan_mask, fill_value, data).astype(np.float32),
        header=header,
        overwrite=True,
    )
    return filled_path, nan_mask


def _check_graxpert_output_not_corrupt(output_path: Path, tool_name: str, error_cls: type[Exception]) -> None:
    """Shared post-call safety check (background-extraction and denoising
    both): file existing is not sufficient -- verified real, twice, from
    two different causes for background-extraction: GraXpert exits 0 and
    writes a fully-formed FITS file that is 100% NaN, with no error beyond
    a "divide by zero" warning that also appears on healthy runs. (Causes
    seen so far: a background clipped to exact zero upstream, see
    calibration.py's pedestal; and any NaN at all in the input, handled by
    `_nan_fill_for_graxpert`.)
    """
    output_data = fits.getdata(output_path, memmap=False)
    nan_fraction = float(np.isnan(output_data).sum()) / output_data.size
    if nan_fraction > 0.5:
        raise error_cls(
            f"GraXpert {tool_name} produced {output_path.name} but {nan_fraction:.1%} of "
            "pixels are NaN -- treating this as a failure, not a degraded success."
        )


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

    filled_path, nan_mask = _nan_fill_for_graxpert(fits_path, output_dir)

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

    _check_graxpert_output_not_corrupt(output_path, "background-extraction", BackgroundExtractionError)

    # Optionally put the input's no-data regions back. Off by default,
    # because the immediate downstream consumer is colour calibration
    # (SPCC), and Siril's star photometry cannot handle NaN either --
    # restoring it here made colour calibration fail with "Error computing
    # FWHM for photometry settings adjustment", the same symptom that was
    # previously (and wrongly) read as evidence that background extraction
    # had to run *after* colour calibration.
    #
    # Siril tolerates NaN fine in stretching and compositing; it is
    # specifically photometry that cannot. So the no-data slivers stay
    # filled with background through colour calibration, and genuine NaN
    # reappears later when the colour image is reprojected onto L's grid.
    if restore_nan and nan_mask.any():
        data, header = fits.getdata(output_path, header=True, memmap=False)
        data = np.where(nan_mask, np.nan, data).astype(np.float32)
        fits.writeto(output_path, data, header=header, overwrite=True)

    return output_path


class DenoiseError(RuntimeError):
    """GraXpert's AI denoising step failed or produced corrupt output.

    Separate from `BackgroundExtractionError` even though both wrap the
    same GraXpert binary and the same NaN-safety pattern -- the two
    commands (`background-extraction` vs `denoising`) are functionally
    unrelated GraXpert operations, and conflating their errors would make
    a caller's `except` clause ambiguous about which real failure
    occurred.
    """


def run_graxpert_denoise(
    fits_path: str | Path,
    output_stem: str,
    graxpert_exe: Path | None = None,
    gpu: bool = True,
    timeout: float | None = None,
    restore_nan: bool = False,
) -> Path:
    """Run GraXpert's AI denoising (optional pipeline stage D1, 2026-09).

    GraXpert always appends '.fits' to whatever -output value is given --
    output_stem must be a bare stem (no extension); the real output path
    is '<output_stem>.fits'. Reuses the same NaN-fill-before/NaN-fraction-
    check-after safety pattern as `run_graxpert_background_extraction`
    (see `_nan_fill_for_graxpert`/`_check_graxpert_output_not_corrupt`) --
    GraXpert's silent-NaN-corruption behavior is a property of the
    GraXpert binary itself, not specific to the background-extraction
    command, so denoising gets no less protection.

    MEASURED, not assumed, against the real installed GraXpert 3.0.2 CLI
    on this machine: `-cmd denoising` does NOT expose `-smoothing`/
    `-correction`/`-bg` the way `-cmd background-extraction` does (those
    flags are listed generically in `-h` output across both commands, but
    denoising's own log output shows it reads "denoise strength" and
    "batch size" from GraXpert's own STORED preferences (last set via the
    GUI, or its own default of 0.5/4) -- there is no CLI flag to set
    denoise strength deterministically as of this version. Document this
    as a real limitation rather than guessing at an argument that does
    not exist: a caller wanting a specific denoise strength must set it
    once via GraXpert's GUI first; this function cannot override it.

    `gpu` defaults to True (matching `run_graxpert_background_extraction`
    and GraXpert's own default), but real-world GPU support varies by
    machine: on this session's own development machine, `-gpu true`
    (DirectML execution provider) crashed with an ONNX runtime error
    ("Non-zero status code... Exception(3)... Unspecified error") on the
    very first real denoise call. `-gpu false` (CPU) worked reliably but
    is SLOW -- a real measured 512x512 crop took ~4.5 minutes on CPU,
    implying a full 4096x4096 frame could take multiple hours. This is a
    genuine hardware/driver-compatibility tradeoff, not something this
    function can paper over: if `-gpu true` fails, retry with
    `gpu=False` and expect it to be slow, same as `find_graxpert()`-style
    tool problems are surfaced rather than silently worked around.
    `timeout` defaults to None (no timeout) for exactly this reason --
    unlike background-extraction's fast, resolution-independent single
    smooth-model fit (default timeout 300s), denoising's per-pixel AI
    inference time scales with resolution and hardware in a way no single
    default timeout could safely cover.
    """
    fits_path = Path(fits_path)
    exe = graxpert_exe or find_graxpert()
    output_dir = fits_path.parent
    output_path = output_dir / f"{output_stem}.fits"

    filled_path, nan_mask = _nan_fill_for_graxpert(fits_path, output_dir)
    # Read BEFORE the subprocess call (not after) and BEFORE filled_path is
    # deleted below -- needed for the post-call no-op check, since this is
    # exactly what filled_path looked like going INTO GraXpert.
    input_data_for_comparison = fits.getdata(filled_path, memmap=False)

    proc = subprocess.run(
        [
            str(exe),
            "-cli", "-cmd", "denoising",
            "-output", output_stem,
            "-gpu", "true" if gpu else "false",
            str(filled_path),
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if filled_path != fits_path:
        filled_path.unlink(missing_ok=True)

    if not output_path.exists():
        raise DenoiseError(
            f"GraXpert did not produce {output_path.name} (exit code {proc.returncode}). "
            "stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-20:])
        )

    _check_graxpert_output_not_corrupt(output_path, "denoising", DenoiseError)

    # Real, measured failure mode (2026-09): a real full-resolution
    # (4096x4096) denoise run against real NaN-containing input FED
    # DIRECTLY (bypassing this function's own NaN-fill, in a manual
    # diagnostic CLI test) exited 0, logged real progress 1%->99%, and
    # wrote a fully-formed, non-degenerate, valid-looking output file --
    # that was BYTE-IDENTICAL to the input. GraXpert silently no-op'd
    # after 2+ hours of apparent real compute. Re-verified through this
    # actual function (NaN-filled first) on a real-NaN-fraction-matched
    # synthetic image at 2048x2048: genuinely denoised correctly (real
    # std reduction, not identical) -- so this function's own NaN-fill
    # step is the likely real fix, but "valid output" and "not mostly
    # NaN" are BOTH insufficient checks on their own, exactly like the
    # NaN-corruption bug this module already guards against elsewhere.
    # Belt-and-suspenders: verify the output actually differs from what
    # went in, not just that it looks superficially fine.
    output_data = fits.getdata(output_path, memmap=False)
    if output_data.shape == input_data_for_comparison.shape and np.array_equal(
        output_data, input_data_for_comparison, equal_nan=True
    ):
        raise DenoiseError(
            f"GraXpert produced {output_path.name} but it is byte-identical to the input -- "
            "denoising silently did nothing (a real, confirmed GraXpert failure mode on "
            "large images, not a hypothetical). Treating as a failure, not a false success."
        )

    if restore_nan and nan_mask.any():
        data, header = fits.getdata(output_path, header=True, memmap=False)
        data = np.where(nan_mask, np.nan, data).astype(np.float32)
        fits.writeto(output_path, data, header=header, overwrite=True)

    return output_path
