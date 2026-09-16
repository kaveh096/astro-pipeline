"""Tests for narrowband_boost.py (capability A2, 2026-09)."""

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.narrowband_boost import (
    DEFAULT_BOOST_FACTOR,
    NarrowbandBoostError,
    boost_channel_with_narrowband,
    equalize_mono_for_boost,
    rescale_narrowband_to_reference,
    split_rgb_channels,
)
from astro_pipeline.siril_driver import find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")


def test_split_rgb_channels_produces_three_real_mono_files(tmp_path: Path) -> None:
    data = np.zeros((3, 10, 10), dtype=np.float32)
    data[0] = 0.1
    data[1] = 0.2
    data[2] = 0.3
    src = tmp_path / "rgb.fit"
    fits.writeto(src, data)

    red, green, blue = split_rgb_channels(src, tmp_path)

    assert red.exists() and green.exists() and blue.exists()
    assert np.all(fits.getdata(red) == 0.1)
    assert np.all(fits.getdata(green) == 0.2)
    assert np.all(fits.getdata(blue) == 0.3)


def test_split_rgb_channels_uses_given_stems(tmp_path: Path) -> None:
    data = np.zeros((3, 4, 4), dtype=np.float32)
    src = tmp_path / "rgb.fit"
    fits.writeto(src, data)

    paths = split_rgb_channels(src, tmp_path, stems=("r", "g", "b"))

    assert [p.name for p in paths] == ["r.fit", "g.fit", "b.fit"]


def test_split_rgb_channels_rejects_non_3channel_input(tmp_path: Path) -> None:
    src = tmp_path / "mono.fit"
    fits.writeto(src, np.ones((8, 8), dtype=np.float32))
    with pytest.raises(ValueError, match="3-channel"):
        split_rgb_channels(src, tmp_path)


@requires_siril
def test_boost_channel_with_narrowband_lightens_where_narrowband_is_brighter(tmp_path: Path) -> None:
    """Real, load-bearing claim (verified against the actual installed
    Siril 1.4.4): max(channel, narrowband*k) genuinely boosts a region
    where narrowband*k exceeds the original channel, and leaves
    unaffected regions untouched."""
    red = np.full((16, 16), 0.3, dtype=np.float32)
    ha = np.full((16, 16), 0.2, dtype=np.float32)
    ha[4:12, 4:12] = 0.95  # a bright nebula region in Ha, much brighter than Red
    red_path = tmp_path / "red.fit"
    ha_path = tmp_path / "ha.fit"
    fits.writeto(red_path, red)
    fits.writeto(ha_path, ha)

    boosted_path = boost_channel_with_narrowband(
        red_path, ha_path, tmp_path, output_stem="boosted", boost_factor=0.5
    )

    boosted = fits.getdata(boosted_path, memmap=False)
    # Background: max(0.3, 0.2*0.5=0.1) = 0.3, unchanged.
    assert boosted[0, 0] == pytest.approx(0.3, abs=1e-4)
    # Nebula region: max(0.3, 0.95*0.5=0.475) = 0.475, genuinely boosted.
    assert boosted[8, 8] == pytest.approx(0.475, abs=1e-3)


@requires_siril
def test_boost_channel_with_narrowband_default_factor_is_a_real_module_constant(tmp_path: Path) -> None:
    """DEFAULT_BOOST_FACTOR is a real, documented starting point (not a
    validated standard -- see module docstring) -- confirm it's actually
    used when the caller doesn't override it."""
    red = np.full((16, 16), 0.1, dtype=np.float32)
    ha = np.full((16, 16), 1.0, dtype=np.float32)
    red_path = tmp_path / "red.fit"
    ha_path = tmp_path / "ha.fit"
    fits.writeto(red_path, red)
    fits.writeto(ha_path, ha)

    boosted_path = boost_channel_with_narrowband(red_path, ha_path, tmp_path, output_stem="boosted_default")
    boosted = fits.getdata(boosted_path, memmap=False)
    assert boosted[0, 0] == pytest.approx(max(0.1, 1.0 * DEFAULT_BOOST_FACTOR), abs=1e-3)


