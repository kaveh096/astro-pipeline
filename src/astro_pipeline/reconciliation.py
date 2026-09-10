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
reprojection must happen AFTER colour calibration (SPCC), not before.
Running colour calibration on a reprojected RGB composite fails with the
same error as running it on GraXpert's raw NaN output ("Error computing
FWHM for photometry settings adjustment") -- the NaN edge pixels and
interpolation artifacts introduced by reprojection break Siril's
photometry statistics the same way background-subtraction artifacts do.
The real pipeline order is: build R/G/B masters at native resolution ->
rgbcomp -> GraXpert -> SPCC (Stages 6/7) -> THEN reproject the processed
RGB composite onto L's grid (this stage) -> GHT stretch both ->
rgbcomp -lum= (Stages 8-9). Multi-channel (already-composited RGB) input
is supported here specifically so reprojection can run this late in the
chain.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from reproject import reproject_interp

from .checkpoints import _estimate_noise


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

    # Memory-conscious by necessity: this runs on a machine with only ~8GB
    # total RAM and, in practice, as little as ~2.5GB free (confirmed via
    # Win32_OperatingSystem). reproject_interp works in float64 internally
    # regardless of input dtype, so a naive "reproject all 3 channels, then
    # np.stack" approach holds several 4096x4096 float64 arrays (channel +
    # footprint, x3) simultaneously -- verified to reliably trigger an
    # out-of-memory kill on this machine (repeatedly reproduced, at the
    # process level -- no Python exception, the process was just gone).
    # Fixed two ways together: each channel is reprojected, cast down to
    # float32, and freed before starting the next one; and reproject_interp
    # is called with block_size="auto" (processes the output in chunks
    # instead of allocating working memory for the whole array at once) --
    # per-channel float32 handling alone was NOT enough on its own (still
    # reproducibly killed); block_size="auto" was the fix that actually
    # worked, verified on the real 2048x2048-to-4096x4096 RGB reprojection.
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
        n_channels = source_data.shape[0]
        reprojected = np.empty((n_channels, out_shape[0], out_shape[1]), dtype=np.float32)
        footprint_min = np.inf
        footprint_sum = 0.0
        for i in range(n_channels):
            reproj_channel, fp = reproject_interp(
                (source_data[i], source_wcs_2d), ref_wcs, shape_out=out_shape,
                block_size="auto", parallel=False,
            )
            reprojected[i] = reproj_channel.astype(np.float32)
            footprint_min = min(footprint_min, float(np.nanmin(fp)))
            footprint_sum += float(np.nanmean(fp))
            del reproj_channel, fp
            gc.collect()
        footprint_mean = footprint_sum / n_channels
        nan_fraction = float(np.isnan(reprojected).sum()) / reprojected.size
    else:
        reprojected, footprint = reproject_interp(
            (source_data, source_wcs), ref_wcs, shape_out=out_shape,
            block_size="auto", parallel=False,
        )
        reprojected = reprojected.astype(np.float32)
        footprint_min = float(np.nanmin(footprint))
        footprint_mean = float(np.nanmean(footprint))
        nan_fraction = float(np.isnan(reprojected).sum()) / reprojected.size
        del footprint

    output_header = source_header.copy()
    for key, value in ref_wcs.to_header().items():
        output_header[key] = value
    output_header["NAXIS1"] = out_shape[1]
    output_header["NAXIS2"] = out_shape[0]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, reprojected, header=output_header, overwrite=True)

    return ReconciliationResult(
        source_path=source_path,
        output_path=output_path,
        footprint_min=footprint_min,
        footprint_mean=footprint_mean,
        nan_fraction=nan_fraction,
    )


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


# --- gain + offset matching (Slice 3.4) --------------------------------


@dataclass
class GainOffsetFit:
    """One channel's fitted correction from a contributor's flux scale
    onto a reference contributor's -- see fit_gain()'s docstring for the
    method and match_gain_offset() for how this gets applied."""

    channel: int
    gain: float
    reference_background: float
    contributor_background: float
    n_pixels: int


def _robust_background(plane: np.ndarray) -> float:
    finite = plane[np.isfinite(plane)]
    return float(np.median(finite)) if finite.size else 0.0


