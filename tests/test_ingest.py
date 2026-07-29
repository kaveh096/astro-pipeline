from pathlib import Path

import pytest

from astro_pipeline.ingest import (
    CalibrationFrame,
    LightFrame,
    UnrecognizedFrame,
    classify_frame,
    scan_session,
)

# Real filenames observed in an actual iTelescope T24 delivery (M51, Jan 2025).
LIGHT_NAME = "raw-T24-kaveh096-M51-20250123-021344-Blue-BIN2-E-300-001.fit"
BIAS_NAME = "T24-kaveh096-Bias-000-LD20250203-LT171434-BIN1.fit"
DARK_NAME = "T24-kaveh096-Dark-300-LD20250203-LT155037-BIN1.fit"

REAL_SESSION_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 on T24")


def touch(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"")  # regex classification never opens the file
    return p


def test_classify_light_frame(tmp_path: Path) -> None:
    frame = classify_frame(touch(tmp_path, LIGHT_NAME))
    assert isinstance(frame, LightFrame)
    assert frame.telescope == "T24"
    assert frame.target == "M51"
    assert frame.filter_name == "Blue"
    assert frame.binning == 2
    assert frame.exptime == 300.0
    assert frame.date == "20250123"
    assert frame.sequence == 1


def test_classify_bias_frame(tmp_path: Path) -> None:
    frame = classify_frame(touch(tmp_path, BIAS_NAME))
    assert isinstance(frame, CalibrationFrame)
    assert frame.frame_type == "Bias"
    assert frame.telescope == "T24"
    assert frame.binning == 1
    assert frame.exptime == 0.0


def test_classify_dark_frame(tmp_path: Path) -> None:
    frame = classify_frame(touch(tmp_path, DARK_NAME))
    assert isinstance(frame, CalibrationFrame)
    assert frame.frame_type == "Dark"
    assert frame.exptime == 300.0


def test_classify_unrecognized_filename_no_header_fallback(tmp_path: Path) -> None:
    frame = classify_frame(touch(tmp_path, "not_a_real_filename.fit"))
    assert isinstance(frame, UnrecognizedFrame)
    assert "unrecognized" in frame.reason.lower()


def test_scan_session_groups_lights_by_telescope_target_filter_binning(tmp_path: Path) -> None:
    touch(tmp_path, "raw-T24-kaveh096-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, "raw-T24-kaveh096-M51-20250123-023017-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, "raw-T24-kaveh096-M51-20250123-021344-Blue-BIN2-E-300-001.fit")
    touch(tmp_path, BIAS_NAME)

    report = scan_session(tmp_path)
    assert len(report.lights) == 3
    assert len(report.calibration) == 1

    groups = report.light_groups()
    lum_key = ("T24", "M51", "Luminance", 1)
    blue_key = ("T24", "M51", "Blue", 2)
    assert len(groups[lum_key]) == 2
    assert len(groups[blue_key]) == 1


def test_missing_calibration_warnings_flags_missing_flats_and_darks(tmp_path: Path) -> None:
    touch(tmp_path, "raw-T24-kaveh096-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, BIAS_NAME)
    touch(tmp_path, DARK_NAME)
    # no flats at all -- matches the real sample session exactly

    report = scan_session(tmp_path)
    warnings = report.missing_calibration_warnings()
    assert any("no bias" in w.lower() for w in warnings) is False  # bias IS present
    assert any("no flat" in w.lower() for w in warnings)


def test_missing_calibration_warnings_flags_missing_bias_and_dark(tmp_path: Path) -> None:
    touch(tmp_path, "raw-T24-kaveh096-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    # no calibration frames at all

    report = scan_session(tmp_path)
    warnings = report.missing_calibration_warnings()
    assert any("no bias" in w.lower() for w in warnings)
    assert any("no dark" in w.lower() for w in warnings)
    assert any("no flat" in w.lower() for w in warnings)


# --- integration test against the real M51-on-T24 delivery -----------------


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_scan_real_m51_t24_session() -> None:
    report = scan_session(REAL_SESSION_DIR)

    assert len(report.lights) == 49
    assert len(report.unrecognized) == 0, [f.path.name for f in report.unrecognized]

    groups = report.light_groups()
    assert ("T24", "M51", "Luminance", 1) in groups
    assert ("T24", "M51", "Red", 2) in groups
    assert ("T24", "M51", "Green", 2) in groups
    assert ("T24", "M51", "Blue", 2) in groups

    # Real delivery has no flats at all -- this must surface, not be silent.
    warnings = report.missing_calibration_warnings()
    assert any("no flat" in w.lower() for w in warnings)
    # But bias/dark for BIN1 and BIN2 at 300s are present, so no warning for those.
    assert not any("no bias" in w.lower() for w in warnings)
    assert not any("no dark" in w.lower() for w in warnings)
