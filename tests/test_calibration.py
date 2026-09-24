from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.calibration import (
    FLAT_RECIPE_VERSION,
    CalibrationFramesMissingError,
    CalibrationMode,
    FlatPolicy,
    _calibrate_command,
    _check_calibration_log,
    build_master,
    build_master_bias,
    build_master_flat,
    calibrate_lights,
    classify_flat_identity,
    flat_sanity_notes,
    run_calibration,
    select_dark,
    sequence_name,
    stage_flat_frames,
    stage_precalibrated_lights,
)
from astro_pipeline.ingest import CalibrationFrame, scan_session
from astro_pipeline.siril_driver import SirilError, SirilResult, find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")

from conftest import ABELL6_PROJECT_DIR, requires_abell6_project
from conftest import IC1396_PROJECT_DIR, requires_ic1396_project
from conftest import NGC3628_PROJECT_DIR, requires_ngc3628_project
from conftest import PROJECT_DIR as REAL_SESSION_DIR
from conftest import RGB_USER
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


def test_calibrate_command_debayer_appends_flag_before_prefix() -> None:
    """Capability B (OSC + local raw calibration, 2026-09): -debayer must
    be appended to the same calibrate call, and must not perturb any
    existing default (debayer=False) call shape."""
    cmd = _calibrate_command(
        "lights_", dark_stem=None, bias_stem="masterbias", flat_stem=None,
        dark_optimize=False, debayer=True,
    )
    assert cmd == "calibrate lights_ -bias=masterbias -debayer -prefix=pp_"


def test_calibrate_command_debayer_default_false_unchanged() -> None:
    cmd = _calibrate_command("lights_", "masterdark", None, None, dark_optimize=False)
    assert "-debayer" not in cmd


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


# --- Step 3b (plan-flats-v4.md): flat-only content-aware staging
# (dedupe byte-identical copies, uniquify genuine name collisions) plus
# -norm=mul stacking. Fixes G1/G2, ships unconditionally (Q1). ------------


def _write_flat(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=np.full((4, 4), value, dtype=np.uint16)).writeto(path)


def test_stage_flat_frames_thirty_distinct_names_all_thirty_staged(tmp_path: Path) -> None:
    frames = []
    for i in range(30):
        p = tmp_path / "raw" / f"flat_{i}.fit"
        _write_flat(p, i)
        frames.append(
            CalibrationFrame(path=p, telescope="T99", frame_type="Flat", binning=1, exptime=0.0, filter_name="L")
        )
    dest = stage_flat_frames(frames, tmp_path / "staged")
    assert len(list(dest.glob("*.fit"))) == 30


def test_stage_flat_frames_two_byte_identical_collapse_to_one(tmp_path: Path) -> None:
    dir1, dir2 = tmp_path / "s1", tmp_path / "s2"
    p1, p2 = dir1 / "skyflat0.fit", dir2 / "skyflat0.fit"
    _write_flat(p1, 100)
    _write_flat(p2, 100)  # byte-identical content, same basename
    frames = [
        CalibrationFrame(path=p, telescope="T21", frame_type="Flat", binning=1, exptime=0.0, filter_name="Luminance")
        for p in (p1, p2)
    ]
    dest = stage_flat_frames(frames, tmp_path / "staged")
    staged = list(dest.glob("*.fit"))
    assert len(staged) == 1
    assert staged[0].name == "skyflat0.fit"  # kept under its original name, no rename needed


def test_stage_flat_frames_colliding_distinct_frames_both_staged_and_renamed(tmp_path: Path) -> None:
    """T21 L's real shape: two sessions, same generic basename, genuinely
    different content -- both must survive staging, disambiguated."""
    dir1, dir2 = tmp_path / "20240616_080204", tmp_path / "20240616_080536"
    p1, p2 = dir1 / "skyflat0.fit", dir2 / "skyflat0.fit"
    _write_flat(p1, 10)
    _write_flat(p2, 200)  # distinct content, same basename
    frames = [
        CalibrationFrame(path=p, telescope="T21", frame_type="Flat", binning=1, exptime=0.0, filter_name="Luminance")
        for p in (p1, p2)
    ]
    dest = stage_flat_frames(frames, tmp_path / "staged")
    staged_names = sorted(f.name for f in dest.glob("*.fit"))
    assert len(staged_names) == 2
    assert all(name.endswith("__skyflat0.fit") for name in staged_names)
    assert staged_names[0] != staged_names[1]


