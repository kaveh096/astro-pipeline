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
SCRATCH_DIR = Path(
    r"C:\Users\Kaveh\AppData\Local\Temp\claude\C--dev-astro-pipeline"
    r"\030735e3-fa3c-4002-a37d-180b9f73ef2a\scratchpad\recon_test"
)
REAL_LUM_MASTER = SCRATCH_DIR / "lum" / "lights" / "master_lum.fit"
REAL_RED_MASTER = SCRATCH_DIR / "red" / "lights" / "master_red.fit"
requires_real_masters = pytest.mark.skipif(
    not (REAL_LUM_MASTER.exists() and REAL_RED_MASTER.exists()),
    reason="Real solved masters not present on this machine (see recon_test scratch dir)",
)


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
