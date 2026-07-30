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

REAL_SESSION_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025")


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
    assert frame.provenance == "raw"
    assert frame.side == "E"


def test_classify_light_frame_west_side_not_hardcoded(tmp_path: Path) -> None:
    """A real delivery (different user, same T24 telescope) proved the 'E'
    token is a meridian-side flag, not a constant -- must accept 'W' too."""
    name = "raw-T24-jmwill-M51-20250225-030711-Luminance-BIN1-W-300-001.fit"
    frame = classify_frame(touch(tmp_path, name))
    assert isinstance(frame, LightFrame)
    assert frame.side == "W"


def test_classify_calibrated_provenance_light_frame(tmp_path: Path) -> None:
    name = "calibrated-T21-kaveh096-M51-20250113-045216-Luminance-BIN1-E-600-001.fit"
    frame = classify_frame(touch(tmp_path, name))
    assert isinstance(frame, LightFrame)
    assert frame.provenance == "calibrated"


def test_classify_t21_camera_model_bias_dark(tmp_path: Path) -> None:
    """T21's calibration frames use a completely different, telescope-less
    naming convention (camera model, not telescope ID) -- telescope must be
    inferred from a 'T<digits>' ancestor directory."""
    t21_dir = tmp_path / "Calibrations" / "T21" / "Bias" / "2024 06"
    t21_dir.mkdir(parents=True)
    bias_path = t21_dir / "FLI6303 -0001biasBin1.fit"
    bias_path.write_bytes(b"")
    frame = classify_frame(bias_path)
    assert isinstance(frame, CalibrationFrame)
    assert frame.telescope == "T21"
    assert frame.frame_type == "Bias"
    assert frame.binning == 1

    dark_dir = tmp_path / "Calibrations" / "T21" / "Darks" / "2024 06"
    dark_dir.mkdir(parents=True)
    dark_path = dark_dir / "FLI6303 -0001dark900secBin2.fit"
    dark_path.write_bytes(b"")
    dark_frame = classify_frame(dark_path)
    assert isinstance(dark_frame, CalibrationFrame)
    assert dark_frame.telescope == "T21"
    assert dark_frame.frame_type == "Dark"
    assert dark_frame.binning == 2
    assert dark_frame.exptime == 900.0


def test_classify_t21_skyflat(tmp_path: Path) -> None:
    flat_dir = tmp_path / "Calibrations" / "T21" / "Flats" / "2024 06" / "raw flats" / "20240616_080204"
    flat_dir.mkdir(parents=True)
    flat_path = flat_dir / "scope_Luminance_1x1_skyflat0.fit"
    flat_path.write_bytes(b"")
    frame = classify_frame(flat_path)
    assert isinstance(frame, CalibrationFrame)
    assert frame.telescope == "T21"
    assert frame.frame_type == "Flat"
    assert frame.binning == 1


def test_classify_telescope_less_calibration_without_telescope_dir_is_unrecognized(tmp_path: Path) -> None:
    """Same camera-model filename, but with no T<digits> ancestor directory
    to infer telescope from -- must be flagged, not silently dropped or
    guessed at."""
    loose_dir = tmp_path / "SomeRandomFolder"
    loose_dir.mkdir()
    path = loose_dir / "FLI6303 -0001biasBin1.fit"
    path.write_bytes(b"")
    frame = classify_frame(path)
    assert isinstance(frame, UnrecognizedFrame)
    assert "telescope" in frame.reason.lower()


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
    lum_key = ("T24", "kaveh096", "M51", "Luminance", 1)
    blue_key = ("T24", "kaveh096", "M51", "Blue", 2)
    assert len(groups[lum_key]) == 2
    assert len(groups[blue_key]) == 1