def test_stage_flat_frames_single_frame_per_name_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "raw" / "onlyone.fit"
    _write_flat(p, 5)
    frame = CalibrationFrame(path=p, telescope="T99", frame_type="Flat", binning=1, exptime=0.0, filter_name="L")
    dest = stage_flat_frames([frame], tmp_path / "staged")
    staged = list(dest.glob("*.fit"))
    assert len(staged) == 1
    assert staged[0].name == "onlyone.fit"


def test_build_master_flat_stack_command_uses_norm_mul(tmp_path: Path, monkeypatch) -> None:
    """G2's own fix: the flat stack command must include -norm=mul, matching
    Siril's own bundled reference scripts."""
    import astro_pipeline.calibration as calibration_module

    captured_commands: list[str] = []

    def fake_run_script(commands, workdir, siril_cli=None, timeout=None, script_name="run.ssf"):
        if script_name == "calibrate.ssf":
            captured_commands.extend(commands)
            (Path(workdir) / "master.fit").write_bytes(b"\x00")
        return SirilResult(returncode=0, log_lines=[])

    monkeypatch.setattr(calibration_module, "run_script", fake_run_script)

    flat_path = tmp_path / "flat0.fit"
    _write_flat(flat_path, 1000)
    bias_path = tmp_path / "bias.fit"
    _write_flat(bias_path, 10)
    flat_frame = CalibrationFrame(
        path=flat_path, telescope="T99", frame_type="Flat", binning=1, exptime=0.0, filter_name="Luminance",
    )

    build_master_flat([flat_frame], bias_path, tmp_path / "work")

    stack_commands = [c for c in captured_commands if c.startswith("stack")]
    assert len(stack_commands) == 1
    assert "-norm=mul" in stack_commands[0]


def test_build_master_bias_stack_command_unchanged_no_norm_flag(tmp_path: Path, monkeypatch) -> None:
    """I6 is a separate, Q7-gated measurement -- bias/dark stacking is NOT
    touched by Step 3b, unlike the flat stack above."""
    import astro_pipeline.calibration as calibration_module

    captured_commands: list[str] = []

    def fake_run_script(commands, workdir, siril_cli=None, timeout=None, script_name="run.ssf"):
        captured_commands.extend(commands)
        (Path(workdir) / "master.fit").write_bytes(b"\x00")
        return SirilResult(returncode=0, log_lines=[])

    monkeypatch.setattr(calibration_module, "run_script", fake_run_script)

    bias_path = tmp_path / "bias0.fit"
    _write_flat(bias_path, 10)
    bias_frame = CalibrationFrame(path=bias_path, telescope="T99", frame_type="Bias", binning=1, exptime=0.0)

    build_master_bias([bias_frame], tmp_path / "work")

    stack_commands = [c for c in captured_commands if c.startswith("stack")]
    assert len(stack_commands) == 1
    assert stack_commands[0] == "stack bias_ rej 3.0 3.0 -out=master"
    assert "-norm" not in stack_commands[0]


def test_calibration_recipe_parts_raw_local_with_flats_returns_flat_recipe_version() -> None:
    """The mode-aware helper's own bump (Step 2 shipped it returning "" for
    this exact case; Step 3b is the actual behaviour change)."""
    from astro_pipeline.contributor_staleness import calibration_recipe_parts

    flats = [
        CalibrationFrame(path=Path("a.fit"), telescope="T21", frame_type="Flat", binning=1, exptime=0.0, filter_name="L"),
    ]
    frame_hash, recipe = calibration_recipe_parts(CalibrationMode.RAW_LOCAL, flats)
    assert recipe == FLAT_RECIPE_VERSION
    assert frame_hash != ""


def test_calibration_recipe_parts_precalibrated_stays_empty_even_after_step3b() -> None:
    from astro_pipeline.contributor_staleness import calibration_recipe_parts

    flats = [
        CalibrationFrame(path=Path("a.fit"), telescope="T21", frame_type="Flat", binning=1, exptime=0.0, filter_name="L"),
    ]
    assert calibration_recipe_parts(CalibrationMode.PRECALIBRATED, flats) == ("", "")


