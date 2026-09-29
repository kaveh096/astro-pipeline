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

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill"))

from astro_pipeline.calibration import CalibrationMode, FlatPolicy  # noqa: E402


def _write_tiny_fit(path: Path, shape: tuple[int, ...] = (3, 4, 4)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=np.zeros(shape, dtype=np.float32)).writeto(path, overwrite=True)


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
    monkeypatch.setattr(run_stage.preflight, "check_prerequisites", lambda **kwargs: [])
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
    monkeypatch.setattr(run_narrowband.preflight, "check_prerequisites", lambda **kwargs: [])
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
    """run_narrowband_boost.py now has a real, standalone build_parser()
    (publish-readiness Step 9) -- this just aliases it. Kept as a function
    (rather than updating every call site to `run_narrowband_boost.
    build_parser()` directly) so this file's existing structure doesn't
    need to change everywhere it's used."""
    import run_narrowband_boost as mod

    return mod.build_parser()


def test_run_narrowband_boost_call_site_passes_parsed_calibration_mode(tmp_path: Path, monkeypatch) -> None:
    """The real regression this test guards against: run_narrowband_boost's
    own main() never passed calibration_mode to build_single_filter_master
    at all before Step 5 -- it always fell through to
    infer_calibration_mode's own default inside that function. Confirms
    the parsed CLI value actually reaches the real call site."""
    import run_narrowband_boost

    final_dir = tmp_path / "_pipeline" / "final"
    final_dir.mkdir(parents=True)
    # Real, tiny FITS now (not junk bytes) -- Step 9's fail-fast Luminance
    # resolution reads both files' headers via astropy before main() ever
    # reaches build_single_filter_master, so both must be real, matching-
    # shape FITS for this test's --calibration-mode assertion to be
    # reachable at all.
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    _write_tiny_fit(final_dir / "lum_bg.fits", shape=(4, 4))

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
    monkeypatch.setattr(run_narrowband_boost.preflight, "check_prerequisites", lambda **kwargs: [])

    run_narrowband_boost.main(
        [
            str(tmp_path), "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--calibration-mode", "raw_local",
        ]
    )
    assert captured.get("calibration_mode") == CalibrationMode.RAW_LOCAL


def test_run_narrowband_boost_main_threads_calibration_header_fallback_and_preserves_source(
    tmp_path: Path, monkeypatch
) -> None:
    """run_narrowband_boost.py wires --calibration-header-fallback into
    scan_session() and wraps the result in _NarrowbandNormalizingReport
    before it reaches build_single_filter_master's header-fallback-aware
    resolution -- but unlike run_narrowband.py/interview.py (see their own
    dedicated tests above), this call site had no test exercising
    calibration_header_fallback=True end-to-end at all.

    Confirms (a) scan_session() actually receives
    calibration_header_fallback=True, and (b) header-fallback-recognized
    frames' `source` attribute survives the REAL _NarrowbandNormalizingReport
    wrapper's attribute delegation: calibration_index() is one of the
    methods that proxy does NOT override (only instrument_groups()/
    calibrated_instrument_groups()/flat_index() are -- see its own
    docstring), so it must delegate straight through via __getattr__ to
    the underlying real report, unchanged -- exactly what
    calibration_policy.infer_calibration_mode()'s own
    getattr(f, "source", "filename") check needs, not silently lost."""
    import run_narrowband_boost
    from astro_pipeline.ingest import CalibrationFrame

    final_dir = tmp_path / "_pipeline" / "final"
    final_dir.mkdir(parents=True)
    # Real, tiny FITS now (not junk bytes) -- see the comment in the test
    # above for why both are needed since Step 9's fail-fast Luminance
    # resolution.
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    _write_tiny_fit(final_dir / "lum_bg.fits", shape=(4, 4))

    header_dark = CalibrationFrame(
        path=tmp_path / "dark0.fit", telescope="T20", frame_type="Dark", binning=2,
        exptime=300.0, source="header",
    )

    class _FakeRawReport:
        def instrument_groups(self):
            return {}

        def calibrated_instrument_groups(self):
            return {}

        def flat_index(self):
            return {}

        def calibration_index(self):
            return {("T20", "Dark", 2, 300.0): [header_dark]}

    captured_scan_kwargs = {}

    def fake_scan_session(project_dir, **kwargs):
        captured_scan_kwargs.update(kwargs)
        return _FakeRawReport()

    monkeypatch.setattr(run_narrowband_boost, "pipeline_dir", lambda project_dir: tmp_path / "_pipeline")
    monkeypatch.setattr(run_narrowband_boost, "scan_session", fake_scan_session)
    # _NarrowbandNormalizingReport itself is left REAL (not mocked) -- the
    # whole point of this test is to confirm its delegation behaviour.

    captured_report = {}

    def fake_build_single_filter_master(project_dir, report, *args, **kwargs):
        captured_report["report"] = report
        return None  # short-circuits main() right after this call

    monkeypatch.setattr(run_narrowband_boost, "build_single_filter_master", fake_build_single_filter_master)
    monkeypatch.setattr(run_narrowband_boost.preflight, "check_prerequisites", lambda **kwargs: [])

    run_narrowband_boost.main(
        [
            str(tmp_path), "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--calibration-header-fallback",
        ]
    )

    assert captured_scan_kwargs.get("calibration_header_fallback") is True

    wrapped_report = captured_report["report"]
    frames = wrapped_report.calibration_index()[("T20", "Dark", 2, 300.0)]
    assert frames[0].source == "header"


