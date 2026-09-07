from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.reconciliation import (
    ReprojectionError,
    combine_same_grid,
    fit_gain,
    match_gain_offset,
    pick_finest_reference,
    pixel_scale_deg,
    reconcile_masters,
    reproject_to_reference,
)

# Real, already plate-solved masters built from the real M51/T24 delivery
# during development of this module (Luminance BIN1 4096x4096, Red BIN2
# 2048x2048, same field). Reused here rather than rebuilt per-test-run to
# avoid repeating the several-minutes calibrate->register->stack->solve
# chain, which is independently tested elsewhere (test_registration_stacking.py,
# test_solving.py).
from conftest import LUM_MASTER as REAL_LUM_MASTER
from conftest import RED_MASTER as REAL_RED_MASTER
from conftest import requires

requires_real_masters = requires(REAL_LUM_MASTER, REAL_RED_MASTER)


def make_solved_fits(tmp_path: Path, name: str, shape: tuple[int, int], pixel_scale_deg: float) -> Path:
    """Minimal synthetic FITS with a valid WCS, for testing pure logic
    (reference-picking, error paths) without needing real astro data."""
    path = tmp_path / name
    data = np.random.default_rng(0).normal(size=shape).astype(np.float32)
    header = fits.Header()
    header["NAXIS1"] = shape[1]
    header["NAXIS2"] = shape[0]
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRPIX1"] = shape[1] / 2
    header["CRPIX2"] = shape[0] / 2
    header["CRVAL1"] = 202.47
    header["CRVAL2"] = 47.19
    header["CDELT1"] = -pixel_scale_deg
    header["CDELT2"] = pixel_scale_deg
    fits.writeto(path, data, header=header, overwrite=True)
    return path


def test_pixel_scale_deg_matches_cdelt(tmp_path: Path) -> None:
    path = make_solved_fits(tmp_path, "a.fit", (100, 100), pixel_scale_deg=0.001)
    assert pixel_scale_deg(path) == pytest.approx(0.001, rel=1e-6)


def test_pixel_scale_deg_raises_without_wcs(tmp_path: Path) -> None:
    path = tmp_path / "no_wcs.fit"
    fits.writeto(path, np.zeros((10, 10), dtype=np.float32))
    with pytest.raises(ReprojectionError):
        pixel_scale_deg(path)


def test_pick_finest_reference_picks_smallest_pixel_scale(tmp_path: Path) -> None:
    coarse = make_solved_fits(tmp_path, "coarse.fit", (100, 100), pixel_scale_deg=0.002)
    fine = make_solved_fits(tmp_path, "fine.fit", (200, 200), pixel_scale_deg=0.001)
    assert pick_finest_reference([coarse, fine]) == fine


def test_reproject_to_reference_raises_without_wcs_on_source(tmp_path: Path) -> None:
    reference = make_solved_fits(tmp_path, "ref.fit", (100, 100), pixel_scale_deg=0.001)
    no_wcs = tmp_path / "no_wcs.fit"
    fits.writeto(no_wcs, np.zeros((50, 50), dtype=np.float32))

    with pytest.raises(ReprojectionError):
        reproject_to_reference(no_wcs, reference, tmp_path / "out.fit")


def test_reconcile_masters_requires_at_least_two(tmp_path: Path) -> None:
    only_one = make_solved_fits(tmp_path, "only.fit", (100, 100), pixel_scale_deg=0.001)
    with pytest.raises(ValueError):
        reconcile_masters([only_one], tmp_path)


# --- real end-to-end test: reproject a real Red/BIN2 master onto a real ----
# --- Luminance/BIN1 master's grid --------------------------------------


