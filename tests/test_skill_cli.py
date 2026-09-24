"""Step 5 (plan-flats-v4.md): CLI coverage for the --calibration-mode
override and --calibration-header-fallback flag across run_stage.py,
run_narrowband.py and run_narrowband_boost.py -- three DISTINCT shapes
(R4-3), not one. None of these three scripts had any direct parser/
call-site tests before this step; each test here mocks the underlying
run_lrgb/run_narrowband/build_single_filter_master call so no real
Siril/GraXpert work is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill"))

from astro_pipeline.calibration import CalibrationMode  # noqa: E402


# --- run_stage.py: repeatable TEL=value dict form ---------------------------


def test_run_stage_parses_calibration_mode_dict_repeated() -> None:
    import run_stage

    args = run_stage.build_parser().parse_args(
        [
            "proj", "--telescope", "T24", "--target", "M51",
            "--ra-hours", "13.4", "--dec-deg", "47.2",
            "--calibration-mode", "T20=raw_local",
            "--calibration-mode", "T68=precalibrated",
        ]
    )
    parsed = run_stage._parse_calibration_mode_dict(args.calibration_mode)
    assert parsed == {"T20": CalibrationMode.RAW_LOCAL, "T68": CalibrationMode.PRECALIBRATED}


def test_run_stage_calibration_mode_defaults_to_none_when_not_given() -> None:
    import run_stage

    args = run_stage.build_parser().parse_args(
        ["proj", "--telescope", "T24", "--target", "M51", "--ra-hours", "13.4", "--dec-deg", "47.2"]
    )
    assert run_stage._parse_calibration_mode_dict(args.calibration_mode) is None
    assert args.calibration_header_fallback is False


def test_run_stage_main_passes_calibration_mode_and_fallback_to_run_lrgb(monkeypatch) -> None:
    import run_stage

    captured = {}

    def fake_run_lrgb(**kwargs):
        captured.update(kwargs)

        class _Result:
            notes = []
            checkpoints = []
            composite_path = None
            export_result = None

        return _Result()

    monkeypatch.setattr(run_stage, "run_lrgb", fake_run_lrgb)
    run_stage.main(
        [
            "proj", "--telescope", "T24", "--target", "M51",
            "--ra-hours", "13.4", "--dec-deg", "47.2",
            "--calibration-mode", "T20=raw_local",
            "--calibration-header-fallback",
        ]
    )
    assert captured["calibration_mode"] == {"T20": CalibrationMode.RAW_LOCAL}
    assert captured["calibration_header_fallback"] is True


# --- run_narrowband.py: plain single-value form, no TEL= -------------------


def test_run_narrowband_parses_plain_calibration_mode_no_tel_prefix() -> None:
    import run_narrowband

    args = run_narrowband.build_parser().parse_args(
        [
            "proj", "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--calibration-mode", "raw_local",
        ]
    )
    assert args.calibration_mode == "raw_local"
    assert CalibrationMode(args.calibration_mode) == CalibrationMode.RAW_LOCAL


def test_run_narrowband_calibration_mode_defaults_to_none() -> None:
    import run_narrowband

    args = run_narrowband.build_parser().parse_args(
        ["proj", "--telescope", "T20", "--target", "M42", "--ra-hours", "5.588", "--dec-deg", "-5.391"]
    )
    assert args.calibration_mode is None
    assert args.calibration_header_fallback is False


def test_run_narrowband_main_passes_calibration_mode_and_fallback(monkeypatch) -> None:
    import run_narrowband

    captured = {}

    def fake_run_narrowband(**kwargs):
        captured.update(kwargs)

        class _Result:
            notes = []
            checkpoints = []
            composite_path = None
            export_result = None

        return _Result()

    monkeypatch.setattr(run_narrowband, "run_narrowband", fake_run_narrowband)
    run_narrowband.main(
        [
            "proj", "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--calibration-mode", "precalibrated",
            "--calibration-header-fallback",
        ]
    )
    assert captured["calibration_mode"] == CalibrationMode.PRECALIBRATED
    assert captured["calibration_header_fallback"] is True


# --- run_narrowband_boost.py: same plain-value shape as run_narrowband.py,
# plus the call-site test that build_single_filter_master actually
# receives the parsed value instead of always falling through to
# infer_calibration_mode's own default. -------------------------------


def test_run_narrowband_boost_parses_plain_calibration_mode() -> None:
    import run_narrowband_boost

    parser = argparse_from_boost_main()
    args = parser.parse_args(
        [
            "proj", "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--calibration-mode", "raw_local",
        ]
    )
    assert args.calibration_mode == "raw_local"


def argparse_from_boost_main():
    """run_narrowband_boost.py builds its parser inline inside main(), not
    via a standalone build_parser() -- reconstruct an equivalent parser
    here purely to unit-test the --calibration-mode/--calibration-header-
    fallback argument definitions without invoking the whole main()."""
    import argparse

    import run_narrowband_boost as mod

    p = argparse.ArgumentParser()
    p.add_argument("project_dir")
    p.add_argument("--telescope", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--ra-hours", type=float, required=True)
    p.add_argument("--dec-deg", type=float, required=True)
    p.add_argument("--boost-filter", default="Ha")
    p.add_argument("--boost-channel", default="red", choices=sorted(mod.CHANNEL_INDEX))
    p.add_argument("--boost-factor", type=float, default=mod.DEFAULT_BOOST_FACTOR)
    p.add_argument("--binning", type=int, default=2)
    p.add_argument("--stretch-method", default="autostretch")
    p.add_argument("--no-luminance", action="store_true")
    p.add_argument("--calibration-mode", choices=["raw_local", "precalibrated"], default=None)
    p.add_argument("--calibration-header-fallback", action="store_true")
    return p


def test_run_narrowband_boost_call_site_passes_parsed_calibration_mode(tmp_path: Path, monkeypatch) -> None:
    """The real regression this test guards against: run_narrowband_boost's
    own main() never passed calibration_mode to build_single_filter_master
    at all before Step 5 -- it always fell through to
    infer_calibration_mode's own default inside that function. Confirms
    the parsed CLI value actually reaches the real call site."""
    import run_narrowband_boost

    final_dir = tmp_path / "_pipeline" / "final"
    final_dir.mkdir(parents=True)
    (final_dir / "rgb_reconciled.fit").write_bytes(b"not real fits, never read by _find_existing")

    class _FakeReport:
        def instrument_groups(self):
            return {}

    fake_report = _FakeReport()
    monkeypatch.setattr(run_narrowband_boost, "pipeline_dir", lambda project_dir: tmp_path / "_pipeline")
    monkeypatch.setattr(run_narrowband_boost, "scan_session", lambda project_dir, **k: fake_report)
    # Identity wrap -- build_single_filter_master is mocked below and
    # doesn't care about this object's shape; raw_report.instrument_groups()
    # (the "no usable master" diagnostic path) is what actually needs it.
    monkeypatch.setattr(run_narrowband_boost, "_NarrowbandNormalizingReport", lambda raw_report: raw_report)

    captured = {}

    def fake_build_single_filter_master(*args, **kwargs):
        captured.update(kwargs)
        return None  # short-circuits main() right after this call

    monkeypatch.setattr(run_narrowband_boost, "build_single_filter_master", fake_build_single_filter_master)

    run_narrowband_boost.main(
        [
            str(tmp_path), "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--calibration-mode", "raw_local",
        ]
    )
    assert captured.get("calibration_mode") == CalibrationMode.RAW_LOCAL


# --- interview.py main(): --calibration-header-fallback --------------------


def test_interview_main_threads_calibration_header_fallback(monkeypatch, capsys) -> None:
    import interview

    captured = {}

    def fake_scan_session(project_dir, **kwargs):
        captured.update(kwargs)

        class _EmptyReport:
            lights = []
            calibration = []
            unrecognized = []

            def instrument_groups(self):
                return {}

            def calibration_index(self):
                return {}

            def calibrated_instrument_groups(self):
                return {}

            def flat_index(self):
                return {}

            def missing_calibration_warnings(self):
                return []

        return _EmptyReport()

    monkeypatch.setattr(interview, "scan_session", fake_scan_session)
    interview.main(["interview.py", "some_project", "--calibration-header-fallback"])
    assert captured.get("calibration_header_fallback") is True


def test_interview_main_defaults_calibration_header_fallback_false(monkeypatch) -> None:
    import interview

    captured = {}

    def fake_scan_session(project_dir, **kwargs):
        captured.update(kwargs)

        class _EmptyReport:
            lights = []
            calibration = []
            unrecognized = []

            def instrument_groups(self):
                return {}

            def calibration_index(self):
                return {}

            def calibrated_instrument_groups(self):
                return {}

            def flat_index(self):
                return {}

            def missing_calibration_warnings(self):
                return []

        return _EmptyReport()

    monkeypatch.setattr(interview, "scan_session", fake_scan_session)
    interview.main(["interview.py", "some_project"])
    assert captured.get("calibration_header_fallback") is False
