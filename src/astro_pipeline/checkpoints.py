"""Stage checkpoints: a preview image plus semantic statistics.

The point of a checkpoint is to make a stage's output judgeable -- by a
human looking at it, and by comparison against the previous stage. This
exists because judging by numbers alone has already failed twice on this
project in ways no amount of extra numbers would have caught:

  * GraXpert silently produced 100% NaN output for an entire session while
    every check (file exists, WCS intact, exit code 0) passed.
  * A stretch default rendered an essentially black frame while passing
    NaN, clipping, spread and star-count checks.

So a checkpoint always produces something to LOOK at, and the statistics
are chosen to catch "ran fine, result is wrong" rather than "crashed".

Two preview modes, and the distinction matters:

  faithful   -- map [0,1] straight to the display. Used for already-stretched
                data. What you see is what the pipeline produced; if it looks
                too dark, the pipeline's stretch is too weak.
  autostretch -- apply a screen-transfer stretch for display only, never
                written back. Linear (pre-stretch) data is essentially black
                otherwise, so this is unavoidable -- but it means the preview
                flatters the data, and anything judged through it is being
                judged through a lie. Always labelled as such in the summary.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits

from .export_image import export

DEFAULT_SHADOWS_CLIP = -2.8
DEFAULT_TARGET_BACKGROUND = 0.25


@dataclass
class FrameStats:
    """Semantic statistics -- chosen to catch a wrong-but-plausible result."""

    shape: tuple[int, ...]
    channels: int
    nan_fraction: float
    zero_fraction: float
    background: float
    noise: float
    signal_to_noise: float
    saturated_fraction: float
    star_count: int | None = None
    median_fwhm: float | None = None
    pixel_scale_arcsec: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Checkpoint:
    label: str
    fits_path: str
    preview_path: str | None
    preview_mode: str
    stats: FrameStats
    warnings: list[str] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stats"] = self.stats.to_dict()
        return d

    def summary(self) -> str:
        s = self.stats
        lines = [f"[{self.label}]  {Path(self.fits_path).name}"]
        lines.append(
            f"  {s.channels}ch {s.shape[-2]}x{s.shape[-1]}  "
            f"background={s.background:.4f}  noise={s.noise:.2e}  SNR={s.signal_to_noise:.1f}"
        )
        detail = []
        if s.star_count is not None:
            detail.append(f"stars={s.star_count}")
        if s.median_fwhm is not None:
            detail.append(f"FWHM={s.median_fwhm:.2f}px")
        if s.pixel_scale_arcsec is not None:
            detail.append(f"scale={s.pixel_scale_arcsec:.2f}\"/px")
        if detail:
            lines.append("  " + "  ".join(detail))
        if s.nan_fraction or s.zero_fraction or s.saturated_fraction:
            lines.append(
                f"  NaN={s.nan_fraction:.1%}  zero={s.zero_fraction:.1%}  "
                f"saturated={s.saturated_fraction:.2%}"
            )
        for key, text in self.comparison.items():
            lines.append(f"  vs previous: {key} {text}")
        for w in self.warnings:
            lines.append(f"  !! {w}")
        if self.preview_path:
            note = " (display stretch applied -- linear data)" if self.preview_mode == "autostretch" else ""
            lines.append(f"  preview: {Path(self.preview_path).name}{note}")
        return "\n".join(lines)


# --- display stretch --------------------------------------------------------


def _mtf(x: np.ndarray, midtone: float) -> np.ndarray:
    """Midtone transfer function, the same shape Siril/PixInsight use for
    screen-transfer previews."""
    denominator = (2.0 * midtone - 1.0) * x - midtone
    with np.errstate(divide="ignore", invalid="ignore"):
        out = ((midtone - 1.0) * x) / denominator
    return np.nan_to_num(out, nan=0.0, posinf=1.0, neginf=0.0)


def autostretch_for_display(
    data: np.ndarray,
    shadows_clip: float = DEFAULT_SHADOWS_CLIP,
    target_background: float = DEFAULT_TARGET_BACKGROUND,
) -> np.ndarray:
    """Screen-transfer stretch, for DISPLAY ONLY -- never written back to
    the FITS. Places the background at `target_background` and clips
    shadows at `shadows_clip` MADs below the median, so linear data becomes
    visible without altering the pipeline's own data.
    """
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return np.zeros_like(data)

    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median))) * 1.4826
    if mad <= 0:
        mad = float(np.std(finite)) or 1e-8

    shadows = max(0.0, median + shadows_clip * mad)
    span = max(1e-8, float(finite.max()) - shadows)
    normalized = np.clip((data - shadows) / span, 0.0, 1.0)

    x0 = max(1e-8, min(1.0 - 1e-8, (median - shadows) / span))
    t = target_background
    # Midtone m satisfying mtf(x0, m) = t.
    midtone = float(np.clip(x0 * (1.0 - t) / (x0 - 2.0 * t * x0 + t), 1e-6, 1.0 - 1e-6))
    return _mtf(normalized, midtone)


# --- statistics -------------------------------------------------------------


def _estimate_noise(plane: np.ndarray) -> float:
    """Noise from the median absolute difference between adjacent rows --
    insensitive to the large-scale structure that dominates std()."""
    sample = plane[::4, ::4]
    diff = np.diff(sample, axis=0)
    finite = diff[np.isfinite(diff)]
    if finite.size == 0:
        return 0.0
    return float(np.median(np.abs(finite)) * 1.4826 / np.sqrt(2))


def _detect_stars(plane: np.ndarray, background: float, noise: float) -> tuple[int | None, float | None]:
    """Star count and median FWHM, via photutils. Returns (None, None) if
    detection is unavailable or finds nothing -- a checkpoint should degrade
    rather than fail."""
    try:
        from photutils.detection import DAOStarFinder
    except Exception:
        return None, None
    if noise <= 0:
        return None, None
    try:
        finder = DAOStarFinder(fwhm=4.0, threshold=5.0 * noise)
        found = finder(np.nan_to_num(plane - background, nan=0.0))
    except Exception:
        return None, None
    if found is None or len(found) == 0:
        return 0, None
    # DAOStarFinder reports sharpness/roundness rather than FWHM directly;
    # the fitted 'npix'-free proxy here is deliberately omitted rather than
    # invented, so FWHM stays None unless a real measurement is available.
    return int(len(found)), None


def _pixel_scale_arcsec(header: fits.Header) -> float | None:
    try:
        from astropy.wcs import WCS
        from astropy.wcs.utils import proj_plane_pixel_scales

        wcs = WCS(header).celestial
        if not wcs.has_celestial:
            return None
        return float(np.mean(proj_plane_pixel_scales(wcs)) * 3600.0)
    except Exception:
        return None


def frame_stats(fits_path: str | Path, detect_stars: bool = True) -> FrameStats:
    data, header = fits.getdata(Path(fits_path), header=True, memmap=False)
    data = np.asarray(data, dtype=np.float32)
    channels = data.shape[0] if data.ndim == 3 else 1
    plane = data.mean(axis=0) if data.ndim == 3 else data

    finite_mask = np.isfinite(data)
    finite = data[finite_mask]
    background = float(np.median(finite)) if finite.size else 0.0
    noise = _estimate_noise(plane)
    peak = float(finite.max()) if finite.size else 0.0

    star_count, fwhm = (None, None)
    if detect_stars:
        star_count, fwhm = _detect_stars(plane, background, noise)

    return FrameStats(
        shape=tuple(int(v) for v in data.shape),
        channels=channels,
        nan_fraction=float((~finite_mask).sum()) / data.size,
        zero_fraction=float((data == 0).sum()) / data.size,
        background=background,
        noise=noise,
        signal_to_noise=float((peak - background) / noise) if noise > 0 else 0.0,
        saturated_fraction=float((finite >= 0.999).sum()) / data.size if finite.size else 0.0,
        star_count=star_count,
        median_fwhm=fwhm,
        pixel_scale_arcsec=_pixel_scale_arcsec(header),
    )


def _warnings_for(stats: FrameStats) -> list[str]:
    """Flag the specific failure shapes this project has actually hit."""
    warnings: list[str] = []
    if stats.nan_fraction > 0.5:
        warnings.append(
            f"{stats.nan_fraction:.0%} NaN -- this is what silent GraXpert corruption looked like"
        )
    if stats.zero_fraction > 0.5:
        warnings.append(
            f"{stats.zero_fraction:.0%} of pixels are exactly zero -- background may have been "
            "clipped at stacking (see calibration.py pedestal)"
        )
    if stats.noise <= 0:
        warnings.append("no measurable noise -- data may be constant or degenerate")
    if stats.saturated_fraction > 0.2:
        warnings.append(f"{stats.saturated_fraction:.0%} of pixels at full scale -- likely over-stretched")
    if stats.star_count == 0:
        warnings.append("no stars detected -- registration and plate solving will fail on this")
    return warnings


def _compare(
    current: FrameStats,
    previous: FrameStats,
    comparable: bool = True,
) -> dict:
    """Report movement in the quantities that indicate a stage did
    something, or did something wrong.

    Only compares like with like. Percentage changes across a resolution
    change or across the linear/stretched boundary are meaningless and
    actively misleading -- an early version cheerfully reported
    "noise +167759%" between a stretched composite and the linear frame
    before it, and "stars 451 -> 2808" across a 2048->4096 reprojection
    where the star-detection threshold scales with a different noise
    floor. A checkpoint that prints confident nonsense is worse than one
    that stays quiet.
    """
    if previous.shape != current.shape:
        return {"shape": f"{previous.shape} -> {current.shape} (not otherwise comparable)"}
    if not comparable:
        return {"scale": "stretched vs linear -- absolute levels not comparable"}

    out: dict = {}
    if previous.background and current.background:
        change = (current.background - previous.background) / abs(previous.background)
        out["background"] = f"{change:+.1%}"
    if previous.noise and current.noise:
        out["noise"] = f"{(current.noise - previous.noise) / previous.noise:+.1%}"
    if previous.star_count and current.star_count is not None:
        out["stars"] = f"{previous.star_count} -> {current.star_count}"
    return out


# --- the checkpoint itself --------------------------------------------------


def checkpoint(
    fits_path: str | Path,
    label: str,
    output_dir: str | Path | None = None,
    linear: bool = True,
    previous: FrameStats | None = None,
    previous_linear: bool | None = None,
    preview_max_dim: int = 1200,
    detect_stars: bool = True,
) -> Checkpoint:
    """Produce a preview plus statistics for one stage output.

    `linear=True` (the default) means the data has not been stretched yet,
    so a display-only autostretch is applied to make it visible -- and the
    summary says so. Pass linear=False for post-stretch data, which is then
    rendered faithfully.
    """
    fits_path = Path(fits_path)
    output_dir = Path(output_dir) if output_dir else fits_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = frame_stats(fits_path, detect_stars=detect_stars)
    mode = "autostretch" if linear else "faithful"
    # Comparing a stretched frame against a linear one yields
    # meaningless ratios; treat unknown provenance as comparable only
    # when the caller says the previous stage matched this one.
    comparable = previous_linear is None or previous_linear == linear
    preview_path: Path | None = None

    try:
        if linear:
            data, header = fits.getdata(fits_path, header=True, memmap=False)
            stretched = autostretch_for_display(np.asarray(data, dtype=np.float32))
            tmp = output_dir / f"_preview_src_{label}.fit"
            fits.writeto(tmp, stretched.astype(np.float32), header=header, overwrite=True)
            result = export(
                tmp, output_dir=output_dir, stem=f"checkpoint_{label}",
                write_tiff=False, preview_max_dim=preview_max_dim,
            )
            tmp.unlink(missing_ok=True)
        else:
            result = export(
                fits_path, output_dir=output_dir, stem=f"checkpoint_{label}",
                write_tiff=False, preview_max_dim=preview_max_dim,
            )
        preview_path = result.preview_path
    except Exception as exc:  # a failed preview must not lose the statistics
        return Checkpoint(
            label=label, fits_path=str(fits_path), preview_path=None, preview_mode=mode,
            stats=stats, warnings=_warnings_for(stats) + [f"preview failed: {exc}"],
            comparison=_compare(stats, previous, comparable) if previous else {},
        )

    return Checkpoint(
        label=label,
        fits_path=str(fits_path),
        preview_path=str(preview_path) if preview_path else None,
        preview_mode=mode,
        stats=stats,
        warnings=_warnings_for(stats),
        comparison=_compare(stats, previous, comparable) if previous else {},
    )


def save_checkpoints(checkpoints: list[Checkpoint], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([c.to_dict() for c in checkpoints], indent=2), encoding="utf-8")