@requires_real_masters
def test_reproject_real_red_onto_real_luminance_grid(tmp_path: Path) -> None:
    import shutil

    lum = tmp_path / "master_lum.fit"
    red = tmp_path / "master_red.fit"
    shutil.copy2(REAL_LUM_MASTER, lum)
    shutil.copy2(REAL_RED_MASTER, red)

    lum_header = fits.getheader(lum)
    red_header = fits.getheader(red)
    assert lum_header["NAXIS1"] == 4096  # BIN1
    assert red_header["NAXIS1"] == 2048  # BIN2 -- genuinely different pixel scale

    result = reproject_to_reference(red, lum, tmp_path / "red_reconciled.fit")

    assert result.output_path.exists()
    out_data, out_header = fits.getdata(result.output_path, header=True)
    assert out_data.shape == (4096, 4096)  # now on L's grid
    assert out_header["NAXIS1"] == 4096

    # Most of the frame should genuinely overlap (same field, same target) --
    # not a full match (framing/rotation differ slightly), but high.
    assert result.footprint_mean > 0.9
    assert 0.0 < result.nan_fraction < 0.2

    # Real signal, not a degenerate/blank output.
    valid = out_data[~np.isnan(out_data)]
    assert valid.std() > 0


@requires_real_masters
def test_reconcile_masters_picks_luminance_as_reference(tmp_path: Path) -> None:
    import shutil

    lum = tmp_path / "master_lum.fit"
    red = tmp_path / "master_red.fit"
    shutil.copy2(REAL_LUM_MASTER, lum)
    shutil.copy2(REAL_RED_MASTER, red)

    reference, results = reconcile_masters([lum, red], tmp_path)

    assert reference == lum  # finer pixel scale, auto-picked
    assert red in results
    assert lum not in results
    assert results[red].output_path.exists()


def test_reprojection_aligns_masters_to_a_common_pointing(tmp_path: Path) -> None:
    """Regression guard for real colour fringing.

    Filters are registered independently, so their masters do not share a
    pointing -- measured on real data, Green sat 8.8px from Red and Blue
    4.4px, against a stellar FWHM of only ~5-9px. `rgbcomp` stacks channels
    without aligning them, so that offset became visible red/green fringing
    on every star. Reprojecting onto a common reference must collapse it.
    """
    import numpy as np
    from astropy.wcs import WCS

    def solved(name: str, crval1: float, crval2: float) -> Path:
        path = tmp_path / name
        data = np.zeros((128, 128), dtype=np.float32)
        data[60:68, 60:68] = 1.0
        header = fits.Header()
        header["CTYPE1"], header["CTYPE2"] = "RA---TAN", "DEC--TAN"
        header["CRPIX1"] = header["CRPIX2"] = 64.0
        header["CRVAL1"], header["CRVAL2"] = crval1, crval2
        header["CDELT1"], header["CDELT2"] = -0.001, 0.001
        fits.writeto(path, data, header=header, overwrite=True)
        return path

    # Same field, but the second master points 5 pixels (0.005 deg) away.
    reference = solved("ref.fit", 202.4696, 47.1953)
    offset = solved("offset.fit", 202.4696, 47.1953 + 0.005)

    def pixel_of_target(path: Path) -> tuple[float, float]:
        wcs = WCS(fits.getheader(path)).celestial
        x, y = wcs.world_to_pixel_values(202.4696, 47.1953)
        return float(x), float(y)

    before = np.hypot(*np.subtract(pixel_of_target(offset), pixel_of_target(reference)))
    assert before > 3, "fixture should start misaligned"

    aligned = tmp_path / "aligned.fit"
    reproject_to_reference(offset, reference, aligned)

    after = np.hypot(*np.subtract(pixel_of_target(aligned), pixel_of_target(reference)))
    assert after < 0.01, f"still misaligned by {after:.3f}px after reprojection"


def _wedge_fits(path: Path, wedge: float) -> Path:
    """Same-grid frame with a rotated wedge of NaN along one edge -- the
    shape reprojection actually produces, since it applies a small
    rotation."""
    data = np.ones((200, 200), dtype=np.float32)
    if wedge:
        ys, xs = np.mgrid[0:200, 0:200]
        data[(ys + xs * 0.05) < wedge] = np.nan
    header = fits.Header()
    header["CRPIX1"] = header["CRPIX2"] = 100.0
    fits.writeto(path, data, header=header, overwrite=True)
    return path