# --- Step 9 (publish-readiness plan): run_narrowband_boost.py's Luminance
# pairing -- shape-matched, not just filename-matched; a stale cropped file
# from an earlier multi-contributor run must not be paired with a later
# single-contributor rgb_reconciled just because the name suggests it should.


def test_find_existing_prefers_final_dir_over_intermediate(tmp_path: Path) -> None:
    import run_narrowband_boost

    final_dir = tmp_path / "final"
    _write_tiny_fit(final_dir / "_intermediate" / "rgb_reconciled.fit")
    _write_tiny_fit(final_dir / "rgb_reconciled.fit")

    found = run_narrowband_boost._find_existing(final_dir, "rgb_reconciled.fit")
    assert found == final_dir / "rgb_reconciled.fit"  # final/ wins, not _intermediate/


def test_spatial_shape_matches_compares_naxis1_naxis2_only(tmp_path: Path) -> None:
    import run_narrowband_boost

    p1, p2, p3 = tmp_path / "a.fit", tmp_path / "b.fit", tmp_path / "c.fit"
    _write_tiny_fit(p1, shape=(3, 4, 4))  # 3-D RGB, 4x4 spatial
    _write_tiny_fit(p2, shape=(1, 4, 4))  # 3-D single-channel L, same 4x4 spatial
    _write_tiny_fit(p3, shape=(3, 8, 8))  # different spatial shape

    assert run_narrowband_boost._spatial_shape_matches(p1, p2) is True
    assert run_narrowband_boost._spatial_shape_matches(p1, p3) is False


def test_resolve_luminance_prefers_cropped_when_it_matches(tmp_path: Path) -> None:
    import run_narrowband_boost

    final_dir = tmp_path / "final"
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    _write_tiny_fit(final_dir / "lum_bg_cropped.fits", shape=(4, 4))
    _write_tiny_fit(final_dir / "lum_bg.fits", shape=(3, 8, 8))  # wrong shape -- must not be picked

    resolved = run_narrowband_boost._resolve_luminance(final_dir, final_dir / "rgb_reconciled.fit")
    assert resolved == final_dir / "lum_bg_cropped.fits"


def test_resolve_luminance_falls_back_to_lum_bg_when_no_cropped_file(tmp_path: Path) -> None:
    import run_narrowband_boost

    final_dir = tmp_path / "final"
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    _write_tiny_fit(final_dir / "lum_bg.fits", shape=(4, 4))

    resolved = run_narrowband_boost._resolve_luminance(final_dir, final_dir / "rgb_reconciled.fit")
    assert resolved == final_dir / "lum_bg.fits"


def test_resolve_luminance_falls_back_to_lum_bg_when_cropped_is_stale(tmp_path: Path) -> None:
    """Real bug fixed: a STALE lum_bg_cropped.fits from an earlier
    multi-contributor run must not be paired with a later single-
    contributor rgb_reconciled just because the filename suggests it
    should -- it's checked by actual shape, not assumed from presence."""
    import run_narrowband_boost

    final_dir = tmp_path / "final"
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    _write_tiny_fit(final_dir / "lum_bg_cropped.fits", shape=(3, 9, 9))  # stale, wrong shape
    _write_tiny_fit(final_dir / "lum_bg.fits", shape=(4, 4))  # matches

    resolved = run_narrowband_boost._resolve_luminance(final_dir, final_dir / "rgb_reconciled.fit")
    assert resolved == final_dir / "lum_bg.fits"


def test_resolve_luminance_raises_clear_error_when_nothing_matches(tmp_path: Path) -> None:
    import run_narrowband_boost

    final_dir = tmp_path / "final"
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    _write_tiny_fit(final_dir / "lum_bg.fits", shape=(3, 9, 9))  # wrong shape, only candidate

    with pytest.raises(FileNotFoundError, match="spatial shape matching"):
        run_narrowband_boost._resolve_luminance(final_dir, final_dir / "rgb_reconciled.fit")