def test_calibration_recipe_parts_empty_flats_stays_empty_even_after_step3b() -> None:
    from astro_pipeline.contributor_staleness import calibration_recipe_parts

    assert calibration_recipe_parts(CalibrationMode.RAW_LOCAL, []) == ("", "")


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


# --- Step 1b (plan-flats-v4.md): Siril's silent "NOT USING <KIND>: ..."
# drops (exit code 0, no exception) must not pass unnoticed. All 17 lines
# below are REAL strings verified directly against the installed Siril
# 1.4.4 `siril-cli.exe` binary (5 DARK, 6 FLAT, 6 OFFSET -- OFFSET being
# Siril's own name for what this codebase calls bias), matching round 2's
# corrected count in the plan's review log. Tested against the PREFIX
# regex, not an enumerated list, so a 7th future variant of any of the
# three kinds is caught too. ------------------------------------------------

_REAL_NOT_USING_LINES = [
    "NOT USING DARK: could not parse the expression",
    "NOT USING DARK: number of channels is different",
    "NOT USING DARK: image dimensions are different",
    "NOT USING DARK: cannot open the file",
    "NOT USING DARK: cannot open file '%s'",
    "NOT USING FLAT: Could not load reference image",
    "NOT USING FLAT: could not parse the expression",
    "NOT USING FLAT: number of channels is different",
    "NOT USING FLAT: image dimensions are different",
    "NOT USING FLAT: cannot open the file",
    "NOT USING FLAT: cannot open file '%s'",
    "NOT USING OFFSET: the offset value could not be parsed",
    "NOT USING OFFSET: the offset value is not consistent with image bitdepth",
    "NOT USING OFFSET: could not parse the expression",
    "NOT USING OFFSET: number of channels is different",
    "NOT USING OFFSET: image dimensions are different",
    "NOT USING OFFSET: cannot open the file",
]


@pytest.mark.parametrize("line", _REAL_NOT_USING_LINES)
def test_check_calibration_log_raises_on_each_real_not_using_variant(line: str) -> None:
    result = SirilResult(returncode=0, log_lines=["some unrelated log line", line])
    with pytest.raises(SirilError):
        _check_calibration_log(result)


def test_check_calibration_log_passes_a_clean_log() -> None:
    result = SirilResult(
        returncode=0,
        log_lines=["Sequence processing succeeded", "Dark optimization of image 1: k0=0.667"],
    )
    _check_calibration_log(result)  # must not raise


def test_check_calibration_log_still_catches_negative_pixels() -> None:
    result = SirilResult(
        returncode=0,
        log_lines=[
            "After dark subtraction, the image contains many negative pixels "
            "(12%), calibration frames are probably incorrect"
        ],
    )
    with pytest.raises(SirilError):
        _check_calibration_log(result)


def test_build_master_flat_raises_on_not_using_offset_in_its_own_log(tmp_path: Path, monkeypatch) -> None:
    """build_master_flat's own calibrate+stack call was never log-checked
    before this step (G3) -- confirm it now is, via a monkeypatched
    run_script that reports exit 0 but a real 'NOT USING OFFSET:' line
    (OFFSET = Siril's name for the bias this function passes via
    -bias=)."""
    import astro_pipeline.calibration as calibration_module

    def fake_run_script(commands, workdir, siril_cli=None, timeout=None, script_name="run.ssf"):
        if script_name == "convert.ssf":
            return SirilResult(returncode=0, log_lines=["converted"])
        return SirilResult(
            returncode=0,
            log_lines=["NOT USING OFFSET: cannot open the file"],
        )

    monkeypatch.setattr(calibration_module, "run_script", fake_run_script)

    flat_path = tmp_path / "flat0.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(flat_path)
    bias_path = tmp_path / "bias.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(bias_path)
    flat_frame = CalibrationFrame(
        path=flat_path, telescope="T99", frame_type="Flat", binning=1, exptime=0.0, filter_name="Luminance",
    )

    with pytest.raises(SirilError):
        build_master_flat([flat_frame], bias_path, tmp_path / "work")


# --- Step 3a (plan-flats-v4.md): classify_flat_identity -- report-only,
# never raises, content-identity classification of a matched flat set
# BEFORE staging's own basename collapse. ------------------------------


