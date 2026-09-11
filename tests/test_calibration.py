from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.calibration import (
    CalibrationFramesMissingError,
    CalibrationMode,
    FlatPolicy,
    _calibrate_command,
    build_master,
    build_master_flat,
    calibrate_lights,
    run_calibration,
    select_dark,
    sequence_name,
    stage_precalibrated_lights,
)
from astro_pipeline.ingest import CalibrationFrame, scan_session
from astro_pipeline.siril_driver import find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")

from conftest import ABELL6_PROJECT_DIR, requires_abell6_project
from conftest import NGC3628_PROJECT_DIR, requires_ngc3628_project
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


def test_stage_precalibrated_lights_raises_on_empty_light_list(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError):
        stage_precalibrated_lights([], tmp_path)


def test_calibration_mode_values() -> None:
    assert CalibrationMode.RAW_LOCAL == "raw_local"
    assert CalibrationMode.PRECALIBRATED == "precalibrated"


@requires_siril
@requires_ngc3628_project
def test_stage_precalibrated_lights_real_t73_produces_pp_lights_sequence(tmp_path: Path) -> None:
    """Real T73 NGC 3628 calibrated-provenance Red BIN2 lights, staged
    directly (no bias/dark/flat) -- confirms the seam claim from
    plan-precalibrated-path.md: converting under basename "pp_lights"
    produces a sequence literally named "pp_lights_", matching what
    pipeline.build_master()'s hardcoded register_and_stack("pp_lights_",
    ...) already expects, with zero change needed downstream."""
    report = scan_session(NGC3628_PROJECT_DIR)
    groups = report.calibrated_instrument_groups()
    lights = groups[("T73", "NGC 3628", "Red", 2)]
    assert len(lights) == 12

    staged = stage_precalibrated_lights(lights, tmp_path, basename="lights")

    assert len(staged) == 12
    assert all(p.name.startswith("pp_lights_") for p in staged)
    for path in staged:
        data = fits.getdata(path)
        assert data.dtype == np.dtype(">f4")
        assert not np.any(data == 0.0), f"{path.name} has exact-zero pixels -- pedestal should prevent this"


@requires_siril
@requires_abell6_project
def test_stage_precalibrated_lights_debayer_real_t02_produces_3channel(tmp_path: Path) -> None:
    """Real T02 Abell 6 and HFG1 calibrated-provenance Color BIN1 lights --
    genuine undemosaiced Bayer-mosaic (RGGB) data, confirmed via a 2x2-phase
    periodicity test and a leftover DeepSkyStacker config independently
    agreeing on the same pattern (see plan-rgb-only-mode.md ??0). Confirms
    the debayer=True path actually demosaics: MEASURED against real Siril
    1.4.4 that plain `convert` alone leaves this 2D (BAYERPAT absent from
    the header, so nothing marks it undemosaiced downstream unless this
    function's debayer step runs); with debayer=True, every staged file
    must come out genuinely 3-channel (3, ny, nx), not (ny, nx)."""
    report = scan_session(ABELL6_PROJECT_DIR)
    groups = report.calibrated_instrument_groups()
    lights = groups[("T02", "Abell 6 and HFG1", "Color", 1)]
    assert len(lights) == 7

    staged = stage_precalibrated_lights(lights, tmp_path, basename="lights", debayer=True, bayer_pattern=0)

    assert len(staged) == 7
    for path in staged:
        data = fits.getdata(path)
        assert data.ndim == 3 and data.shape[0] == 3, f"{path.name}: expected (3, ny, nx), got {data.shape}"
        assert data.dtype == np.dtype(">f4")
        # Real per-channel means must differ meaningfully -- a degenerate
        # "debayer" that just replicated one plane three times would pass
        # a bare shape check but not this.
        means = [float(data[c].mean()) for c in range(3)]
        assert len(set(round(m, 6) for m in means)) == 3, f"{path.name}: channels look degenerate: {means}"


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