def fit_gain(
    reference_plane: np.ndarray,
    contributor_plane: np.ndarray,
    channel: int = 0,
    sigma_threshold: float = 50.0,
    clip_sigma: float = 3.0,
    clip_iterations: int = 3,
    min_pixels: int = 10,
) -> GainOffsetFit:
    """Fit a multiplicative gain from one contributor's flux scale onto a
    reference's, on high-signal pixels only.

    WHY GAIN, NOT A PEDESTAL OFFSET: measured directly on the real cropped
    M51 contributors (final/combined_crop/rgb_reconciled_contrib{0,1}.fit,
    BIN1 vs BIN2 RGB), a linear fit on high-signal pixels gives
    gain(BIN1/BIN2) ~= 0.17-0.20 across R/G/B -- i.e. one contributor
    carries ~5x the flux of the other at matched signal (expected: a BIN2
    pixel sums 4 photosites' worth of light). The background *offset*
    between them, by contrast, was a tiny, spatially uniform constant
    (+0.01552, identical across channels to 5 decimals) with no visible
    effect once averaged and stretched. An additive-only correction is
    therefore near a no-op against the real defect; only a multiplicative
    gain actually fixes it.

    WHY HIGH-SIGNAL PIXELS ONLY, NOT A FULL-FRAME FIT: ~99% of pixels in
    a background-extracted astro frame are noise-dominated background on
    both sides of the fit. An earlier full-frame attempt at this same fit
    failed for exactly that reason -- the noise floor dominates the sum
    and dilutes the signal from the ~1% of pixels (galaxy structure,
    stars) that actually carry the flux-scale information worth fitting.
    Restricting to pixels roughly `sigma_threshold` sigma above each
    side's own background (sigma from `checkpoints._estimate_noise`, the
    existing adjacent-pixel-difference estimator -- reused rather than
    reinvented) selects only bright structure, where the fit is well
    conditioned.

    WHY ZERO-INTERCEPT: both planes are background-subtracted before the
    fit (`a = reference_plane - reference_background`, `b =
    contributor_plane - contributor_background`), so `a ~= gain * b` by
    construction has no intercept term left to fit -- the offset is
    already handled separately, at the background level (see
    match_gain_offset's `corrected = gain * (contributor - contrib_bkg) +
    reference_bkg`), rather than fit jointly with the gain on background
    noise the way an earlier, rejected version of this design did.

    Least squares over `a ~= gain * b` gives `gain = sum(a*b) / sum(b*b)`,
    then `clip_iterations` passes of sigma-clipping on the residual `a -
    gain*b` (reject beyond `clip_sigma` residual-sigma, refit) reject
    outliers -- cosmic rays, a star saturated in one contributor but not
    the other, residual misalignment at a bright edge -- that survived
    into the high-signal selection.

    Raises ReprojectionError if fewer than `min_pixels` pixels clear the
    high-signal threshold on both sides (e.g. no field overlap, or a
    threshold too aggressive for this data) -- refusing to report a gain
    fit on close to zero points, rather than silently returning a
    meaningless number.
    """
    reference_background = _robust_background(reference_plane)
    contributor_background = _robust_background(contributor_plane)

    reference_noise = _estimate_noise(reference_plane)
    contributor_noise = _estimate_noise(contributor_plane)

    a = reference_plane - reference_background
    b = contributor_plane - contributor_background

    high_signal = np.isfinite(a) & np.isfinite(b)
    if reference_noise > 0:
        high_signal &= a > sigma_threshold * reference_noise
    if contributor_noise > 0:
        high_signal &= b > sigma_threshold * contributor_noise

    a_sel = a[high_signal].astype(np.float64)
    b_sel = b[high_signal].astype(np.float64)

    if a_sel.size < min_pixels:
        raise ReprojectionError(
            f"Only {a_sel.size} pixel(s) above {sigma_threshold}-sigma in both the "
            f"reference and this contributor (channel {channel}) -- need at least "
            f"{min_pixels} to fit a gain. Check field overlap or lower sigma_threshold."
        )

    gain = float(np.sum(a_sel * b_sel) / np.sum(b_sel * b_sel))
    for _ in range(clip_iterations):
        if a_sel.size < min_pixels:
            break
        residual = a_sel - gain * b_sel
        resid_std = float(np.std(residual))
        if resid_std <= 0:
            break
        keep = np.abs(residual) <= clip_sigma * resid_std
        if keep.all():
            break
        a_sel, b_sel = a_sel[keep], b_sel[keep]
        gain = float(np.sum(a_sel * b_sel) / np.sum(b_sel * b_sel))

    return GainOffsetFit(
        channel=channel,
        gain=gain,
        reference_background=reference_background,
        contributor_background=contributor_background,
        n_pixels=int(a_sel.size),
    )


