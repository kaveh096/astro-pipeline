from pathlib import Path

import numpy as np
import pytest
import tifffile
from astropy.io import fits
from PIL import Image

from astro_pipeline.export_image import (
    ExportError,
    _orient_for_display,
    _scale_to_uint,
    export,
    export_with_black_point,
)


def write_fits(path: Path, data: np.ndarray, row_order: str | None = None) -> Path:
    header = fits.Header()
    if row_order is not None:
        header["ROWORDER"] = row_order
    fits.writeto(path, data, header=header, overwrite=True)
    return path


# --- row order: the trap that yields a plausible-looking mirrored image ---


def test_bottom_up_is_flipped() -> None:
    # Row 0 is the bottom of the image in FITS convention, so it must end
    # up as the LAST row for display.
    data = np.array([[1.0, 1.0], [9.0, 9.0]], dtype=np.float32)
    oriented, row_order = _orient_for_display(data, fits.Header({"ROWORDER": "BOTTOM-UP"}))
    assert row_order == "BOTTOM-UP"
    assert oriented[0, 0] == 9.0  # was the last row, now the top


def test_top_down_is_not_flipped() -> None:
    data = np.array([[1.0, 1.0], [9.0, 9.0]], dtype=np.float32)
    oriented, row_order = _orient_for_display(data, fits.Header({"ROWORDER": "TOP-DOWN"}))
    assert row_order == "TOP-DOWN"
    assert oriented[0, 0] == 1.0  # already top-first, unchanged


def test_absent_row_order_defaults_to_fits_convention() -> None:
    """Absent ROWORDER means the FITS default (bottom-up), so it must flip
    -- silently assuming top-down would mirror every file lacking the
    keyword."""
    data = np.array([[1.0, 1.0], [9.0, 9.0]], dtype=np.float32)
    oriented, row_order = _orient_for_display(data, fits.Header())
    assert row_order == "BOTTOM-UP"
    assert oriented[0, 0] == 9.0


def test_row_order_flip_applies_to_colour_data() -> None:
    """Channel-first (3, ny, nx): the flip must hit the row axis, not the
    channel axis."""
    data = np.zeros((3, 2, 2), dtype=np.float32)
    data[:, 1, :] = 9.0  # bottom row bright
    oriented, _ = _orient_for_display(data, fits.Header({"ROWORDER": "BOTTOM-UP"}))
    assert oriented.shape == (3, 2, 2)  # channel axis intact
    assert np.all(oriented[:, 0, :] == 9.0)  # bright row now on top


# --- scaling / clipping / NaN ------------------------------------------


def test_scale_maps_range_to_full_integer_range() -> None:
    data = np.array([0.0, 0.5, 1.0], dtype=np.float32)
    pixels, low_frac, high_frac = _scale_to_uint(data, 16, 0.0, 1.0)
    assert pixels[0] == 0
    assert pixels[2] == 65535
    assert abs(int(pixels[1]) - 32768) <= 1
    assert low_frac == 0.0 and high_frac == 0.0


def test_scale_reports_clipping() -> None:
    data = np.array([-1.0, 0.5, 2.0], dtype=np.float32)
    pixels, low_frac, high_frac = _scale_to_uint(data, 16, 0.0, 1.0)
    assert pixels[0] == 0 and pixels[2] == 65535
    assert low_frac == pytest.approx(1 / 3)
    assert high_frac == pytest.approx(1 / 3)


def test_nan_becomes_black_not_garbage() -> None:
    """Reprojected data carries real NaN outside the footprint; casting NaN
    straight to an integer is undefined, so it must be mapped explicitly."""
    data = np.array([np.nan, 0.5, np.nan], dtype=np.float32)
    pixels, _, _ = _scale_to_uint(data, 16, 0.0, 1.0)
    assert pixels[0] == 0 and pixels[2] == 0


def test_scale_rejects_degenerate_range() -> None:
    with pytest.raises(ExportError):
        _scale_to_uint(np.array([0.5], dtype=np.float32), 16, 1.0, 1.0)


# --- end-to-end file writing -------------------------------------------


def test_export_mono_writes_real_tiff_and_png(tmp_path: Path) -> None:
    data = np.linspace(0, 1, 64 * 64, dtype=np.float32).reshape(64, 64)
    src = write_fits(tmp_path / "mono.fit", data, row_order="TOP-DOWN")

    result = export(src, output_dir=tmp_path, preview_max_dim=32)

    assert result.tiff_path is not None and result.tiff_path.exists()
    assert result.preview_path is not None and result.preview_path.exists()

    tiff = tifffile.imread(result.tiff_path)
    assert tiff.dtype == np.uint16
    assert tiff.shape == (64, 64)

    # The preview must be a genuine PNG, not a TIFF wearing a .png name --
    # tifffile would happily have written the latter.
    with Image.open(result.preview_path) as img:
        assert img.format == "PNG"
        assert max(img.size) <= 32


