"""Stage 11: export a processed FITS to 16-bit TIFF (for Photoshop) and a
PNG preview (for looking at).

Three real correctness traps handled here explicitly, because getting any
of them wrong produces a file that opens fine and looks plausible while
being wrong:

1. **Row order.** FITS traditionally stores the first row as the BOTTOM of
   the image; TIFF/PNG treat the first row as the TOP. Siril records which
   convention a file uses in the ROWORDER keyword. Ignoring it yields a
   vertically mirrored image -- which on a star field can easily pass a
   casual glance, but is wrong (and on M51 specifically would put the
   companion galaxy NGC 5195 on the wrong side).
2. **Channel axis.** FITS colour data is channel-first, (3, ny, nx);
   TIFF/PNG want channel-last, (ny, nx, 3).
3. **NaN.** Reprojected data legitimately carries NaN outside the source
   frame's footprint (an honest "no data" marker, see reconciliation.py).
   NaN cannot be represented in an integer image, so it is mapped to black
   explicitly rather than left to whatever the int cast happens to do.

ICC profiles are deliberately NOT embedded by default. Siril's GHT output
is a custom perceptual curve that does not match sRGB's transfer curve, so
tagging the file "sRGB" would replace "Photoshop guesses" with "Photoshop
confidently applies a specific wrong transform" -- harder to notice, not
easier. Left untagged and documented; revisit once Stage 12 shows whether
an untagged file makes Photoshop CS6 raise a missing-profile dialog (which
would block COM automation).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile
from astropy.io import fits
from PIL import Image


class ExportError(RuntimeError):
    pass


@dataclass
class ExportResult:
    tiff_path: Path | None
    preview_path: Path | None
    shape: tuple[int, ...]
    nan_fraction: float
    clipped_low_fraction: float
    clipped_high_fraction: float
    row_order: str


def _orient_for_display(data: np.ndarray, header: fits.Header) -> tuple[np.ndarray, str]:
    """Return data with row 0 = top of image, plus the ROWORDER it came from.

    FITS's own default (and the traditional convention) is bottom-up, so an
    absent ROWORDER is treated as BOTTOM-UP and flipped. Siril writes this
    keyword explicitly on its outputs.
    """
    row_order = str(header.get("ROWORDER", "BOTTOM-UP")).strip().upper()
    if row_order == "TOP-DOWN":
        return data, row_order
    # BOTTOM-UP (or unknown/absent): flip the row axis, which is the last
    # -2 axis for both (ny, nx) and (c, ny, nx).
    return np.flip(data, axis=-2), row_order


def _to_channel_last(data: np.ndarray) -> np.ndarray:
    """(3, ny, nx) -> (ny, nx, 3); 2D mono passes through unchanged."""
    if data.ndim == 3:
        return np.moveaxis(data, 0, -1)
    return data


def _scale_to_uint(
    data: np.ndarray, bits: int, low: float, high: float
) -> tuple[np.ndarray, float, float]:
    """Map [low, high] onto the full integer range, reporting how much got
    clipped at each end so a silently blown-out export is visible in the
    result rather than only in the pixels."""
    finite = np.isfinite(data)
    clipped_low = float(np.sum(finite & (data < low))) / data.size
    clipped_high = float(np.sum(finite & (data > high))) / data.size

    span = high - low
    if span <= 0:
        raise ExportError(f"Invalid scaling range: low={low} high={high}")

    scaled = (data - low) / span
    scaled = np.clip(scaled, 0.0, 1.0)
    scaled = np.nan_to_num(scaled, nan=0.0, posinf=1.0, neginf=0.0)

    max_value = (1 << bits) - 1
    dtype = np.uint16 if bits == 16 else np.uint8
    return (scaled * max_value).round().astype(dtype), clipped_low, clipped_high


def export(
    fits_path: str | Path,
    output_dir: str | Path | None = None,
    stem: str | None = None,
    write_tiff: bool = True,
    write_preview: bool = True,
    preview_max_dim: int = 1400,
    low: float = 0.0,
    high: float = 1.0,
    embed_srgb_profile: bool = False,
) -> ExportResult:
    """Export a processed FITS image.

    `low`/`high` define the input range mapped onto the output range;
    defaults assume already-stretched data in [0, 1] (what Stage 8-9
    produces). The preview is a *faithful* rendering of the same data --
    deliberately not re-stretched to look nicer, because its purpose is
    judging whether the pipeline's own stretch was right. A preview that
    comes out too dark is real information, not a preview bug.
    """
    fits_path = Path(fits_path)
    output_dir = Path(output_dir) if output_dir else fits_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or fits_path.stem

    data, header = fits.getdata(fits_path, header=True, memmap=False)
    data = data.astype(np.float32)

    nan_fraction = float(np.isnan(data).sum()) / data.size
    data, row_order = _orient_for_display(data, header)
    data = _to_channel_last(data)

    tiff_path: Path | None = None
    preview_path: Path | None = None
    clipped_low = clipped_high = 0.0

    if write_tiff:
        pixels16, clipped_low, clipped_high = _scale_to_uint(data, 16, low, high)
        tiff_path = output_dir / f"{stem}.tif"
        kwargs: dict = {}
        if embed_srgb_profile:
            # Only meaningful if the data's transfer curve actually matches
            # sRGB -- see module docstring before enabling this.
            kwargs["photometric"] = "rgb" if pixels16.ndim == 3 else "minisblack"
        tifffile.imwrite(tiff_path, pixels16, **kwargs)

    if write_preview:
        preview = data
        # Downsample by simple striding -- cheap, and adequate for a
        # look-at-it preview on a RAM-constrained machine.
        step = max(1, int(np.ceil(max(preview.shape[:2]) / preview_max_dim)))
        if step > 1:
            preview = preview[::step, ::step]
        pixels8, _, _ = _scale_to_uint(preview, 8, low, high)
        preview_path = output_dir / f"{stem}_preview.png"
        # Pillow, not tifffile, for the PNG -- tifffile writes TIFF only and
        # would happily produce a TIFF named ".png".
        Image.fromarray(pixels8, mode="RGB" if pixels8.ndim == 3 else "L").save(preview_path)

    return ExportResult(
        tiff_path=tiff_path,
        preview_path=preview_path,
        shape=data.shape,
        nan_fraction=nan_fraction,
        clipped_low_fraction=clipped_low,
        clipped_high_fraction=clipped_high,
        row_order=row_order,
    )
