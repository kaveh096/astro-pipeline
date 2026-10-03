"""Tests for the post-denoise blend and auto black point (synthetic images)."""

import numpy as np
import pytest

from astro_pipeline.denoise_blend import (
    auto_black_point,
    blend_denoised,
    estimate_sky_level,
    structure_mask,
)


def _synthetic(sky: float = 0.25) -> np.ndarray:
    """(3, 128, 128): flat sky with a bright Gaussian blob in the middle."""
    y, x = np.mgrid[0:128, 0:128]
    blob = 0.6 * np.exp(-(((x - 64) ** 2 + (y - 64) ** 2) / (2 * 12.0**2)))
    return np.repeat((sky + blob)[None], 3, axis=0).astype(np.float32)


def test_estimate_sky_level_ignores_bright_structure() -> None:
    assert estimate_sky_level(_synthetic(0.25)) == pytest.approx(0.25, abs=0.01)


@pytest.mark.parametrize(
    "sky,target,expected",
    [(0.25, 0.10, 0.15 / 0.90), (0.25, 0.05, 0.20 / 0.95), (0.08, 0.10, 0.0), (0.10, 0.10, 0.0)],
)
def test_auto_black_point_maps_sky_to_target(sky: float, target: float, expected: float) -> None:
    bp = auto_black_point(sky, target)
    assert bp == pytest.approx(expected)
    if bp:
        assert (sky - bp) / (1 - bp) == pytest.approx(target)


def test_auto_black_point_rejects_bad_target() -> None:
    with pytest.raises(ValueError):
        auto_black_point(0.25, 1.0)


def test_mask_is_zero_on_sky_and_one_on_core() -> None:
    mask = structure_mask(_synthetic())
    assert mask[5, 5] == 0.0
    assert mask[64, 64] > 0.95


def test_blend_extremes_and_core_strength() -> None:
    original = _synthetic()
    denoised = original * 0.9
    full, _ = blend_denoised(original, denoised, 1.0, 1.0)
    none, _ = blend_denoised(original, denoised, 0.0, 0.0)
    np.testing.assert_allclose(full, denoised, atol=1e-6)
    np.testing.assert_allclose(none, original, atol=1e-6)

    out, strength = blend_denoised(original, denoised, 1.0, 0.75)
    assert strength[5, 5] == pytest.approx(1.0)
    assert strength[64, 64] == pytest.approx(0.75, abs=0.03)
    np.testing.assert_allclose(out[:, 5, 5], denoised[:, 5, 5], atol=1e-6)
    expected_core = original[:, 64, 64] + strength[64, 64] * (denoised[:, 64, 64] - original[:, 64, 64])
    np.testing.assert_allclose(out[:, 64, 64], expected_core, atol=1e-6)


def test_blend_takes_denoised_where_original_is_nan() -> None:
    original = _synthetic()
    denoised = original * 0.9
    original[:, 3, 3] = np.nan
    out, _ = blend_denoised(original, denoised, 1.0, 0.75)
    np.testing.assert_allclose(out[:, 3, 3], denoised[:, 3, 3])


@pytest.mark.parametrize("bg,core", [(1.2, 0.5), (0.5, -0.1)])
def test_blend_rejects_out_of_range_strengths(bg: float, core: float) -> None:
    img = _synthetic()
    with pytest.raises(ValueError):
        blend_denoised(img, img, bg, core)


def test_blend_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="shape"):
        blend_denoised(_synthetic(), np.zeros((3, 8, 8), dtype=np.float32))
