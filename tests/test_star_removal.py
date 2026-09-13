"""Tests for star_removal.py (capability C, 2026-09).

All fixtures here are SYNTHETIC, never real astronomical data -- this is
a real requirement of StarNet2's own bundled license (see
star_removal.py's module docstring), not just this project's usual
convention. Real, measured minimum input size for the installed StarNet2
2.6.0 CLI: 256x256 and 300x300 both failed with "Image size is too small
for the window!"; 600x600 works -- fixtures below use 600x600 for any
test that runs the real tool.
"""

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.star_removal import (
    StarNetError,
    find_starnet,
    run_star_removal,
)

try:
    STARNET_EXE = find_starnet()
    STARNET_AVAILABLE = True
except FileNotFoundError:
    STARNET_EXE = None
    STARNET_AVAILABLE = False

requires_starnet = pytest.mark.skipif(not STARNET_AVAILABLE, reason="StarNet2 not installed on this machine")


def synthetic_starfield(size: int = 600, n_stars: int = 20, seed: int = 0) -> np.ndarray:
    """A synthetic star field: gaussian background noise plus a handful of
    small bright gaussian 'stars' -- real, measured against the actual
    installed StarNet2 CLI to confirm it genuinely separates the two."""
    rng = np.random.default_rng(seed)
    data = rng.normal(0.1, 0.01, (size, size)).astype(np.float32)
    yy, xx = np.mgrid[0:size, 0:size]
    for _ in range(n_stars):
        cy, cx = rng.integers(20, size - 20), rng.integers(20, size - 20)
        r2 = (yy - cy) ** 2 + (xx - cx) ** 2
        data += 0.8 * np.exp(-r2 / 8.0)
    return np.clip(data, 0, 1).astype(np.float32)


@requires_starnet
def test_run_star_removal_actually_removes_stars(tmp_path: Path) -> None:
    """Regression guard for the plumbing actually working, not just
    producing files: the starless output's peak value must be far below
    the original's real star peaks, and the star-layer output must
    capture that removed signal instead."""
    data = synthetic_starfield()
    src = tmp_path / "starfield.fit"
    fits.writeto(src, data)

    result = run_star_removal(src, output_dir=tmp_path, output_stem="test", timeout=120)

    assert result.starless_path.exists() and result.stars_path.exists()
    starless = fits.getdata(result.starless_path, memmap=False)
    stars = fits.getdata(result.stars_path, memmap=False)

    assert starless.max() < 0.5 * data.max(), "starless output still contains bright star peaks"
    assert stars.max() > 0.5 * data.max(), "star layer did not capture the removed star signal"


@requires_starnet
def test_run_star_removal_raises_when_output_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.fit"
    with pytest.raises((StarNetError, FileNotFoundError, Exception)):
        run_star_removal(missing, output_dir=tmp_path, output_stem="whatever", timeout=30)


@requires_starnet
def test_run_star_removal_nan_fill_before_and_no_restore_by_default(tmp_path: Path) -> None:
    """Real, measured StarNet2 behavior differs from GraXpert's (it does
    not crash or produce all-NaN garbage on NaN input -- it silently
    treats NaN as 0.0). This module still NaN-fills before the call for
    consistency with the rest of the pipeline's external-tool call sites
    -- confirm the fill actually happens and, by default, is not
    restored afterward."""
    data = synthetic_starfield()
    data[0:10, 0:10] = np.nan
    src = tmp_path / "has_nan.fit"
    fits.writeto(src, data)

    result = run_star_removal(src, output_dir=tmp_path, output_stem="nantest", timeout=120)

    starless = fits.getdata(result.starless_path, memmap=False)
    assert not np.isnan(starless).any()
    # The NaN-filled temp copy must not be left behind.
    assert not (tmp_path / "has_nan__nanfilled.fit").exists()


@requires_starnet
def test_run_star_removal_restores_nan_when_requested(tmp_path: Path) -> None:
    data = synthetic_starfield()
    data[0:10, 0:10] = np.nan
    src = tmp_path / "has_nan2.fit"
    fits.writeto(src, data)

    result = run_star_removal(
        src, output_dir=tmp_path, output_stem="nantest2", timeout=120, restore_nan=True
    )

    starless = fits.getdata(result.starless_path, memmap=False)
    assert np.isnan(starless[0:10, 0:10]).all()


def test_run_star_removal_raises_when_files_missing_mocked(tmp_path: Path, monkeypatch) -> None:
    """Mocked (no real StarNet2 call): if the tool exits 0 but the
    expected output files simply aren't there, this must raise rather
    than return a false success."""
    import subprocess as subprocess_module

    class FakeCompletedProcess:
        returncode = 0
        stdout = "fake success, but no files written"

    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: FakeCompletedProcess())

    src = tmp_path / "input.fit"
    fits.writeto(src, np.ones((32, 32), dtype=np.float32))

    with pytest.raises(StarNetError, match="did not produce"):
        run_star_removal(src, output_dir=tmp_path, output_stem="fake", starnet_exe=Path("fake.exe"), timeout=30)


def test_find_starnet_raises_clear_error_when_not_found(monkeypatch) -> None:
    import astro_pipeline.star_removal as star_removal_module

    monkeypatch.setattr(star_removal_module, "DEFAULT_STARNET_CANDIDATES", [Path("Z:/does/not/exist.exe")])
    with pytest.raises(FileNotFoundError, match="starnet2.exe"):
        star_removal_module.find_starnet()