def test_crop_to_common_coverage_removes_all_nan(tmp_path: Path) -> None:
    """A first attempt kept only rows/columns that were valid across their
    full span. Because the invalid region is a diagonal wedge rather than a
    clean border, no row qualified and it cropped to nothing. The greedy
    edge trim must actually converge on a clean box."""
    from astro_pipeline.reconciliation import crop_to_common_coverage

    paths = [
        _wedge_fits(tmp_path / "a.fit", 0),
        _wedge_fits(tmp_path / "b.fit", 4),
        _wedge_fits(tmp_path / "c.fit", 8),
    ]
    outputs = crop_to_common_coverage(paths, tmp_path / "cropped")

    shapes = set()
    for out in outputs:
        data = fits.getdata(out, memmap=False)
        assert not np.isnan(data).any(), f"{out.name} still contains NaN"
        shapes.add(data.shape)
    assert len(shapes) == 1, "channels must stay dimensionally identical for rgbcomp"


def test_crop_to_common_coverage_updates_wcs_reference_pixel(tmp_path: Path) -> None:
    """Cropping moves the reference pixel; without updating CRPIX the WCS
    would silently describe the wrong part of the sky."""
    from astro_pipeline.reconciliation import crop_to_common_coverage

    paths = [_wedge_fits(tmp_path / "a.fit", 0), _wedge_fits(tmp_path / "b.fit", 8)]
    outputs = crop_to_common_coverage(paths, tmp_path / "cropped")

    original = fits.getheader(paths[0])
    cropped = fits.getheader(outputs[0])
    trimmed_rows = original["NAXIS2"] - fits.getdata(outputs[0], memmap=False).shape[0]
    assert trimmed_rows > 0
    assert cropped["CRPIX2"] == pytest.approx(original["CRPIX2"] - trimmed_rows)


def test_combine_same_grid_averages_with_weights(tmp_path: Path) -> None:
    from astro_pipeline.reconciliation import combine_same_grid

    a = tmp_path / "a.fit"
    b = tmp_path / "b.fit"
    fits.writeto(a, np.full((4, 4), 10.0, dtype=np.float32))
    fits.writeto(b, np.full((4, 4), 20.0, dtype=np.float32))

    out = combine_same_grid([a, b], tmp_path / "combined.fit")
    data = fits.getdata(out, memmap=False)
    assert np.allclose(data, 15.0)  # equal weights -> plain mean

    weighted = combine_same_grid([a, b], tmp_path / "weighted.fit", weights=[3.0, 1.0])
    wdata = fits.getdata(weighted, memmap=False)
    assert np.allclose(wdata, 12.5)  # (3*10 + 1*20) / 4


def test_combine_same_grid_is_nan_aware_per_pixel(tmp_path: Path) -> None:
    """Where only one contributor has real data (e.g. a smaller frame's
    edge), the average must use only the contributors that do -- a single
    NaN must not drag the whole pixel to NaN."""
    from astro_pipeline.reconciliation import combine_same_grid

    a = np.full((4, 4), 10.0, dtype=np.float32)
    a[0, 0] = np.nan
    b = np.full((4, 4), 20.0, dtype=np.float32)

    a_path, b_path = tmp_path / "a.fit", tmp_path / "b.fit"
    fits.writeto(a_path, a)
    fits.writeto(b_path, b)

    out = combine_same_grid([a_path, b_path], tmp_path / "combined.fit")
    data = fits.getdata(out, memmap=False)
    assert data[0, 0] == pytest.approx(20.0)  # only b contributed here
    assert data[1, 1] == pytest.approx(15.0)  # both contributed elsewhere