def match_gain_offset(
    contributor_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
    **fit_kwargs,
) -> list[GainOffsetFit]:
    """Correct one contributor's flux scale AND background offset onto a
    reference contributor's, per channel, independently -- see fit_gain()
    for the method. `reference_path` and `contributor_path` must already
    share a pixel grid (same shape -- typically both are
    crop_to_common_coverage() outputs).

    Applies `corrected = gain * (contributor - contributor_bkg) +
    reference_bkg` per channel: this corrects both the flux scale and the
    background offset in one step, on the scale that actually matters
    (bright structure, via fit_gain's high-signal selection) rather than
    by fitting the offset separately on background noise.

    Does NOT touch the reference itself -- only ever call this for a
    contributor that is not the designated reference (see run_lrgb, which
    picks the highest-STACKCNT contributor as reference and leaves it
    unmodified).

    Does NOT fix the separate background-model gradient artifact
    documented on combine_same_grid (near a contributor's own field edge)
    -- a different mechanism: that one is a smooth intra-contributor
    background-modelling error, not a scale/offset mismatch BETWEEN
    contributors, and this gain/offset match operates on already-fixed
    background levels (the robust medians), not the gradient itself.
    """
    contributor_path = Path(contributor_path)
    reference_path = Path(reference_path)
    contributor_data, contributor_header = fits.getdata(contributor_path, header=True)
    reference_data = fits.getdata(reference_path, memmap=False)
    contributor_data = np.asarray(contributor_data, dtype=np.float32)
    reference_data = np.asarray(reference_data, dtype=np.float32)

    if contributor_data.shape != reference_data.shape:
        raise ReprojectionError(
            f"Cannot gain-match {contributor_path.name} ({contributor_data.shape}) "
            f"against {reference_path.name} ({reference_data.shape}) -- shapes must "
            "match (both should already be on the same grid)."
        )

    is_multichannel = contributor_data.ndim == 3
    n_channels = contributor_data.shape[0] if is_multichannel else 1
    corrected = np.empty_like(contributor_data)
    fits_results: list[GainOffsetFit] = []

    for c in range(n_channels):
        ref_plane = reference_data[c] if is_multichannel else reference_data
        contrib_plane = contributor_data[c] if is_multichannel else contributor_data
        result = fit_gain(ref_plane, contrib_plane, channel=c, **fit_kwargs)
        fits_results.append(result)
        corrected_plane = (
            result.gain * (contrib_plane - result.contributor_background)
            + result.reference_background
        )
        if is_multichannel:
            corrected[c] = corrected_plane
        else:
            corrected = corrected_plane

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, corrected.astype(np.float32), header=contributor_header, overwrite=True)
    return fits_results


