"""Optional post-denoise tuning: a brightness-aware blend of the GraXpert
output with the original, and an auto black point from the measured sky.

GraXpert's own strength is a plain linear blend, `orig + s * (D - orig)`
with D independent of s (verified against the real 3.0.2 CLI), so a
single full-strength run gives every weaker strength for free. That makes
a per-pixel strength cheap: `s(x) = bg*(1 - m(x)) + core*m(x)`, where
m(x) is a smooth 0..1 mask that is ~0 on sky/faint structure and ~1 on
the brightest structure (galaxy core, bright arms). Defaults (bg 1.0,
core 0.75) were chosen by eye on a noisy M51 stack: full strength on the
background, a little detail back where the source is brightest. Both are
user-tunable; re-running with different values reuses the expensive
GraXpert output.

The mask is defined relative to the image's own sky level and brightness
range (not absolute thresholds), so it transfers across targets, but it
is a brightness heuristic, not a segmentation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from scipy.ndimage import gaussian_filter

DEFAULT_BG_STRENGTH = 1.0
DEFAULT_CORE_STRENGTH = 0.75
DEFAULT_SKY_TARGET = 0.10

_SKY_QUANTILE = 25.0
_SKY_SUBSAMPLE = 4
_SKY_SMOOTH_SIGMA = 10.0
_MASK_SIGMA = 6.0
_MASK_LO_FRACTION = 0.25
_MASK_HI_PERCENTILE = 99.5
_MAX_BLACK_POINT = 0.95


@dataclass(frozen=True)
class BlendResult:
    path: Path
    sky_level: float
    mean_strength: float


def _luminance(data: np.ndarray) -> np.ndarray:
    return data.mean(axis=0) if data.ndim == 3 else data


def estimate_sky_level(data: np.ndarray) -> float:
    """Median luminance of the darkest quarter of the (smoothed) frame.

    Assumes at least a quarter of the frame is sky or faint background --
    true for galaxy and most nebula framings, not for a frame filled
    edge-to-edge with nebulosity (override the black point manually).
    """
    lum = _luminance(data)[::_SKY_SUBSAMPLE, ::_SKY_SUBSAMPLE]
    lum = np.nan_to_num(lum, nan=float(np.nanmedian(lum)) if np.isfinite(lum).any() else 0.0)
    smooth = gaussian_filter(lum, _SKY_SMOOTH_SIGMA)
    return float(np.median(lum[smooth <= np.percentile(smooth, _SKY_QUANTILE)]))


def auto_black_point(sky_level: float, sky_target: float = DEFAULT_SKY_TARGET) -> float:
    """Black point that maps `sky_level` onto `sky_target` under
    `(x - bp) / (1 - bp)`. 0.0 when the sky is already at/below target."""
    if not 0.0 < sky_target < 1.0:
        raise ValueError(f"sky_target must be in (0, 1) -- got {sky_target}")
    if sky_level <= sky_target:
        return 0.0
    return min((sky_level - sky_target) / (1.0 - sky_target), _MAX_BLACK_POINT)


def structure_mask(original: np.ndarray, sky_level: float | None = None) -> np.ndarray:
    """(H, W) float32 mask in [0, 1]: 0 on sky/faint structure, 1 on the
    brightest structure, smoothstep in between."""
    smooth = gaussian_filter(np.nan_to_num(_luminance(original)).astype(np.float32), _MASK_SIGMA)
    sky = estimate_sky_level(original) if sky_level is None else sky_level
    hi = float(np.percentile(smooth, _MASK_HI_PERCENTILE))
    lo = sky + _MASK_LO_FRACTION * (hi - sky)
    if hi - lo <= 1e-6:
        return np.zeros(smooth.shape, dtype=np.float32)
    m = np.clip((smooth - lo) / (hi - lo), 0.0, 1.0)
    return (m * m * (3.0 - 2.0 * m)).astype(np.float32)


def _check_strength(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1] -- got {value}")


def blend_denoised(
    original: np.ndarray,
    denoised: np.ndarray,
    bg_strength: float = DEFAULT_BG_STRENGTH,
    core_strength: float = DEFAULT_CORE_STRENGTH,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel blend of `denoised` (a full-strength result) into
    `original`. Returns (blended float32, per-pixel strength (H, W))."""
    _check_strength("bg_strength", bg_strength)
    _check_strength("core_strength", core_strength)
    if original.shape != denoised.shape:
        raise ValueError(f"shape mismatch: original {original.shape} vs denoised {denoised.shape}")
    mask = structure_mask(original)
    strength = (bg_strength * (1.0 - mask) + core_strength * mask).astype(np.float32)

    out = np.empty(original.shape, dtype=np.float32)
    orig_ch = original[None] if original.ndim == 2 else original
    den_ch = denoised[None] if denoised.ndim == 2 else denoised
    out_ch = out[None] if out.ndim == 2 else out
    for c in range(orig_ch.shape[0]):
        o, d = orig_ch[c], den_ch[c]
        out_ch[c] = np.where(np.isfinite(o), o + strength * (d - o), d)
    return out, strength


def blend_denoised_fits(
    original_path: str | Path,
    denoised_path: str | Path,
    out_path: str | Path,
    bg_strength: float = DEFAULT_BG_STRENGTH,
    core_strength: float = DEFAULT_CORE_STRENGTH,
) -> BlendResult:
    """FITS wrapper around `blend_denoised`; never modifies its inputs."""
    original = fits.getdata(original_path, memmap=False).astype(np.float32)
    denoised, header = fits.getdata(denoised_path, header=True, memmap=False)
    blended, strength = blend_denoised(original, denoised.astype(np.float32), bg_strength, core_strength)
    fits.writeto(out_path, blended, header=header, overwrite=True)
    return BlendResult(
        path=Path(out_path),
        sky_level=estimate_sky_level(blended),
        mean_strength=float(strength.mean()),
    )