def test_export_colour_converts_to_channel_last(tmp_path: Path) -> None:
    data = np.zeros((3, 32, 32), dtype=np.float32)
    data[0] = 1.0  # pure red
    src = write_fits(tmp_path / "rgb.fit", data, row_order="TOP-DOWN")

    result = export(src, output_dir=tmp_path, write_preview=False)

    tiff = tifffile.imread(result.tiff_path)
    assert tiff.shape == (32, 32, 3)  # channel-last for image formats
    assert tiff[0, 0, 0] == 65535 and tiff[0, 0, 1] == 0  # red survived as red


def test_export_reports_nan_fraction(tmp_path: Path) -> None:
    data = np.full((16, 16), 0.5, dtype=np.float32)
    data[:4] = np.nan
    src = write_fits(tmp_path / "withnan.fit", data, row_order="TOP-DOWN")

    result = export(src, output_dir=tmp_path, write_preview=False)

    assert result.nan_fraction == pytest.approx(0.25)


# --- export_with_black_point (capability D2, 2026-09) -----------------------


def test_black_point_crushes_shadows_and_rescales_remaining_range(tmp_path: Path) -> None:
    """A black_point of 0.2 should clip everything at/below 0.2 to output 0,
    and rescale [0.2, 1.0] to fill the full output range -- NOT just shift
    everything down uniformly."""
    data = np.array([0.0, 0.2, 0.6, 1.0], dtype=np.float32)
    src = write_fits(tmp_path / "shadows.fit", data, row_order="TOP-DOWN")

    result = export_with_black_point(src, black_point=0.2, output_dir=tmp_path, write_preview=False)

    tiff = tifffile.imread(result.tiff_path)
    # 0.0 is strictly below black_point -> clipped. 0.2 lands exactly at the
    # new zero point via the rescale math itself (0.2-0.2)/(1.0-0.2) = 0.0,
    # not via the clipped-pixel counter (which only counts data < low,
    # matching _scale_to_uint's existing, already-tested semantics).
    assert tiff[0] == 0 and tiff[1] == 0
    # 0.6 is (0.6-0.2)/(1.0-0.2) = 0.5 of the remaining range.
    assert abs(int(tiff[2]) - 32768) <= 1
    assert tiff[3] == 65535
    assert result.clipped_low_fraction == pytest.approx(0.25)  # only the 0.0 pixel is < black_point


def test_black_point_zero_is_equivalent_to_plain_export(tmp_path: Path) -> None:
    data = np.array([0.0, 0.5, 1.0], dtype=np.float32)
    src = write_fits(tmp_path / "plain.fit", data, row_order="TOP-DOWN")

    plain = export(src, output_dir=tmp_path, stem="plain_out", write_preview=False)
    darkened = export_with_black_point(
        src, black_point=0.0, output_dir=tmp_path, stem="darkened_out", write_preview=False
    )

    assert np.array_equal(tifffile.imread(plain.tiff_path), tifffile.imread(darkened.tiff_path))


def test_black_point_writes_a_separate_file_not_overwriting_the_faithful_export(tmp_path: Path) -> None:
    data = np.array([0.0, 0.5, 1.0], dtype=np.float32)
    src = write_fits(tmp_path / "orion.fit", data, row_order="TOP-DOWN")

    faithful = export(src, output_dir=tmp_path, stem="orion_lrgb", write_preview=False)
    darkened = export_with_black_point(
        src, black_point=0.3, output_dir=tmp_path, stem="orion_darkened", write_preview=False
    )

    assert faithful.tiff_path != darkened.tiff_path
    assert faithful.tiff_path.exists() and darkened.tiff_path.exists()
    assert not np.array_equal(tifffile.imread(faithful.tiff_path), tifffile.imread(darkened.tiff_path))


def test_black_point_rejects_out_of_range_values(tmp_path: Path) -> None:
    data = np.array([0.5], dtype=np.float32)
    src = write_fits(tmp_path / "x.fit", data, row_order="TOP-DOWN")

    with pytest.raises(ValueError):
        export_with_black_point(src, black_point=1.0, output_dir=tmp_path, write_preview=False)
    with pytest.raises(ValueError):
        export_with_black_point(src, black_point=-0.1, output_dir=tmp_path, write_preview=False)