def combine_same_grid(
    paths: list[Path],
    output_path: str | Path,
    weights: list[float] | None = None,
    reference_index: int = 0,
) -> Path:
    """Combine N images that already share an identical grid (same WCS,
    same shape -- typically the output of reproject_to_reference against a
    common reference) into one, via a NaN-aware weighted mean.

    This is how multiple contributors (e.g. two users' RGB at different
    binnings, each independently colour-calibrated and reprojected onto a
    common Luminance grid) get folded into one final channel: average
    rather than pick-one, so every contributor's signal counts. NaN-aware
    per pixel, not per frame -- wherever only some contributors have real
    data (edge regions outside a smaller frame's field of view), the
    average uses only those that do, rather than one NaN dragging the
    whole pixel to NaN. A pixel is NaN in the output only if it is NaN in
    every contributor.

    `weights` (default: equal) lets a contributor with more stacked subs
    count for more. As of Slice 3, callers pass STACKCNT (subs that
    actually survived quality filtering/rejection into the stacked
    master), not raw sub count -- an earlier design weighted by raw
    sub_count, and a design that came after it weighted by each
    contributor's own measured noise (inverse-variance); the latter was
    measured, on the real cropped contributors, to push weight ~2.7x
    TOWARD the wrong (lower-flux) contributor, because raw noise isn't
    comparable across un-gain-corrected flux scales -- the lower-flux
    contributor's absolute noise looks smaller only because its whole
    scale is compressed. STACKCNT is scale-free by construction and does
    not have that failure mode. This function itself does not know or
    care what its caller's weights mean; it just needs one float per path.

    THIS FUNCTION DOES NOT GAIN/OFFSET-MATCH ITS INPUTS -- that must
    already have happened (see match_gain_offset / fit_gain) before
    calling this. Averaging un-gain-matched contributors together is the
    ~5x flux-scale bug this slice fixes; this function's job stays
    narrowly the weighted average, so its NaN-aware-mean behaviour and
    tests stay stable independent of how the gain fit evolves.

    MEMORY, Slice 3.6: earlier, this built the full N-image stack via
    np.stack plus two full-size np.where temporaries -- roughly (N+3)x a
    single array's memory held simultaneously, all N contributors loaded
    at once. Replaced with a running `weighted_sum`/`weight_total`
    accumulated one contributor at a time (loaded, folded in, freed,
    before moving to the next) -- peak is now ~3x a single array's memory
    (the two running accumulators plus whichever one contributor is
    currently loaded), independent of N. crop_to_common_coverage(), which
    every caller runs first, was fixed the same way (two-pass, one
    contributor resident at a time) -- see its own docstring; the ~8GB-RAM
    machine's memory ceiling on this whole reconciliation path is no
    longer O(N) in contributor count at either stage.

    `reference_index` (default 0, i.e. paths[0] -- preserves the old
    behaviour for callers that don't care) picks which input's FITS header
    becomes the output's. Earlier this was always paths[0] positionally --
    with paths ordered by sorted(rgb_binnings) rather than any meaningful
    identity, "first" was accidental, not intentional (see pipeline.py's
    naming history). Slice 3 callers pass the index of the same
    highest-STACKCNT contributor used as the gain-fit reference (3.4), so
    the output's header actually comes from a deliberately chosen
    contributor rather than whichever happened to sort first.

    KNOWN RESIDUAL ARTIFACT, not fixed by cropping to common coverage
    first (crop_to_common_coverage): even where every contributor has
    genuinely valid data (0% NaN after cropping), a smooth background
    GRADIENT can remain near a smaller contributor's own field edge.
    Measured on real data: two contributors' linear backgrounds matched to
    within 0.1% through most of the frame, but diverged smoothly starting
    ~300px before one contributor's true edge (its Blue channel rising
    ~0.1% -> ~1% relative to Red over that span) -- small in absolute
    linear terms, but a shadow-clipped stretch operates right at the
    steepest part of its curve near the background level, so that gradient
    got amplified into a strong, clearly visible colour-shifted band along
    that edge in the final image. Root cause is almost certainly each
    contributor's independent GraXpert background-extraction being less
    reliable near ITS OWN frame boundary (a background model has less
    supporting data to fit near an edge than in the interior) -- which
    crop_to_common_coverage cannot detect, because it only distinguishes
    valid data from NaN, not confident background modelling from
    uncertain-but-technically-valid modelling near an edge. This is a
    DIFFERENT mechanism from the flux-scale mismatch match_gain_offset
    fixes (a per-contributor edge effect, not a whole-frame scale
    mismatch between contributors), and is NOT addressed by it.

    Not fixed here: the pragmatic mitigation is trimming an extra margin
    off the affected edge before stretching (standard practice for
    combined/mosaicked astro images anyway), which was not automated as of
    this writing -- final framing/cropping is Kaveh's own manual step in
    Photoshop, so the artifact sits inside the region he crops away
    regardless. Worth automating if this pipeline ever needs to hand off a
    combined multi-contributor frame somewhere the edges matter.
    """
    paths = [Path(p) for p in paths]
    if len(paths) < 2:
        raise ValueError("Need at least two images to combine.")
    if weights is None:
        weights = [1.0] * len(paths)
    if len(weights) != len(paths):
        raise ValueError("weights must have the same length as paths.")
    if not 0 <= reference_index < len(paths):
        raise ValueError(f"reference_index {reference_index} out of range for {len(paths)} paths.")

    weighted_sum: np.ndarray | None = None
    weight_total: np.ndarray | None = None
    reference_shape: tuple[int, ...] | None = None
    for path, weight in zip(paths, weights):
        data = fits.getdata(path, memmap=False).astype(np.float32)
        if reference_shape is None:
            reference_shape = data.shape
        elif data.shape != reference_shape:
            raise ReprojectionError(
                f"Cannot combine images of different shapes: {reference_shape} vs "
                f"{data.shape} ({path.name})"
            )
        valid = np.isfinite(data)
        contribution = np.where(valid, data * weight, 0.0)
        weight_contribution = np.where(valid, weight, 0.0).astype(np.float32)
        if weighted_sum is None:
            weighted_sum = contribution
            weight_total = weight_contribution
        else:
            weighted_sum += contribution
            weight_total += weight_contribution
        del data, valid, contribution, weight_contribution

    with np.errstate(invalid="ignore", divide="ignore"):
        combined = weighted_sum / weight_total
    combined[weight_total == 0] = np.nan

    header = fits.getheader(paths[reference_index])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, combined.astype(np.float32), header=header, overwrite=True)
    return output_path
