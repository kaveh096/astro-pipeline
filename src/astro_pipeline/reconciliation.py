"""Stage 5: reconcile masters shot at different pixel scales/instruments/
users onto one common pixel grid via WCS-based reprojection.

This is the *only* mechanism used for pixel-grid reconciliation (no naive
fixed-ratio pixel-scale multiply -- that causes color-fringing from
binning-grid misalignment, per the design review). Multi-instrument and
multi-user combining both happen here, at the master level: each
instrument/user's data is independently calibrated, registered, and
stacked first (Stages 2-4), and only the resulting masters get reprojected
together -- never raw subs across instruments/users.

Verified against real data: a Luminance master (BIN1, 4096x4096) and a Red
master (BIN2, 2048x2048) of the same M51 field, both plate-solved.
Reprojecting the Red master onto the Luminance grid produced a correct
4096x4096 output with ~97.8% footprint coverage -- the ~2.2% outside the
Red frame's actual field of view comes back as NaN (an honest "no data"
marker), not a fabricated zero or extrapolated value.

IMPORTANT pipeline-ordering finding, also verified against real data:
reprojection must happen AFTER color calibration (PCC/SPCC), not before.
Running PCC on a reprojected RGB composite failed with the same error as
running it after GraXpert background extraction ("Error computing FWHM
for photometry settings adjustment") -- the NaN edge pixels and
interpolation artifacts introduced by reprojection break Siril's
photometry statistics the same way background-subtraction artifacts do.
The real pipeline order is: build R/G/B masters at native resolution ->
rgbcomp -> PCC (Stage 6/7) -> GraXpert -> THEN reproject the processed RGB
composite onto L's grid (this stage) -> GHT stretch both -> rgbcomp -lum=
(Stages 8-9). Multi-channel (already-composited RGB) input is supported
here specifically so reprojection can run this late in the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from reproject import reproject_interp


class ReprojectionError(RuntimeError):
    pass


@dataclass
class ReconciliationResult:
    source_path: Path
    output_path: Path
    footprint_min: float
    footprint_mean: float
    nan_fraction: float


def _require_wcs(path: Path) -> WCS:
    header = fits.getheader(path)
    wcs = WCS(header)
    if not wcs.has_celestial:
        raise ReprojectionError(f"{path.name} has no WCS solution -- plate-solve it first (Stage 3).")
    return wcs


def pixel_scale_deg(path: str | Path) -> float:
    """Mean pixel scale in degrees/pixel, via WCS (not raw CDELT/CD access,
    which doesn't account for rotation correctly)."""
    wcs = _require_wcs(Path(path))
    return float(np.mean(proj_plane_pixel_scales(wcs)))


def pick_finest_reference(master_paths: list[Path]) -> Path:
    """The master with the smallest pixel scale (most detail) -- typically
    the Luminance master -- is the natural reconciliation reference."""
    return min(master_paths, key=pixel_scale_deg)


def reproject_to_reference(
    source_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
) -> ReconciliationResult:
    """Resample source_path's data onto reference_path's exact pixel grid
    (shape + WCS). Both must already be plate-solved masters -- this is
    pixel-grid reconciliation across masters, not a substitute for
    calibration/registration.

    Output keeps source_path's non-WCS metadata (FILTER, EXPTIME, etc --
    it's still that filter's data, just resampled) but adopts the
    reference's WCS/shape.
    """
    source_path = Path(source_path)
    reference_path = Path(reference_path)
    output_path = Path(output_path)

    ref_header = fits.getheader(reference_path)
    ref_wcs = _require_wcs(reference_path)
    source_data, source_header = fits.getdata(source_path, header=True)
    source_wcs = _require_wcs(source_path)

    out_shape = (int(ref_header["NAXIS2"]), int(ref_header["NAXIS1"]))

    if source_data.ndim == 3:
        # Multi-channel (e.g. an already rgbcomp'd RGB image): reproject
        # each channel independently, since reproject_interp works on 2D
        # data. Channel axis is first (NAXIS3, C, ny, nx) per FITS/Siril
        # convention, verified against real rgbcomp output. The FITS
        # header's WCS is inherently 3D here (NAXIS=3 includes the channel
        # axis) -- must drop to the 2D celestial sub-WCS before reprojecting
        # each 2D channel slice, or reproject_interp rejects the dimension
        # mismatch (verified empirically).
        source_wcs_2d = source_wcs.celestial
        channels = []
        footprints = []
        for channel_data in source_data:
            reproj_channel, fp = reproject_interp((channel_data, source_wcs_2d), ref_wcs, shape_out=out_shape)
            channels.append(reproj_channel)
            footprints.append(fp)
        reprojected = np.stack(channels, axis=0)
        footprint = np.stack(footprints, axis=0)
    else:
        reprojected, footprint = reproject_interp((source_data, source_wcs), ref_wcs, shape_out=out_shape)

    output_header = source_header.copy()
    for key, value in ref_wcs.to_header().items():
        output_header[key] = value
    output_header["NAXIS1"] = out_shape[1]
    output_header["NAXIS2"] = out_shape[0]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, reprojected.astype(np.float32), header=output_header, overwrite=True)

    return ReconciliationResult(
        source_path=source_path,
        output_path=output_path,
        footprint_min=float(np.nanmin(footprint)),
        footprint_mean=float(np.nanmean(footprint)),
        nan_fraction=float(np.isnan(reprojected).sum()) / reprojected.size,
    )


def reconcile_masters(
    master_paths: list[Path],
    work_dir: str | Path,
    reference_path: Path | None = None,
) -> tuple[Path, dict[Path, ReconciliationResult]]:
    """Reproject every master in master_paths onto a common grid. Returns
    (reference_path, {source_path: ReconciliationResult}) -- the reference
    itself is excluded from the results dict since it's already on its own
    grid and needs no reprojection.
    """
    if len(master_paths) < 2:
        raise ValueError("Need at least two masters to reconcile.")
    if reference_path is None:
        reference_path = pick_finest_reference(master_paths)
    if reference_path not in master_paths:
        raise ValueError("reference_path must be one of master_paths.")

    work_dir = Path(work_dir)
    results: dict[Path, ReconciliationResult] = {}
    for path in master_paths:
        if path == reference_path:
            continue
        out_path = work_dir / f"reconciled_{path.stem}.fit"
        results[path] = reproject_to_reference(path, reference_path, out_path)
    return reference_path, results
