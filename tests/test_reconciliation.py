from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.reconciliation import (
    ReprojectionError,
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