# --- Slice 2.1: _calibrate_command's real THIRD state ("no dark at all"),
# the exact thing round 2 of plan-flats-v3.md found round 1's own fix had
# NOT actually produced. All three states asserted together here so a
# regression in any one is caught by the same test module. -----------------


def test_calibrate_command_dark_only_state_unchanged_by_slice2() -> None:
    """State (a): dark-only, dark_optimize=False -- today's only real light
    path. Must be byte-identical to before Slice 2's dark_stem-optional
    change (this is the same assertion as
    test_calibrate_command_t24_shaped_call_is_byte_identical_to_today,
    repeated here so all three states are visible side by side)."""
    cmd = _calibrate_command("lights_", "masterdark", None, None, dark_optimize=False)
    assert cmd == "calibrate lights_ -dark=masterdark -cc=dark -prefix=pp_"


def test_calibrate_command_dark_optimized_state_unchanged_by_slice2() -> None:
    """State (b): dark-optimized (-opt=exp), T21's scaled-dark path. Must
    also be byte-identical to before Slice 2 -- the new dark_stem=None
    branch must not perturb this existing branch."""
    cmd = _calibrate_command("lights_", "masterdark", "masterbias", None, dark_optimize=True)
    assert cmd == "calibrate lights_ -dark=masterdark -bias=masterbias -opt=exp -prefix=pp_"


def test_calibrate_command_no_dark_state_matches_fact8_bias_only_command() -> None:
    """State (c): dark_stem=None -- the new state build_master_flat() needs.
    Must produce EXACTLY fact 8's real, live-Siril-verified command: no
    -dark=, no -cc=dark (round 1's first-draft bug: -cc=dark fires off
    dark_optimize alone, independent of dark_stem), no -opt=exp, and the
    output prefix must be "bc_" (not the default "pp_") so
    build_master_flat's own follow-on `stack bc_<seq>...` command actually
    matches something.
    """
    cmd = _calibrate_command(
        "flat_", dark_stem=None, bias_stem="masterbias", flat_stem=None,
        dark_optimize=False, prefix="bc_",
    )
    assert cmd == "calibrate flat_ -bias=masterbias -prefix=bc_"
    assert "-dark=" not in cmd
    assert "-cc=dark" not in cmd
    assert "-opt=exp" not in cmd


def test_calibrate_command_no_dark_state_ignores_dark_optimize_true() -> None:
    """dark_optimize=True must NOT resurrect -opt=exp when dark_stem=None
    -- -opt requires an actual dark master (Siril's own `help calibrate`),
    so this combination would otherwise be a physically meaningless
    command that happens to not error."""
    cmd = _calibrate_command(
        "flat_", dark_stem=None, bias_stem="masterbias", flat_stem=None,
        dark_optimize=True, prefix="bc_",
    )
    assert cmd == "calibrate flat_ -bias=masterbias -prefix=bc_"
    assert "-opt=exp" not in cmd


# --- Slice 2 verification: T24 property check, not a rerun -- mirrors
# rev4 Slice 1's own pattern (Siril stamps a wall-clock DATE header, so
# byte-identical file comparison across real runs is unachievable by
# construction; a command-string property check is the checkable proxy). --