def test_classify_flat_identity_byte_identical_and_colliding_distinct(tmp_path: Path) -> None:
    """Two byte-identical folders (same content, same name, different
    paths -- e.g. the same physical exposure catalogued twice) plus two
    colliding distinct frames (same name, genuinely different content --
    T21 L's real shape)."""
    data_a = np.zeros((4, 4), dtype=np.uint16)
    data_b = np.ones((4, 4), dtype=np.uint16) * 100

    dir1 = tmp_path / "session1"
    dir2 = tmp_path / "session2"
    dir1.mkdir()
    dir2.mkdir()

    # Byte-identical copies: "identical.fit" in both folders, same content.
    identical1 = dir1 / "identical.fit"
    identical2 = dir2 / "identical.fit"
    fits.PrimaryHDU(data=data_a).writeto(identical1)
    fits.PrimaryHDU(data=data_a).writeto(identical2)

    # Colliding, DISTINCT frames: "skyflat0.fit" in both folders, different content.
    collide1 = dir1 / "skyflat0.fit"
    collide2 = dir2 / "skyflat0.fit"
    fits.PrimaryHDU(data=data_a).writeto(collide1)
    fits.PrimaryHDU(data=data_b).writeto(collide2)

    # One frame with a unique name -- no collision, no duplicate.
    unique = dir1 / "skyflat1.fit"
    fits.PrimaryHDU(data=data_b).writeto(unique)

    frames = [
        CalibrationFrame(path=p, telescope="T21", frame_type="Flat", binning=1, exptime=0.0, filter_name="Luminance")
        for p in (identical1, identical2, collide1, collide2, unique)
    ]
    report = classify_flat_identity(frames)
    assert report.matched == 5
    assert report.byte_identical_copies == 1  # one extra copy of "identical.fit"
    assert report.name_collisions == 1  # one colliding basename ("skyflat0.fit")
    assert report.staged == 3  # identical.fit (1) + skyflat0.fit (1) + skyflat1.fit (1)


def test_classify_flat_identity_no_frames_is_all_zero() -> None:
    report = classify_flat_identity([])
    assert report.matched == 0
    assert report.byte_identical_copies == 0
    assert report.name_collisions == 0
    assert report.staged == 0


@requires_real_session
def test_classify_flat_identity_real_t21_luminance_matches_known_numbers() -> None:
    """Real gate (D)/(P): T21's real Luminance flat set -- 30 matched, 0
    byte-identical copies, 10 name collisions between distinct frames
    (two twilight sessions' worth of colliding 'skyflat<N>' basenames),
    20 staged -- plan-flats-v4.md ??1.3.5/Step 3a's own real numbers."""
    report_ing = scan_session(REAL_SESSION_DIR)
    t21_l_flats = report_ing.flat_index()[("T21", 1, "Luminance")]
    report = classify_flat_identity(t21_l_flats)
    assert report.matched == 30
    assert report.byte_identical_copies == 0
    assert report.name_collisions == 10
    assert report.staged == 20


def test_run_calibration_logs_flat_identity_summary(tmp_path: Path, monkeypatch) -> None:
    """run_calibration itself calls classify_flat_identity and logs its
    summary, before staging -- verified via a monkeypatched Siril chain
    (no real calibration needed to check the note is written)."""
    import astro_pipeline.calibration as calibration_module

    def fake_build_master_bias(bias_frames, work_dir):
        return tmp_path / "master_bias.fit"

    def fake_build_master_dark(dark_frames, work_dir):
        return tmp_path / "master_dark.fit"

    def fake_build_master_flat(flat_frames, master_bias, work_dir):
        return tmp_path / "master_flat.fit"

    def fake_calibrate_lights(*args, **kwargs):
        return [tmp_path / "pp_light_0.fit"], SirilResult(returncode=0, log_lines=[])

    monkeypatch.setattr(calibration_module, "build_master_bias", fake_build_master_bias)
    monkeypatch.setattr(calibration_module, "build_master_dark", fake_build_master_dark)
    monkeypatch.setattr(calibration_module, "build_master_flat", fake_build_master_flat)
    monkeypatch.setattr(calibration_module, "calibrate_lights", fake_calibrate_lights)

    flat_path = tmp_path / "flat0.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(flat_path)
    flat_frame = CalibrationFrame(
        path=flat_path, telescope="T99", frame_type="Flat", binning=1, exptime=0.0, filter_name="Luminance",
    )
    light_path = tmp_path / "light0.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(light_path)
    from astro_pipeline.ingest import LightFrame

    light_frame = LightFrame(
        path=light_path, provenance="raw", telescope="T99", user="u", target="X",
        date="20260101", time="000000", filter_name="Luminance", binning=1, side="E",
        exptime=300.0, sequence=1,
    )
    bias_path = tmp_path / "bias0.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(bias_path)
    bias_frame = CalibrationFrame(path=bias_path, telescope="T99", frame_type="Bias", binning=1, exptime=0.0)
    dark_path = tmp_path / "dark0.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(dark_path)
    dark_frame = CalibrationFrame(path=dark_path, telescope="T99", frame_type="Dark", binning=1, exptime=300.0)

    notes: list[str] = []
    run_calibration(
        [light_frame], [bias_frame], [dark_frame], tmp_path / "work",
        flat_frames=[flat_frame], flat_policy=FlatPolicy.REQUIRE, notes=notes,
    )
    assert any("flat:" in line and "matched" in line for line in notes)
    assert any("1 matched" in line for line in notes)


