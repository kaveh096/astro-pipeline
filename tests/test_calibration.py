from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.calibration import (
    CalibrationFramesMissingError,
    FlatPolicy,
    _calibrate_command,
    build_master,
    calibrate_lights,
    run_calibration,
    select_dark,
    sequence_name,
)
from astro_pipeline.ingest import CalibrationFrame, scan_session
from astro_pipeline.siril_driver import find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")

from conftest import PROJECT_DIR as REAL_SESSION_DIR
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


# --- _calibrate_command: property check that existing T24-shaped calls
# emit a byte-identical command string to before Slice 1 -- the actual,
# checkable proof that "nothing changes for existing data", since the
# real files always carry a wall-clock DATE header and can't be diffed
# byte-for-byte themselves. ---------------------------------------------


def test_calibrate_command_t24_shaped_call_is_byte_identical_to_today() -> None:
    # T24's real path: dark-only, no bias, no flat, no -opt, no -norm.
    cmd = _calibrate_command("lights_", "masterdark", None, None, dark_optimize=False)
    assert cmd == "calibrate lights_ -dark=masterdark -cc=dark -prefix=pp_"


def test_calibrate_command_t24_shaped_call_with_flat_is_byte_identical_to_today() -> None:
    cmd = _calibrate_command("lights_", "masterdark", None, "masterflat", dark_optimize=False)
    assert cmd == "calibrate lights_ -dark=masterdark -cc=dark -flat=masterflat -prefix=pp_"


def test_calibrate_command_scaled_path_drops_cc_dark_adds_opt_and_bias() -> None:
    # T21's real (600s/300s lights, 900s dark) shape.
    cmd = _calibrate_command("lights_", "masterdark", "masterbias", None, dark_optimize=True)
    assert cmd == "calibrate lights_ -dark=masterdark -bias=masterbias -opt=exp -prefix=pp_"
    assert "-cc=dark" not in cmd


@requires_siril
@requires_real_session
def test_calibrate_lights_real_t24_command_unaffected_by_slice1(tmp_path: Path) -> None:
    """The actual, real-data version of the property check above: build a
    real T24 group through calibrate_lights with today's call shape
    (subtract_bias=False, dark_optimize=False, no flat) and confirm the
    emitted command string, not just the isolated builder, matches.
    """
    import astro_pipeline.calibration as calibration_module

    report = scan_session(REAL_SESSION_DIR)
    groups = report.light_groups()
    lum_lights = groups[("T24", "kaveh096", "M51", "Luminance", 1)]
    cal_index = report.calibration_index()
    bias = cal_index[("T24", "Bias", 1, 0.0)]
    dark = cal_index[("T24", "Dark", 1, 300.0)]

    from astro_pipeline.calibration import build_master_bias, build_master_dark

    master_bias = build_master_bias(bias, tmp_path)
    master_dark = build_master_dark(dark, tmp_path)

    captured: dict[str, str] = {}
    real_calibrate_command = calibration_module._calibrate_command

    def spy(*args, **kwargs):
        cmd = real_calibrate_command(*args, **kwargs)
        captured["command"] = cmd
        return cmd

    calibration_module._calibrate_command = spy
    try:
        calibrate_lights(lum_lights, master_bias, master_dark, tmp_path)
    finally:
        calibration_module._calibrate_command = real_calibrate_command

    assert captured["command"] == "calibrate lights_ -dark=masterdark -cc=dark -prefix=pp_"


# --- select_dark: the Slice 1 dark-scaling policy, unit-tested against
# synthetic CalibrationFrames (no Siril / real data needed) -------------


def _dark(exptime: float, binning: int = 1, telescope: str = "T21") -> CalibrationFrame:
    return CalibrationFrame(
        path=Path(f"dark_{telescope}_{exptime:.0f}s_bin{binning}.fit"),
        telescope=telescope,
        frame_type="Dark",
        binning=binning,
        exptime=exptime,
    )


def test_select_dark_exact_match_stays_unscaled() -> None:
    # Every existing real T24 group is single-exptime with an exact-match
    # dark -- this is today's ONLY path, and it must not change.
    darks = [_dark(300.0, telescope="T24")]
    cal_index = {("T24", "Dark", 1, 300.0): darks}
    selection = select_dark(cal_index, "T24", 1, {300.0})
    assert selection.scaled is False
    assert selection.exptime == 300.0
    assert selection.frames == darks


def test_select_dark_scales_when_no_exact_match_exists() -> None:
    # T21's real case: 600s/300s Luminance lights, only a 900s dark exists.
    darks_900 = [_dark(900.0)]
    cal_index = {("T21", "Dark", 1, 900.0): darks_900}
    selection = select_dark(cal_index, "T21", 1, {600.0, 300.0})
    assert selection.scaled is True
    assert selection.exptime == 900.0
    assert selection.frames == darks_900