def test_light_groups_keeps_different_users_separate(tmp_path: Path) -> None:
    """Two collaborators shooting the same target/telescope/filter/binning
    must land in different groups -- confirmed real (kaveh096 and jmwill
    both imaged M51 on T24), and silently merging them was a real bug."""
    touch(tmp_path, "raw-T24-kaveh096-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, "raw-T24-jmwill-M51-20250226-020311-Luminance-BIN1-E-300-001.fit")

    report = scan_session(tmp_path)
    groups = report.light_groups()

    assert len(groups[("T24", "kaveh096", "M51", "Luminance", 1)]) == 1
    assert len(groups[("T24", "jmwill", "M51", "Luminance", 1)]) == 1


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


# --- integration tests against the real multi-telescope M51 delivery -------
#
# This tree supersedes the original tidy single-telescope sample: it's messy
# on purpose (real iTelescope deliveries across two telescopes, two users,
# multiple filename conventions, zip-wrapped exposures). scan_session only
# covers loose .fit/.fits/.fts files under root -- zip-wrapped T21 lights and
# non-FITS files (previews, master calibration TIFFs) are index.py's job,
# covered in test_index.py.


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_scan_real_multi_telescope_session() -> None:
    report = scan_session(REAL_SESSION_DIR)

    # T24 raw lights: 95, from TWO different iTelescope users -- Kaveh
    # (kaveh096) and a collaborator (jmwill) who independently imaged the
    # same target on the same telescope with different binning choices for
    # RGB. T21's lights are zip-wrapped and invisible to a bare fit-glob
    # scan -- that's expected here, not a bug (see test_index.py).
    raw_lights = [f for f in report.lights if f.provenance == "raw"]
    assert len(raw_lights) == 95
    assert all(f.telescope == "T24" for f in raw_lights)
    assert {f.user for f in raw_lights} == {"kaveh096", "jmwill"}

    # iTelescope-side-calibrated duplicates of the same exposures are present
    # too, and must NOT show up in light_groups() (which only ever returns
    # raw provenance) -- conflating them would double-process or silently
    # prefer one over the other.
    calibrated_lights = [f for f in report.lights if f.provenance == "calibrated"]
    assert len(calibrated_lights) > 0
    groups = report.light_groups()
    for frames in groups.values():
        assert all(f.provenance == "raw" for f in frames)

    # The two users' data must land in SEPARATE groups, not merged --
    # confirmed real: without `user` in the grouping key, Kaveh's 13
    # kaveh096 Luminance/BIN1 subs and jmwill's 8 would have silently
    # combined into one group of 21.
    assert ("T24", "kaveh096", "M51", "Luminance", 1) in groups
    assert len(groups[("T24", "kaveh096", "M51", "Luminance", 1)]) == 13
    assert ("T24", "jmwill", "M51", "Luminance", 1) in groups
    assert len(groups[("T24", "jmwill", "M51", "Luminance", 1)]) == 8

    assert ("T24", "kaveh096", "M51", "Red", 2) in groups
    assert ("T24", "kaveh096", "M51", "Green", 2) in groups
    assert ("T24", "kaveh096", "M51", "Blue", 2) in groups
    # jmwill shoots RGB at BIN1 (matching L directly -- no drizzle/reproject
    # reconciliation needed for jmwill's own contributed frames, unlike
    # Kaveh's BIN2 RGB).
    assert ("T24", "jmwill", "M51", "Red", 1) in groups
    assert ("T24", "jmwill", "M51", "Green", 1) in groups
    assert ("T24", "jmwill", "M51", "Blue", 1) in groups

    # T24 has real bias/dark (Calibrations/T24/Fresh) but genuinely no
    # flats anywhere in this delivery -- must surface, not be silent.
    warnings = report.missing_calibration_warnings()
    assert any("no flat" in w.lower() and "t24" in w.lower() for w in warnings)
    assert not any("no bias" in w.lower() and "t24" in w.lower() for w in warnings)
    assert not any("no dark" in w.lower() and "t24" in w.lower() for w in warnings)

    # The only unrecognized FITS files should be the unidentified
    # "Master_Flat <Filter> ..." set (a 4th, unrecognized instrument/source)
    # -- correctly refused rather than guessed at.
    assert len(report.unrecognized) == 11
    assert all("master_flat" in f.path.name.lower() for f in report.unrecognized)
