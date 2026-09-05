import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.siril_driver import find_siril_cli
from astro_pipeline.stretch_compose import (
    _ght_command,
    ght_stretch,
    rgbcomp_lum,
    stretch_and_compose,
)

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")

# Real, dimension-matched, plate-solved, GraXpert-background-
# extracted fixtures. Using background-EXTRACTED data matters here, not
# just any real data: a raw calibrated master's background is almost
# uniformly at the pedestal level (see calibration.py) with only rare star
# pixels differing, so >99% of pixels cluster at nearly the same value and
# GHT stretch legitimately has little to work with -- that's not a stretch
# bug, it's an unrepresentative fixture (verified: this was tried first and
# every real-data assertion failed for exactly this reason). Real pipeline
# usage always stretches post-GraXpert data, never a raw calibrated master.
# The luminance fixture is the real L channel and the colour fixture has
# been reprojected onto its grid, so the two share dimensions as
# rgbcomp -lum= requires.
from conftest import LUM_BG as REAL_MONO
from conftest import RGB_RECONCILED as REAL_RGB
from conftest import requires

requires_real_fixtures = requires(REAL_MONO, REAL_RGB)


def assert_non_degenerate_stretch(data: np.ndarray) -> None:
    """The real bug this guards against: a too-aggressive stretch crushed
    an entire real composite into the top ~1.5% of the value range
    (median 0.988, essentially blown-out white). Note percentile-spread
    (e.g. p1-p99) is NOT a reliable check here and was tried first --
    real star fields are legitimately >99.99% background with dynamic
    range concentrated in a tiny fraction of star pixels (verified: one
    real fixture had max=0.73 but only 0.002% of pixels above 0.2), so a
    percentile-spread threshold flags real, healthy data as "degenerate."
    max value and near-white fraction are the checks that actually target
    the observed failure mode.
    """
    valid = data[~np.isnan(data)]
    assert valid.std() > 0
    assert valid.max() > 0.15, f"no real bright signal found (max={valid.max():.4f})"


def test_ght_command_includes_mandatory_strength() -> None:
    cmd = _ght_command(0.5, 0.0, 0.0, 0.0, 1.0, weighting=None, channels=None)
    assert cmd.startswith("ght -D=0.5")
    assert "-B=0.0" in cmd
    assert "-HP=1.0" in cmd


def test_ght_command_includes_weighting_when_given() -> None:
    cmd = _ght_command(0.5, 0.0, 0.0, 0.0, 1.0, weighting="human", channels=None)
    assert cmd.endswith("-human")


def test_ght_command_omits_weighting_when_none() -> None:
    cmd = _ght_command(0.5, 0.0, 0.0, 0.0, 1.0, weighting=None, channels=None)
    assert "-human" not in cmd and "-even" not in cmd and "-independent" not in cmd


def test_ght_command_includes_channels_when_given() -> None:
    cmd = _ght_command(0.5, 0.0, 0.0, 0.0, 1.0, weighting=None, channels="RG")
    assert cmd.endswith(" RG")


# --- real end-to-end tests --------------------------------------------------


@requires_siril
@requires_real_fixtures
def test_ght_stretch_real_mono_produces_non_degenerate_output(tmp_path: Path) -> None:
    staged = tmp_path / "mono.fit"
    shutil.copy2(REAL_MONO, staged)
    before = fits.getdata(staged, memmap=False)

    ght_stretch(staged, tmp_path, strength=0.5, weighting=None)

    after = fits.getdata(staged)
    assert after.shape == before.shape
    assert not np.allclose(before, after)  # stretch actually changed something
    assert_non_degenerate_stretch(after)


@requires_siril
@requires_real_fixtures
def test_rgbcomp_lum_real_data_matching_dimensions(tmp_path: Path) -> None:
    lum = tmp_path / "lum.fit"
    rgb = tmp_path / "rgb.fit"
    shutil.copy2(REAL_MONO, lum)
    shutil.copy2(REAL_RGB, rgb)

    # Siril's LRGB workflow expects pre-stretched inputs (see module docstring).
    ght_stretch(lum, tmp_path, strength=0.5, weighting=None)
    ght_stretch(rgb, tmp_path, strength=0.5, weighting="human")

    output_path, result = rgbcomp_lum(lum, rgb, "composed", tmp_path)

    assert output_path.exists()
    data, header = fits.getdata(output_path, header=True)
    # Shape follows the luminance reference grid rather than a hardcoded
    # size -- the fixtures moved from 2048x2048 to 4096x4096 when they
    # became real pipeline outputs, and a magic number silently rotted.
    assert data.shape == (3, *fits.getdata(lum, memmap=False).shape[-2:])
    assert header.get("PLTSOLVD") is True
    assert np.isnan(data).sum() / data.size < 0.05
    assert_non_degenerate_stretch(data)


@requires_siril
@requires_real_fixtures
def test_stretch_and_compose_real_data_default_method(tmp_path: Path) -> None:
    """End-to-end guard on the default stretch. Two real failures inform
    this: a hand-picked GHT strength of 3.0-4.0 crushed everything into
    near-white, and a strength of 0.5 produced a nearly black frame that
    still passed every numeric check. The default is now `autostretch`,
    which derives its parameters from the data."""
    lum = tmp_path / "lum.fit"
    rgb = tmp_path / "rgb.fit"
    shutil.copy2(REAL_MONO, lum)
    shutil.copy2(REAL_RGB, rgb)

    result = stretch_and_compose(lum, rgb, tmp_path, output_stem="final")

    assert result.composite_path.exists()
    data = fits.getdata(result.composite_path)
    # Shape follows the luminance reference grid rather than a hardcoded
    # size -- the fixtures moved from 2048x2048 to 4096x4096 when they
    # became real pipeline outputs, and a magic number silently rotted.
    assert data.shape == (3, *fits.getdata(lum, memmap=False).shape[-2:])
    assert_non_degenerate_stretch(data)

    # Specifically guard against the observed failure mode: nearly all
    # pixels crushed into a narrow band near 1.0.
    valid = data[~np.isnan(data)]
    near_white_fraction = np.mean(valid > 0.97)
    assert near_white_fraction < 0.5, (
        f"{near_white_fraction:.1%} of pixels are >0.97 -- looks like the "
        "over-aggressive-stretch bug is back."
    )