def test_combine_same_grid_nan_only_where_all_contributors_are_nan(tmp_path: Path) -> None:
    from astro_pipeline.reconciliation import combine_same_grid

    a = np.full((4, 4), np.nan, dtype=np.float32)
    a[0, 0] = 5.0
    b = np.full((4, 4), np.nan, dtype=np.float32)
    b[0, 0] = 7.0

    a_path, b_path = tmp_path / "a.fit", tmp_path / "b.fit"
    fits.writeto(a_path, a)
    fits.writeto(b_path, b)

    out = combine_same_grid([a_path, b_path], tmp_path / "combined.fit")
    data = fits.getdata(out, memmap=False)
    assert data[0, 0] == pytest.approx(6.0)
    assert np.isnan(data[1, 1])


def test_combine_same_grid_rejects_mismatched_shapes(tmp_path: Path) -> None:
    from astro_pipeline.reconciliation import combine_same_grid

    a_path, b_path = tmp_path / "a.fit", tmp_path / "b.fit"
    fits.writeto(a_path, np.zeros((4, 4), dtype=np.float32))
    fits.writeto(b_path, np.zeros((8, 8), dtype=np.float32))

    with pytest.raises(ReprojectionError):
        combine_same_grid([a_path, b_path], tmp_path / "combined.fit")


# --- Slice 3.4: gain + offset matching --------------------------------------


def _bright_structure(shape: tuple[int, int] = (200, 200)) -> np.ndarray:
    """A few Gaussian blobs on zero background -- stands in for real
    galaxy/star structure, at a fixed random seed so tests are
    deterministic. ~99% background/1% structure, matching the real data's
    proportions well enough to exercise the high-signal-only selection."""
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    structure = np.zeros(shape, dtype=np.float64)
    for cy, cx, amp, sigma in [(50, 50, 500.0, 8.0), (120, 140, 800.0, 6.0), (90, 160, 300.0, 10.0)]:
        structure += amp * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma**2)))
    return structure


def test_fit_gain_recovers_known_injected_gain() -> None:
    """The core claim of Slice 3.4: fit_gain must recover a KNOWN,
    injected multiplicative gain difference between two contributors,
    within a few percent -- this is the actual defect measured on real
    M51 data (gain(BIN1/BIN2) ~= 0.17-0.20, i.e. ~5x), reproduced here
    with a synthetic fixture with a known answer rather than trusted from
    real data alone."""
    rng = np.random.default_rng(1234)
    structure = _bright_structure()
    reference_background, contributor_background = 0.100, 0.085
    injected_gain = 5.4  # reference carries injected_gain x the contributor's flux
    noise_sigma = 0.002

    reference_plane = reference_background + structure + rng.normal(0, noise_sigma, structure.shape)
    contributor_plane = (
        contributor_background + structure / injected_gain + rng.normal(0, noise_sigma, structure.shape)
    )

    fit = fit_gain(reference_plane, contributor_plane)

    assert fit.gain == pytest.approx(injected_gain, rel=0.05)  # within a few percent
    assert fit.reference_background == pytest.approx(reference_background, abs=0.01)
    assert fit.contributor_background == pytest.approx(contributor_background, abs=0.01)
    assert fit.n_pixels > 0


def test_fit_gain_raises_with_too_few_high_signal_pixels() -> None:
    """Two flat, featureless (background-only) planes have nothing above
    the high-signal threshold -- must refuse to report a meaningless fit
    rather than divide-by-near-zero silently."""
    rng = np.random.default_rng(0)
    shape = (50, 50)
    reference_plane = 0.1 + rng.normal(0, 0.001, shape)
    contributor_plane = 0.09 + rng.normal(0, 0.001, shape)

    with pytest.raises(ReprojectionError):
        fit_gain(reference_plane, contributor_plane)


