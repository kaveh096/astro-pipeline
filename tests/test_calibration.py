from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.calibration import (
    CalibrationFramesMissingError,
    FlatPolicy,
    build_master,
    calibrate_lights,
    run_calibration,
    sequence_name,
)
from astro_pipeline.ingest import scan_session
from astro_pipeline.siril_driver import find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")

REAL_SESSION_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 on T24")
requires_real_session = pytest.mark.skipif(
    not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine"
)


def test_sequence_name_appends_underscore() -> None:
    assert sequence_name("bias") == "bias_"
    assert sequence_name("lights") == "lights_"


def test_build_master_raises_on_empty_frame_list(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError):
        build_master([], "bias", tmp_path)


def test_calibrate_lights_raises_on_empty_light_list(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError):
        calibrate_lights([], master_bias=tmp_path / "b.fit", master_dark=tmp_path / "d.fit", work_dir=tmp_path)


def test_run_calibration_raises_on_missing_bias(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError, match="bias"):
        run_calibration(
            light_frames=["fake"],  # type: ignore[list-item]
            bias_frames=[],
            dark_frames=["fake"],  # type: ignore[list-item]
            work_dir=tmp_path,
        )


def test_run_calibration_raises_on_missing_dark(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError, match="dark"):
        run_calibration(
            light_frames=["fake"],  # type: ignore[list-item]
            bias_frames=["fake"],  # type: ignore[list-item]
            dark_frames=[],
            work_dir=tmp_path,
        )


def test_run_calibration_raises_on_missing_flat_when_required(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError, match="flat"):
        run_calibration(
            light_frames=["fake"],  # type: ignore[list-item]
            bias_frames=["fake"],  # type: ignore[list-item]
            dark_frames=["fake"],  # type: ignore[list-item]
            work_dir=tmp_path,
            flat_frames=None,
            flat_policy=FlatPolicy.REQUIRE,
        )


# --- real end-to-end test: proves calibration works without flats ----------


@requires_siril
@requires_real_session
def test_calibrate_real_luminance_bin1_without_flats(tmp_path: Path) -> None:
    """Directly answers: can we make progress with no flats for T24?
    Builds real master bias/dark from the actual delivery and calibrates
    the real Luminance BIN1 lights with flat_policy=SKIP_IF_MISSING
    (the default), since the real session has zero flat frames.
    """
    report = scan_session(REAL_SESSION_DIR)
    groups = report.light_groups()
    lum_lights = groups[("T24", "M51", "Luminance", 1)]
    assert len(lum_lights) == 13

    cal_index = report.calibration_index()
    bias = cal_index[("T24", "Bias", 1, 0.0)]
    dark = cal_index[("T24", "Dark", 1, 300.0)]
    assert len(bias) == 5
    assert len(dark) == 5

    result = run_calibration(
        light_frames=lum_lights,
        bias_frames=bias,
        dark_frames=dark,
        work_dir=tmp_path,
        flat_frames=None,
    )

    assert result.flat_corrected is False
    assert result.master_flat is None
    assert len(result.calibrated_lights) == 13

    raw_data = fits.getdata(lum_lights[0].path).astype(np.float64)
    calibrated_data = fits.getdata(result.calibrated_lights[0]).astype(np.float64)
    assert calibrated_data.shape == raw_data.shape
    # Bias+dark subtraction must actually change the pixel values, not
    # just pass the file through unmodified.
    assert not np.allclose(raw_data, calibrated_data)
    # Calibration should reduce the mean level (removing bias/dark offset),
    # not add an implausible amount of signal.
    assert calibrated_data.mean() < raw_data.mean()