def test_boost_channel_with_narrowband_raises_when_output_missing(tmp_path: Path, monkeypatch) -> None:
    """Mocked: if Siril reports success but the expected output file
    genuinely isn't there (e.g. a typo'd -- expression syntax that
    silently no-ops), this must raise rather than a false success."""
    import astro_pipeline.narrowband_boost as nb_module

    class _FakeResult:
        log_lines = ["fake success, but no file written"]

    def fake_run_script(commands, workdir=None, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(nb_module, "run_script", fake_run_script)

    red_path = tmp_path / "red.fit"
    ha_path = tmp_path / "ha.fit"
    fits.writeto(red_path, np.ones((4, 4), dtype=np.float32))
    fits.writeto(ha_path, np.ones((4, 4), dtype=np.float32))

    with pytest.raises(NarrowbandBoostError, match="did not produce"):
        boost_channel_with_narrowband(red_path, ha_path, tmp_path, output_stem="never_written")


# --- equalize_mono_for_boost -------------------------------------------


def test_equalize_mono_for_boost_normalizes_background_to_zero(tmp_path: Path) -> None:
    data = np.full((10, 10), 5.0, dtype=np.float32)
    data[3:7, 3:7] = 10.0
    src = tmp_path / "mono.fit"
    fits.writeto(src, data)

    out_path = tmp_path / "equalized.fit"
    median, high = equalize_mono_for_boost(src, out_path, high_percentile=99.0)

    result = fits.getdata(out_path, memmap=False)
    assert median == pytest.approx(5.0)
    assert abs(float(np.median(result))) < 0.01


def test_equalize_mono_for_boost_handles_degenerate_input(tmp_path: Path) -> None:
    src = tmp_path / "flat.fit"
    fits.writeto(src, np.full((8, 8), 3.0, dtype=np.float32))
    out_path = tmp_path / "out.fit"
    equalize_mono_for_boost(src, out_path)  # must not raise divide-by-zero


# --- rescale_narrowband_to_reference (the real, RGB-safe fix) -----------


def test_rescale_narrowband_to_reference_preserves_reference_scale(tmp_path: Path) -> None:
    """REAL, load-bearing claim (caught on real M42 data): rescaling must
    map the narrowband layer INTO the reference's own real (median, high)
    range -- not touch the reference's own scale at all."""
    # Reference (e.g. Red): background ~0.1, bright ~0.4.
    reference = np.full((20, 20), 0.1, dtype=np.float32)
    reference[5:15, 5:15] = 0.4
    ref_path = tmp_path / "reference.fit"
    fits.writeto(ref_path, reference)

    # Narrowband (e.g. Ha): background ~0.05, bright ~0.9 -- a very
    # different real absolute scale from the reference, the exact real
    # mismatch found on real M42 data.
    narrowband = np.full((20, 20), 0.05, dtype=np.float32)
    narrowband[5:15, 5:15] = 0.9
    nb_path = tmp_path / "narrowband.fit"
    fits.writeto(nb_path, narrowband)

    out_path = tmp_path / "rescaled.fit"
    result_path, ref_median = rescale_narrowband_to_reference(nb_path, ref_path, out_path, high_percentile=99.0)

    rescaled = fits.getdata(result_path, memmap=False)
    assert ref_median == pytest.approx(0.1, abs=1e-3)
    # Background region of the narrowband layer now sits at the
    # REFERENCE's own background level, not narrowband's own 0.05.
    assert rescaled[0, 0] == pytest.approx(0.1, abs=1e-2)
    # Bright region now sits near the REFERENCE's own high level (~0.4),
    # not narrowband's own 0.9.
    assert rescaled[8, 8] == pytest.approx(0.4, abs=1e-2)


def test_rescale_narrowband_to_reference_handles_degenerate_narrowband(tmp_path: Path) -> None:
    ref_path = tmp_path / "ref.fit"
    fits.writeto(ref_path, np.full((8, 8), 0.2, dtype=np.float32))
    nb_path = tmp_path / "flat_nb.fit"
    fits.writeto(nb_path, np.full((8, 8), 0.5, dtype=np.float32))  # zero variance
    out_path = tmp_path / "out.fit"
    rescale_narrowband_to_reference(nb_path, ref_path, out_path)  # must not raise


@requires_siril
def test_boost_channel_with_narrowband_channel_median_preserves_background(tmp_path: Path) -> None:
    """REAL, load-bearing claim (the actual bug caught on real M42 data):
    without channel_median, a narrowband layer's own background offset
    gets scaled down by boost_factor too, which can push it BELOW the
    reference's real background -- boosting 0% of pixels even where real
    signal exists. With channel_median given, background is preserved
    and genuine bright regions still boost through."""
    channel = np.full((16, 16), 0.5, dtype=np.float32)  # reference background = 0.5
    # A narrowband layer already rescaled into the reference's own units
    # (as rescale_narrowband_to_reference would produce): background at
    # channel's own level (0.5), a genuinely bright region at 0.9.
    narrowband = np.full((16, 16), 0.5, dtype=np.float32)
    narrowband[4:12, 4:12] = 0.9
    channel_path = tmp_path / "channel.fit"
    narrowband_path = tmp_path / "narrowband.fit"
    fits.writeto(channel_path, channel)
    fits.writeto(narrowband_path, narrowband)

    # WITHOUT channel_median: max(0.5, 0.9*0.4=0.36) = 0.5 -- the real
    # bug, zero boost despite genuine bright narrowband signal.
    naive = boost_channel_with_narrowband(
        channel_path, narrowband_path, tmp_path, output_stem="naive", boost_factor=0.4
    )
    naive_data = fits.getdata(naive, memmap=False)
    assert naive_data[8, 8] == pytest.approx(0.5, abs=1e-3)

    # WITH channel_median: max(0.5, 0.5*(1-0.4) + 0.9*0.4 = 0.66) = 0.66
    # -- the real fix, genuine boost in the bright region.
    fixed = boost_channel_with_narrowband(
        channel_path, narrowband_path, tmp_path, output_stem="fixed", boost_factor=0.4, channel_median=0.5
    )
    fixed_data = fits.getdata(fixed, memmap=False)
    assert fixed_data[8, 8] == pytest.approx(0.66, abs=1e-2)
    # Background (equal in channel and narrowband) is unaffected either way.
    assert fixed_data[0, 0] == pytest.approx(0.5, abs=1e-3)