def test_main_no_luminance_skips_luminance_resolution_entirely(tmp_path: Path, monkeypatch) -> None:
    """--no-luminance must never call _resolve_luminance -- an RGB-only
    target genuinely has no Luminance file to find, and searching for one
    (and potentially raising) would be a real regression."""
    import run_narrowband_boost

    final_dir = tmp_path / "_pipeline" / "final"
    _write_tiny_fit(final_dir / "rgb_reconciled.fit", shape=(3, 4, 4))
    # Deliberately NO lum_bg.fits/lum_bg_cropped.fits anywhere.

    class _FakeReport:
        def instrument_groups(self):
            return {}

    monkeypatch.setattr(run_narrowband_boost, "pipeline_dir", lambda project_dir: tmp_path / "_pipeline")
    monkeypatch.setattr(run_narrowband_boost, "scan_session", lambda project_dir, **k: _FakeReport())
    monkeypatch.setattr(run_narrowband_boost, "_NarrowbandNormalizingReport", lambda raw_report: raw_report)
    monkeypatch.setattr(run_narrowband_boost, "build_single_filter_master", lambda *a, **k: None)
    monkeypatch.setattr(run_narrowband_boost.preflight, "check_prerequisites", lambda **kwargs: [])

    def fail_if_called(*a, **k):
        raise AssertionError("_resolve_luminance must not be called under --no-luminance")

    monkeypatch.setattr(run_narrowband_boost, "_resolve_luminance", fail_if_called)

    run_narrowband_boost.main(
        [
            str(tmp_path), "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391",
            "--no-luminance",
        ]
    )


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
    monkeypatch.setattr(interview, "prerequisites_summary", lambda report: [])
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
    monkeypatch.setattr(interview, "prerequisites_summary", lambda report: [])
    interview.main(["interview.py", "some_project"])
    assert captured.get("calibration_header_fallback") is False


# --- Step 8: --flat-policy, added to run_stage.py/run_narrowband.py/
# run_narrowband_boost.py/interview.py alongside --calibration-mode. ------


def test_run_stage_parses_flat_policy() -> None:
    import run_stage

    args = run_stage.build_parser().parse_args(
        [
            "proj", "--telescope", "T24", "--target", "M51",
            "--ra-hours", "13.4", "--dec-deg", "47.2", "--flat-policy", "require",
        ]
    )
    assert args.flat_policy == "require"


def test_run_stage_main_passes_flat_policy_to_run_lrgb(monkeypatch) -> None:
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
    monkeypatch.setattr(run_stage.preflight, "check_prerequisites", lambda **kwargs: [])
    run_stage.main(
        [
            "proj", "--telescope", "T24", "--target", "M51",
            "--ra-hours", "13.4", "--dec-deg", "47.2", "--flat-policy", "skip_if_missing",
        ]
    )
    assert captured["flat_policy"] == FlatPolicy.SKIP_IF_MISSING


def test_run_narrowband_parses_and_passes_flat_policy(monkeypatch) -> None:
    import run_narrowband

    args = run_narrowband.build_parser().parse_args(
        [
            "proj", "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391", "--flat-policy", "require",
        ]
    )
    assert args.flat_policy == "require"

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
    monkeypatch.setattr(run_narrowband.preflight, "check_prerequisites", lambda **kwargs: [])
    run_narrowband.main(
        [
            "proj", "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391", "--flat-policy", "require",
        ]
    )
    assert captured["flat_policy"] == FlatPolicy.REQUIRE


def test_run_narrowband_boost_parses_flat_policy() -> None:
    parser = argparse_from_boost_main()
    args = parser.parse_args(
        [
            "proj", "--telescope", "T20", "--target", "M42",
            "--ra-hours", "5.588", "--dec-deg", "-5.391", "--flat-policy", "skip_if_missing",
        ]
    )
    assert args.flat_policy == "skip_if_missing"


def test_interview_main_threads_flat_policy_override(monkeypatch) -> None:
    import interview

    captured = {}
    real_render = interview.render

    def spy_render(project_dir, report, flat_policy_override=None, prerequisites=None):
        captured["flat_policy_override"] = flat_policy_override
        return real_render(project_dir, report, flat_policy_override, prerequisites)

    def fake_scan_session(project_dir, **kwargs):
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
    monkeypatch.setattr(interview, "render", spy_render)
    monkeypatch.setattr(interview, "prerequisites_summary", lambda report: [])
    interview.main(["interview.py", "some_project", "--flat-policy", "require"])
    assert captured["flat_policy_override"] == FlatPolicy.REQUIRE