def test_select_dark_raises_naming_exptime_when_no_dark_at_binning() -> None:
    with pytest.raises(CalibrationFramesMissingError, match="300s"):
        select_dark({}, "T21", 2, {300.0})


def test_select_dark_refuses_to_scale_a_dark_up() -> None:
    # Only a SHORTER dark is available than the lights -- scaling UP is the
    # unsafe direction (plan's explicit refusal condition) and must raise,
    # not silently attempt it.
    cal_index = {("T21", "Dark", 1, 300.0): [_dark(300.0)]}
    with pytest.raises(CalibrationFramesMissingError, match="unsafe"):
        select_dark(cal_index, "T21", 1, {600.0})


def test_select_dark_exact_ratio_of_one_is_allowed() -> None:
    # max(light_exptimes) / dark_exptime == 1 exactly is the boundary --
    # the plan's refusal condition is "> 1", so equality must be accepted
    # (scaled, since it's not a same-single-exptime exact-index match).
    cal_index = {("T21", "Dark", 1, 300.0): [_dark(300.0)]}
    selection = select_dark(cal_index, "T21", 1, {300.0, 150.0})
    assert selection.scaled is True
    assert selection.exptime == 300.0


def test_select_dark_never_falls_back_across_binnings() -> None:
    # A dark exists, but only at a DIFFERENT binning -- a different binning
    # is a different pixel dimension, not just a different exposure time,
    # and must never be substituted even when it's the only dark around.
    cal_index = {("T21", "Dark", 2, 900.0): [_dark(900.0, binning=2)]}
    with pytest.raises(CalibrationFramesMissingError):
        select_dark(cal_index, "T21", 1, {600.0})


def test_select_dark_picks_closest_from_above_when_multiple_available() -> None:
    cal_index = {
        ("T21", "Dark", 1, 600.0): [_dark(600.0)],
        ("T21", "Dark", 1, 900.0): [_dark(900.0)],
    }
    selection = select_dark(cal_index, "T21", 1, {300.0})
    assert selection.scaled is True
    assert selection.exptime == 600.0  # closer from above than 900s


@requires_real_session
def test_select_dark_real_t21_luminance_group() -> None:
    """Real-data check: T21's actual delivery has darks only at 900s (both
    binnings), and its real Luminance group is 600s/300s mixed -- exactly
    the scaled path, and safe (k0 < 1 both ways)."""
    report = scan_session(REAL_SESSION_DIR)
    cal_index = report.calibration_index()
    lights = report.instrument_groups()[("T21", "M51", "Luminance", 1)]
    light_exptimes = {f.exptime for f in lights}
    assert light_exptimes == {600.0, 300.0}

    selection = select_dark(cal_index, "T21", 1, light_exptimes)
    assert selection.scaled is True
    assert selection.exptime == 900.0
    assert len(selection.frames) == 25


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
    lum_lights = groups[("T24", "kaveh096", "M51", "Luminance", 1)]
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

    # Real, critical regression guard: bias+dark-only calibration (no flat)
    # routinely leaves the background slightly negative on average, and
    # Siril's stack output was found to clip negative-averaged pixels to
    # exact 0.0 -- destroying the background's continuous noise texture
    # (>99.9% of a real master ended up exactly zero) and silently
    # breaking GraXpert's background extraction downstream with 100% NaN
    # output and no error. The default pedestal must keep the calibrated
    # data comfortably non-negative so this can't happen.
    assert calibrated_data.min() > 0


@requires_siril
@requires_real_session
def test_calibrate_and_stack_produces_no_exact_zero_background(tmp_path: Path) -> None:
    """Direct regression test for the real bug: without the pedestal,
    stacking a bias+dark-only-calibrated sequence produced a master that
    was >99.9% exact zero. With the default pedestal, the stacked master
    must have a real, continuous, non-clipped background.
    """
    from astro_pipeline.registration_stacking import register_and_stack

    report = scan_session(REAL_SESSION_DIR)
    groups = report.light_groups()
    lum_lights = groups[("T24", "kaveh096", "M51", "Luminance", 1)]
    cal_index = report.calibration_index()
    bias = cal_index[("T24", "Bias", 1, 0.0)]
    dark = cal_index[("T24", "Dark", 1, 300.0)]

    run_calibration(lum_lights, bias, dark, work_dir=tmp_path, flat_frames=None)
    stack_result = register_and_stack("pp_lights_", tmp_path / "lights", out_name="master_lum")

    master_data = fits.getdata(stack_result.master_path)
    zero_fraction = float(np.sum(master_data == 0)) / master_data.size
    assert zero_fraction < 0.01, (
        f"{zero_fraction:.1%} of the master is exact zero -- the background-clipping "
        "bug is back."
    )
    assert master_data.min() > 0