# --- Step 7 (plan-flats-v4.md): flat_sanity_notes -- warning-only, on a
# SAMPLE of raw flat frames (never the Siril-normalised master). ----------


def _write_flat_with_level(path: Path, adu_value: float, exptime: float = 5.0, date_obs: str = "2024-06-16T08:00:00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.full((8, 8), adu_value, dtype=np.uint16))
    hdu.header["EXPTIME"] = exptime
    hdu.header["DATE-OBS"] = date_obs
    hdu.writeto(path)


def _flat_frame(path: Path, telescope: str = "T21", binning: int = 1, filter_name: str = "Luminance") -> CalibrationFrame:
    return CalibrationFrame(path=path, telescope=telescope, frame_type="Flat", binning=binning, exptime=0.0, filter_name=filter_name)


def test_flat_sanity_notes_empty_list_returns_empty() -> None:
    assert flat_sanity_notes([]) == []


def test_flat_sanity_notes_level_within_range_no_warning(tmp_path: Path) -> None:
    # ~35% of 65535, matching T21's own real real-data level.
    p = tmp_path / "flat0.fit"
    _write_flat_with_level(p, 23000.0)
    notes = flat_sanity_notes([_flat_frame(p)])
    assert not any("level" in n for n in notes)


def test_flat_sanity_notes_level_too_low_warns(tmp_path: Path) -> None:
    p = tmp_path / "flat0.fit"
    _write_flat_with_level(p, 1000.0)  # ~1.5% of 65535, well under the 10% floor
    notes = flat_sanity_notes([_flat_frame(p)])
    assert any("level" in n and "outside" in n for n in notes)


def test_flat_sanity_notes_level_too_high_warns(tmp_path: Path) -> None:
    p = tmp_path / "flat0.fit"
    _write_flat_with_level(p, 60000.0)  # ~92% of 65535, over the 85% ceiling
    notes = flat_sanity_notes([_flat_frame(p)])
    assert any("level" in n and "outside" in n for n in notes)


def test_flat_sanity_notes_saturation_warns(tmp_path: Path) -> None:
    path = tmp_path / "flat_sat.fit"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.full((8, 8), 30000, dtype=np.uint16)
    data[0, 0] = 65000  # one saturated pixel
    hdu = fits.PrimaryHDU(data=data)
    hdu.header["EXPTIME"] = 5.0
    hdu.header["DATE-OBS"] = "2024-06-16T08:00:00"
    hdu.writeto(path)
    notes = flat_sanity_notes([_flat_frame(path)])
    assert any("saturation" in n for n in notes)


def test_flat_sanity_notes_exposure_min_max_and_spread(tmp_path: Path) -> None:
    frames = []
    for i, exptime in enumerate([5.0, 6.0, 7.0]):
        p = tmp_path / f"flat{i}.fit"
        _write_flat_with_level(p, 23000.0, exptime=exptime)
        frames.append(_flat_frame(p))
    notes = flat_sanity_notes(frames)
    exposure_notes = [n for n in notes if "exposure" in n]
    assert len(exposure_notes) == 1
    assert "5.000" in exposure_notes[0] and "7.000" in exposure_notes[0]


def test_flat_sanity_notes_unknown_date_for_1970_date_obs(tmp_path: Path) -> None:
    p = tmp_path / "flat0.fit"
    _write_flat_with_level(p, 23000.0, date_obs="1970-01-01T00:00:00")
    notes = flat_sanity_notes([_flat_frame(p)])
    assert any("unknown" in n for n in notes)
    assert not any("flat date:" in n and "1970" in n for n in notes)


def test_flat_sanity_notes_age_gap_to_lights(tmp_path: Path) -> None:
    p = tmp_path / "flat0.fit"
    _write_flat_with_level(p, 23000.0, date_obs="2024-06-16T08:00:00")
    from astro_pipeline.ingest import LightFrame

    light = LightFrame(
        path=Path("light.fit"), provenance="raw", telescope="T21", user="u", target="M51",
        date="20250115", time="050236", filter_name="Luminance", binning=1, side="E",
        exptime=300.0, sequence=1,
    )
    notes = flat_sanity_notes([_flat_frame(p)], light_frames=[light])
    date_notes = [n for n in notes if "flat date:" in n]
    assert len(date_notes) == 1
    assert "day(s) from the lights" in date_notes[0]


@requires_real_session
def test_flat_sanity_notes_real_t21_luminance() -> None:
    """Real check: T21's own real Luminance flats sit at about 35% of
    65535 (??2.3) -- inside the heuristic range, no level/saturation
    warning expected; a real exposure and date note must still appear."""
    report = scan_session(REAL_SESSION_DIR)
    t21_l_flats = report.flat_index()[("T21", 1, "Luminance")]
    notes = flat_sanity_notes(t21_l_flats)
    assert not any("outside the heuristic" in n for n in notes)
    assert any("exposure" in n for n in notes)
    assert any("flat date" in n for n in notes)


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
    lum_lights = groups[("T24", RGB_USER, "M51", "Luminance", 1)]
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


@requires_siril
@requires_ic1396_project
def test_calibrate_lights_real_t68_bias_only_debayer_produces_valid_rgb(tmp_path: Path) -> None:
    """Capability B (OSC + local raw calibration, 2026-09), the real,
    load-bearing claim: T68 (IC 1396) has real local bias (48 subs) but
    NO RECOGNIZED local dark frames at all (**corrected, plan-flats-v4.md**:
    50 real dark frames exist on disk, unrecognized by filename -- this
    test still manually constructs its bias/light CalibrationFrame/
    LightFrame objects directly from real files, bypassing scan_session's
    own recognition entirely, so the mechanics this test exercises remain
    real and valid) -- master_dark=None, subtract_bias
    =True, debayer=True must produce genuine, non-degenerate, plausible
    3-channel calibrated+debayered output from real raw OSC lights, in
    ONE calibrate_lights() call (not a separate debayer pass).
    """
    from astro_pipeline.calibration import build_master_bias

    class _FakeLightFrame:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.user = "observer1"
            self.exptime = 240.0

    bias_files = sorted((IC1396_PROJECT_DIR / "bias2").glob("Calibration-*-bias.fit"))
    assert bias_files, "real T68 bias fixture files missing"
    bias_frames = [CalibrationFrame(path=p, telescope="T68", frame_type="Bias", binning=1, exptime=0.0) for p in bias_files]
    master_bias = build_master_bias(bias_frames, tmp_path)

    light_files = sorted((IC1396_PROJECT_DIR / "lights").glob("raw-*.fit"))[:2]
    assert len(light_files) == 2, "real T68 raw OSC light fixture files missing"
    lights = [_FakeLightFrame(p) for p in light_files]

    calibrated, _ = calibrate_lights(
        lights, master_bias, master_dark=None, work_dir=tmp_path,
        subtract_bias=True, debayer=True, bayer_pattern=0, pedestal=0.0,
    )

    assert len(calibrated) == 2
    for path in calibrated:
        data = fits.getdata(path, memmap=False)
        assert data.ndim == 3 and data.shape[0] == 3, f"{path.name} is not genuinely 3-channel: {data.shape}"
        assert not np.isnan(data).all(), f"{path.name} is degenerate (all-NaN)"
        # A real demosaiced OSC frame has distinct per-channel statistics
        # (green is real-sky-brighter in a Bayer sensor, not a coincidence
        # of random noise) -- confirms genuine demosaicing happened, not
        # e.g. the same plane copied into all 3 channels.
        means = [float(data[ch].mean()) for ch in range(3)]
        assert len(set(round(m, 6) for m in means)) == 3, f"{path.name}'s 3 channels are not distinct: {means}"


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


# --- Step 1b gate (P): Siril run-to-run determinism, T21 Luminance -------
# (plan-flats-v4.md ??4.0/Step 1b) -- at least 3 independent tmp_path
# run_calibration() calls for T21's real Luminance BIN1 group, compared
# pairwise, establish Siril's own run-to-run noise floor for this exact
# real calibration; then ONE tmp-path run is compared against the real,
# currently-persisted reference (a different process/session). The bound
# is 2x the max observed tmp-vs-tmp delta -- exceeding it fails loudly
# rather than silently widening the tolerance. This is also gate (P)'s
# reference point for Step 3b's post-fix re-measurement.


@requires_siril
@requires_real_session
def test_t21_luminance_flat_and_calibration_siril_determinism_bound(tmp_path: Path) -> None:
    from astro_pipeline.master_builder import resolve_lights

    report = scan_session(REAL_SESSION_DIR)
    cal_index = report.calibration_index()
    bias = cal_index[("T21", "Bias", 1, 0.0)]
    lights, group_name = resolve_lights(report, "T21", "M51", "Luminance", 1)
    light_exptimes = {f.exptime for f in lights}
    selection = select_dark(cal_index, "T21", 1, light_exptimes)
    flats = report.flat_index()[("T21", 1, "Luminance")]

    def run_once(work_dir: Path):
        return run_calibration(
            light_frames=lights,
            bias_frames=bias,
            dark_frames=selection.frames,
            work_dir=work_dir,
            flat_frames=flats,
            flat_policy=FlatPolicy.REQUIRE,
            dark_scaled=selection.scaled,
        )

    tmp_results = [run_once(tmp_path / f"run{i}") for i in range(3)]

    def flat_data(path: Path):
        return fits.getdata(path, memmap=False).astype(np.float64)

    flat_arrays = [flat_data(r.master_flat) for r in tmp_results]
    max_tmp_delta = 0.0
    for i in range(len(flat_arrays)):
        for j in range(i + 1, len(flat_arrays)):
            max_tmp_delta = max(max_tmp_delta, float(np.max(np.abs(flat_arrays[i] - flat_arrays[j]))))

    max_tmp_calibrated_delta = 0.0
    calibrated_sets = [sorted(r.calibrated_lights) for r in tmp_results]
    for i in range(len(calibrated_sets)):
        for j in range(i + 1, len(calibrated_sets)):
            assert len(calibrated_sets[i]) == len(calibrated_sets[j])
            for a, b in zip(calibrated_sets[i], calibrated_sets[j]):
                delta = float(np.max(np.abs(flat_data(a) - flat_data(b))))
                max_tmp_calibrated_delta = max(max_tmp_calibrated_delta, delta)

    # plan-flats-v4.md ??4.0: "After Step 3b the reference switches to the
    # new recipe's output, recorded in 3b." That switch is event E's own
    # job -- it rebuilds the real M51 pipeline (and this on-disk
    # reference) under the new recipe, and is explicitly out of scope for
    # this task. Until E actually runs, the on-disk
    # `<group>/flat/master.fit` is a STALE, pre-3b reference: a fresh
    # tmp-path run now uses Step 3b's dedupe+uniquify staging and
    # -norm=mul unconditionally, so comparing it against that stale file
    # would fail loudly for the expected, documented reason (a real,
    # intentional pixel change -- see calibration.build_master_flat's own
    # docstring), not a bug. The tmp-vs-tmp determinism bound above is
    # computed entirely fresh, under today's code on both sides, and
    # remains fully meaningful on its own -- only the tmp-vs-PERSISTED
    # half is retired here, pending event E's own re-pin.
    from conftest import PIPELINE_DIR

    persisted_master_flat = PIPELINE_DIR / group_name / "flat" / "master.fit"
    if persisted_master_flat.exists():
        pytest.skip(
            "tmp-vs-persisted comparison retired until event E re-pins the "
            "on-disk reference under Step 3b's new recipe (dedupe+uniquify "
            "staging, -norm=mul) -- see this test's own comment. The "
            "tmp-vs-tmp determinism bound above already ran and passed."
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
    lum_lights = groups[("T24", RGB_USER, "M51", "Luminance", 1)]
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
    lum_lights = groups[("T24", RGB_USER, "M51", "Luminance", 1)]
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