def test_fit_gain_sigma_clip_rejects_outliers() -> None:
    """A handful of injected outlier pixels (e.g. a cosmic ray in one
    contributor but not the other) must not drag the fitted gain far from
    the true value -- the 3-iteration sigma-clip is what's supposed to
    catch these."""
    rng = np.random.default_rng(7)
    structure = _bright_structure()
    injected_gain = 5.0
    noise_sigma = 0.002

    reference_plane = 0.1 + structure + rng.normal(0, noise_sigma, structure.shape)
    contributor_plane = 0.085 + structure / injected_gain + rng.normal(0, noise_sigma, structure.shape)

    # A small number of wildly discrepant pixels, on top of otherwise
    # clean high-signal structure.
    contributor_plane[48:52, 48:52] += 50.0

    fit = fit_gain(reference_plane, contributor_plane)
    assert fit.gain == pytest.approx(injected_gain, rel=0.05)


def test_match_gain_offset_applies_gain_and_offset_per_channel(tmp_path: Path) -> None:
    """End-to-end mechanics of Slice 3.4's `corrected = gain * (contributor
    - contributor_bkg) + reference_bkg`, over a 3-channel (RGB) contributor
    -- each channel independently, matching real rgbcomp output shape
    (channel axis first)."""
    rng = np.random.default_rng(99)
    structure = _bright_structure()
    injected_gains = [5.0, 4.5, 5.5]  # deliberately different per channel
    ref_bkgs = [0.10, 0.11, 0.09]
    contrib_bkgs = [0.085, 0.086, 0.084]

    reference = np.stack(
        [rb + structure + rng.normal(0, 0.002, structure.shape) for rb in ref_bkgs]
    ).astype(np.float32)
    contributor = np.stack(
        [
            cb + structure / g + rng.normal(0, 0.002, structure.shape)
            for cb, g in zip(contrib_bkgs, injected_gains)
        ]
    ).astype(np.float32)

    ref_path = tmp_path / "reference.fit"
    contrib_path = tmp_path / "contributor.fit"
    fits.writeto(ref_path, reference)
    fits.writeto(contrib_path, contributor, header=fits.Header({"FILTER": "test"}))

    out_path = tmp_path / "matched.fit"
    fit_results = match_gain_offset(contrib_path, ref_path, out_path)

    assert len(fit_results) == 3
    for channel, fit in enumerate(fit_results):
        assert fit.gain == pytest.approx(injected_gains[channel], rel=0.05)

    corrected, header = fits.getdata(out_path, header=True)
    # Corrected contributor should now sit close to the reference's own
    # scale/background over the same high-signal region used for the fit.
    for c in range(3):
        high_signal = (reference[c] - ref_bkgs[c]) > 10 * 0.002
        assert np.median(corrected[c][high_signal] - reference[c][high_signal]) == pytest.approx(0.0, abs=0.05)
    # Non-WCS metadata (e.g. FILTER) is preserved from the contributor, per
    # reproject_to_reference's own convention for "still that filter's
    # data, just corrected".
    assert header.get("FILTER") == "test"


def test_match_gain_offset_rejects_mismatched_shapes(tmp_path: Path) -> None:
    a_path = tmp_path / "a.fit"
    b_path = tmp_path / "b.fit"
    fits.writeto(a_path, np.zeros((3, 10, 10), dtype=np.float32))
    fits.writeto(b_path, np.zeros((3, 20, 20), dtype=np.float32))

    with pytest.raises(ReprojectionError):
        match_gain_offset(b_path, a_path, tmp_path / "out.fit")


# --- Slice 3.5/3.6: STACKCNT weighting, incremental accumulation, ----------
# --- reference-header selection --------------------------------------------


def test_combine_same_grid_incremental_matches_manual_weighted_mean_three_contributors(
    tmp_path: Path,
) -> None:
    """Generalizes the existing 2-contributor weighted-mean test to three,
    covering the incremental accumulation loop (Slice 3.6) rather than
    just the N=2 case np.stack would have handled the same way."""
    paths = []
    values = [10.0, 20.0, 40.0]
    for i, v in enumerate(values):
        p = tmp_path / f"c{i}.fit"
        fits.writeto(p, np.full((4, 4), v, dtype=np.float32))
        paths.append(p)

    weights = [1.0, 2.0, 3.0]
    out = combine_same_grid(paths, tmp_path / "combined.fit", weights=weights)
    data = fits.getdata(out, memmap=False)

    expected = sum(v * w for v, w in zip(values, weights)) / sum(weights)
    assert np.allclose(data, expected)