@requires_real_session
def test_t24_real_groups_have_no_matched_flats_and_command_is_unaffected() -> None:
    """For every real T24 (telescope, binning, filter) light group this
    delivery actually needs, flat_index().get(...) must return [] (T24
    ships zero flats of any kind, structurally, for this delivery -- see
    plan-flats-v3.md's Context section) -> flat_frames=[] ->
    run_calibration's existing `if flat_frames: build master` logic takes
    the falsy path exactly as before this slice -> calibrate_lights would
    pass flat_stem=None to _calibrate_command -> the emitted `calibrate`
    command string for every real T24 group is BYTE-IDENTICAL to before
    Slice 2, with no -flat= token at all. Checkable without invoking
    Siril, matching every one of this file's other command-string
    property checks.
    """
    report = scan_session(REAL_SESSION_DIR)
    flat_index = report.flat_index()

    # T24's real light groups, per test_ingest.py's own
    # test_instrument_groups_merges_users_sharing_telescope_and_binning /
    # test_missing_calibration_warnings_real_per_filter_flat_check.
    t24_groups = [
        (1, "Luminance"), (1, "Red"), (1, "Green"), (1, "Blue"),
        (2, "Red"), (2, "Green"), (2, "Blue"),
    ]
    for binning, filter_name in t24_groups:
        flat_frames = flat_index.get(("T24", binning, filter_name), [])
        assert flat_frames == [], (
            f"T24 BIN{binning}/{filter_name} unexpectedly has matched flats -- "
            "this delivery is supposed to have zero T24 flats of any kind."
        )
        # The exact command calibrate_lights() would build for this group:
        # flat_stem=None (from an empty flat_frames list) must produce the
        # same byte-identical, no -flat= command as every existing T24
        # command-string property check in this file.
        cmd = _calibrate_command("lights_", "masterdark", None, None, dark_optimize=False)
        assert cmd == "calibrate lights_ -dark=masterdark -cc=dark -prefix=pp_"
        assert "-flat=" not in cmd


# --- build_master_flat: real bias-subtraction-before-stacking, on a fast
# synthetic (seeded, no real data needed) fixture -- mirrors fact 8's real
# measurement (raw-mean-minus-bias-mean) without a full 30-frame real-data
# round trip on every CI run. -----------------------------------------------


@requires_siril
def test_build_master_flat_bias_subtracts_before_stacking(tmp_path: Path) -> None:
    """A known bias level is burned into synthetic flat frames (uint16, the
    real raw-flat dtype -- see the real T21 flats, BITPIX=16) plus a
    separate synthetic master bias. Siril's `convert` preserves raw ADU
    values verbatim (verified directly: a 20000-ADU synthetic probe frame
    converts to exactly 20000, no scaling), but its internal working
    representation (used by `calibrate`/`stack`) normalizes 16-bit input by
    65535 -- also verified directly against this exact code path before
    writing this test (avg raw flat 20498.46 ADU, bias 499.76 ADU ->
    (20498.46-499.76)/65535 = 0.305163, and the real built master's mean
    came back 0.30516237, matching to 5 decimal places). So the assertion
    below compares the built master's mean against
    (raw_mean - bias_mean) / 65535, not raw ADU directly.
    """
    rng = np.random.default_rng(20260907)
    bias_level = 500.0
    flat_signal = 20000.0
    shape = (32, 32)

    raw_dir = tmp_path / "raw_flats"
    raw_dir.mkdir()
    flat_frames: list[CalibrationFrame] = []
    raw_means: list[float] = []
    for i in range(5):
        data = rng.normal(loc=bias_level + flat_signal, scale=50.0, size=shape).astype(np.uint16)
        path = raw_dir / f"flat_{i}.fit"
        fits.PrimaryHDU(data=data).writeto(path)
        raw_means.append(float(fits.getdata(path).astype(np.float64).mean()))
        flat_frames.append(
            CalibrationFrame(
                path=path, telescope="T99", frame_type="Flat", binning=1,
                exptime=0.0, filter_name="Luminance",
            )
        )

    bias_data = rng.normal(loc=bias_level, scale=5.0, size=shape).astype(np.uint16)
    bias_path = tmp_path / "master_bias.fit"
    fits.PrimaryHDU(data=bias_data).writeto(bias_path)
    bias_mean = float(bias_data.astype(np.float64).mean())

    master_path = build_master_flat(flat_frames, bias_path, tmp_path / "work")
    master_data = fits.getdata(master_path).astype(np.float64)

    expected = (sum(raw_means) / len(raw_means) - bias_mean) / 65535.0
    assert master_data.mean() == pytest.approx(expected, rel=0.01)


def test_build_master_flat_raises_on_empty_frame_list(tmp_path: Path) -> None:
    with pytest.raises(CalibrationFramesMissingError):
        build_master_flat([], tmp_path / "bias.fit", tmp_path)


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
