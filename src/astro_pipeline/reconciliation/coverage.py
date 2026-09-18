"""Crop a set of same-grid images to the region all of them cover."""

from __future__ import annotations

import gc
from pathlib import Path

import numpy as np
from astropy.io import fits

from .reproject import ReprojectionError


def crop_to_common_coverage(paths: list[Path], output_dir: str | Path) -> list[Path]:
    """Crop a set of same-grid images to the region all of them cover.

    Aligning channels by reprojection leaves NaN slivers wherever one
    channel does not reach. Padding those instead (filling with a constant
    so downstream tools cope) is worse than it looks: the fill is visible to
    GraXpert's background model, which then fits a subtly wrong background
    over a much wider region than the sliver itself. On real data that
    produced a green excess of only 0.0002 in the reconciled colour image --
    invisible at that stage -- which the shadow-clipped stretch then
    amplified into a conspicuous green band across the bottom of the final
    composite. Restoring NaN after the fact does not help, because the
    damage is in the fitted background, not in the filled pixels.

    Cropping avoids the problem entirely: every remaining pixel has real
    data in every channel. The cost is a few pixels at the frame edge,
    which registration dithering has already made the least reliable part
    of the image.

    MEMORY: two passes over `paths`, never holding more than one
    contributor's array at a time -- the same "load, fold in, free" shape
    `combine_same_grid` already uses (see its own docstring). Verified on
    real data (two 4096x4096x3 float32 M51 contributors, ~201MB each):
    the earlier single-pass version, which loaded every contributor
    simultaneously into `arrays` and kept them all resident through both
    the mask computation and the output loop, peaked at 873MB working set
    -- O(N) in contributor count. This version's peak is dominated by one
    contributor's array plus the boolean mask, independent of N. Cost:
    each file's pixel data is read from disk twice (once per pass)
    instead of once; header reads are unchanged.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    valid: np.ndarray | None = None
    for p in paths:
        data = fits.getdata(Path(p), memmap=False)
        plane = np.all(np.isfinite(data), axis=0) if data.ndim == 3 else np.isfinite(data)
        valid = plane if valid is None else (valid & plane)
        del data
        gc.collect()
    if valid is None:
        raise ValueError("Need at least one path to crop.")

    # Trim greedily from whichever edge currently carries the most invalid
    # pixels, until the box is clean. A simpler "keep fully-valid rows and
    # columns" rule does not work here: reprojection applies a small
    # rotation, so the invalid region is a thin diagonal wedge and NO row
    # spans the full width validly -- that rule rejected every row and
    # cropped to nothing.
    ny, nx = valid.shape
    y0, y1, x0, x1 = 0, ny, 0, nx
    max_trim = 0.25  # refuse to eat more than a quarter of either axis
    while True:
        box = valid[y0:y1, x0:x1]
        if box.all():
            break
        if (y1 - y0) < ny * (1 - max_trim) or (x1 - x0) < nx * (1 - max_trim):
            raise ReprojectionError(
                "Channels overlap too poorly to crop to common coverage "
                f"(would need to discard more than {max_trim:.0%} of the frame)."
            )
        counts = {
            "top": int((~box[0, :]).sum()),
            "bottom": int((~box[-1, :]).sum()),
            "left": int((~box[:, 0]).sum()),
            "right": int((~box[:, -1]).sum()),
        }
        worst = max(counts, key=counts.get)
        if worst == "top":
            y0 += 1
        elif worst == "bottom":
            y1 -= 1
        elif worst == "left":
            x0 += 1
        else:
            x1 -= 1

    outputs: list[Path] = []
    for p in paths:
        path = Path(p)
        data = fits.getdata(path, memmap=False)
        if data.shape[-2:] != valid.shape:
            raise ReprojectionError(
                f"{path.name} changed shape between passes ({data.shape[-2:]} vs "
                f"{valid.shape} seen earlier) -- re-run crop_to_common_coverage."
            )
        header = fits.getheader(path).copy()
        cropped = data[..., y0:y1, x0:x1]
        # A crop moves the reference pixel; without this the WCS would
        # silently describe the wrong part of the sky.
        if "CRPIX1" in header:
            header["CRPIX1"] = float(header["CRPIX1"]) - x0
        if "CRPIX2" in header:
            header["CRPIX2"] = float(header["CRPIX2"]) - y0
        header["NAXIS1"], header["NAXIS2"] = cropped.shape[-1], cropped.shape[-2]
        out_path = output_dir / path.name
        fits.writeto(out_path, np.asarray(cropped, dtype=np.float32), header=header, overwrite=True)
        outputs.append(out_path)
        del data, cropped
        gc.collect()
    return outputs