def test_combine_same_grid_reference_index_selects_header(tmp_path: Path) -> None:
    """Slice 3.6: the output header comes from `reference_index`, not
    always paths[0] -- verified here by a header keyword that differs per
    input, distinguishing which one the output actually inherited from.
    Default (unset) stays paths[0], preserving old behaviour for callers
    that don't care which header they get."""
    a_path, b_path = tmp_path / "a.fit", tmp_path / "b.fit"
    fits.writeto(a_path, np.full((4, 4), 10.0, dtype=np.float32), header=fits.Header({"SRC": "A"}))
    fits.writeto(b_path, np.full((4, 4), 20.0, dtype=np.float32), header=fits.Header({"SRC": "B"}))

    default_out = combine_same_grid([a_path, b_path], tmp_path / "default.fit")
    assert fits.getheader(default_out)["SRC"] == "A"

    keyed_out = combine_same_grid([a_path, b_path], tmp_path / "keyed.fit", reference_index=1)
    assert fits.getheader(keyed_out)["SRC"] == "B"


def test_combine_same_grid_rejects_out_of_range_reference_index(tmp_path: Path) -> None:
    a_path, b_path = tmp_path / "a.fit", tmp_path / "b.fit"
    fits.writeto(a_path, np.zeros((4, 4), dtype=np.float32))
    fits.writeto(b_path, np.zeros((4, 4), dtype=np.float32))

    with pytest.raises(ValueError):
        combine_same_grid([a_path, b_path], tmp_path / "out.fit", reference_index=2)


def test_combine_same_grid_stackcnt_weighting_favors_different_contributor_than_subcount(
    tmp_path: Path,
) -> None:
    """The actual claim Slice 3.5 exists to satisfy, using the REAL numbers
    measured on the M51 T24 data (see plan-rev4.md / handoff notes):
    jmwill's BIN1 RGB contributor has MORE raw subs (14+12+12=38) than
    kaveh's BIN2 (12+12+12=36), so sub_count weighting favours BIN1 --
    but kaveh's BIN2 has slightly MORE STACKCNT (10+10+9=29 vs
    9+9+9=27, i.e. more subs actually survived quality filtering into the
    stacked masters), so STACKCNT weighting favours BIN2 instead. The two
    schemes disagree on which contributor should dominate, and this test
    is exactly the case Slice 3.5 exists to get right (STACKCNT, not
    sub_count) -- not a synthetic worst case, the real data's own
    numbers."""
    bin1_path = tmp_path / "bin1.fit"  # jmwill, more raw subs, less STACKCNT
    bin2_path = tmp_path / "bin2.fit"  # kaveh, fewer raw subs, more STACKCNT
    fits.writeto(bin1_path, np.full((4, 4), 100.0, dtype=np.float32))
    fits.writeto(bin2_path, np.full((4, 4), 200.0, dtype=np.float32))

    sub_count_weights = [38.0, 36.0]
    stackcnt_weights = [27.0, 29.0]

    by_sub_count = fits.getdata(
        combine_same_grid([bin1_path, bin2_path], tmp_path / "by_sub_count.fit", weights=sub_count_weights),
        memmap=False,
    )
    by_stackcnt = fits.getdata(
        combine_same_grid([bin1_path, bin2_path], tmp_path / "by_stackcnt.fit", weights=stackcnt_weights),
        memmap=False,
    )

    midpoint = 150.0  # equal-weight average of 100 and 200
    # sub_count weighting pulls toward BIN1 (100, below the midpoint);
    # STACKCNT weighting pulls toward BIN2 (200, above it) -- a real,
    # measurable disagreement in which contributor dominates.
    assert by_sub_count.flat[0] < midpoint
    assert by_stackcnt.flat[0] > midpoint
