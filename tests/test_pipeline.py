from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.background_color import UnknownInstrumentError, resolve_instrument_profile
from astro_pipeline.calibration import CalibrationMode, FlatPolicy
from astro_pipeline.ingest import scan_session
from astro_pipeline.pipeline import (
    NARROWBAND_PALETTES,
    ColourContributor,
    _build_colour_contributor,
    _NarrowbandNormalizingReport,
    build_group_master,
    build_single_filter_master,
    contributor_dir,
    contributor_fwhm_arcsec,
    discover_luminance_contributors,
    equalize_narrowband_channels,
    infer_calibration_mode,
    infer_flat_policy,
    normalize_narrowband_filter_name,
    resolve_lights,
    run_lrgb,
    run_narrowband,
    select_luminance_source,
)
from astro_pipeline.siril_driver import find_siril_cli

from conftest import (
    FINAL_DIR,
    LUM_USER,
    NGC3628_PROJECT_DIR,
    PIPELINE_DIR,
    PROJECT_DIR as REAL_SESSION_DIR,
    RGB_USER,
    requires,
    requires_ngc3628_project,
)

requires_real_session = pytest.mark.skipif(
    not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine"
)

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False
requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")


# --- 1.1 multi-telescope Luminance discovery --------------------------------


def test_discover_luminance_contributors_single_telescope_unchanged() -> None:
    """No other telescope has Luminance data for this fake report -> the
    caller's own (telescope, lum_binning) is returned alone, exactly like
    today's single-contributor behaviour."""

    class FakeReport:
        def instrument_groups(self):
            return {
                ("T24", "M51", "Luminance", 1): ["light"],
                ("T24", "M51", "Red", 2): ["light"],
            }

    contributors = discover_luminance_contributors(FakeReport(), "M51", "T24", 1)
    assert contributors == [("T24", 1)]


def test_discover_luminance_contributors_finds_multiple_telescopes() -> None:
    class FakeReport:
        def instrument_groups(self):
            return {
                ("T24", "M51", "Luminance", 1): ["light"],
                ("T21", "M51", "Luminance", 1): ["light"],
                ("T24", "M51", "Red", 2): ["light"],
            }

    contributors = discover_luminance_contributors(FakeReport(), "M51", "T24", 1)
    assert set(contributors) == {("T24", 1), ("T21", 1)}
    # caller's own combo is always present, even though it also happens to
    # be discovered
    assert ("T24", 1) in contributors


def test_discover_luminance_contributors_ignores_other_targets() -> None:
    class FakeReport:
        def instrument_groups(self):
            return {
                ("T24", "M51", "Luminance", 1): ["light"],
                ("T21", "M101", "Luminance", 1): ["light"],  # different target
            }

    contributors = discover_luminance_contributors(FakeReport(), "M51", "T24", 1)
    assert contributors == [("T24", 1)]


def test_discover_luminance_contributors_includes_caller_even_with_no_data() -> None:
    """A telescope with genuinely no Luminance data yet (e.g. a first run
    before any lights exist) must still get its caller-supplied combo back
    -- resolve_lights() then returns an empty list, handled downstream."""

    class FakeReport:
        def instrument_groups(self):
            return {}

    contributors = discover_luminance_contributors(FakeReport(), "M51", "T24", 1)
    assert contributors == [("T24", 1)]


# --- resolve_lights: calibration_mode-aware group lookup (precalibrated-
# path plan, 2026-09) -----------------------------------------------------


class _FakeLight:
    def __init__(self, tag: str) -> None:
        self.user = tag


def test_resolve_lights_default_mode_uses_raw_instrument_groups() -> None:
    raw_light, calibrated_light = _FakeLight("raw"), _FakeLight("calibrated")

    class FakeReport:
        def instrument_groups(self):
            return {("T73", "NGC 3628", "Red", 2): [raw_light]}

        def calibrated_instrument_groups(self):
            return {("T73", "NGC 3628", "Red", 2): [calibrated_light]}

    lights, _ = resolve_lights(FakeReport(), "T73", "NGC 3628", "Red", 2)
    assert lights == [raw_light]


def test_resolve_lights_precalibrated_mode_uses_calibrated_instrument_groups() -> None:
    raw_light, calibrated_light = _FakeLight("raw"), _FakeLight("calibrated")

    class FakeReport:
        def instrument_groups(self):
            return {("T73", "NGC 3628", "Red", 2): [raw_light]}

        def calibrated_instrument_groups(self):
            return {("T73", "NGC 3628", "Red", 2): [calibrated_light]}

    lights, _ = resolve_lights(
        FakeReport(), "T73", "NGC 3628", "Red", 2, calibration_mode=CalibrationMode.PRECALIBRATED
    )
    assert lights == [calibrated_light]


@requires_real_session
def test_discover_luminance_contributors_real_data_finds_t24_and_t21() -> None:
    """The actual real-data claim Slice 1 exists to satisfy: without this,
    T21's Luminance never gets built under any slice (see plan-rev4.md)."""
    report = scan_session(REAL_SESSION_DIR)
    contributors = discover_luminance_contributors(report, "M51", "T24", 1)
    assert ("T24", 1) in contributors
    assert ("T21", 1) in contributors
    assert len(contributors) == 2


# --- 1.2 exposure time derived from the lights, not a caller parameter -----


@requires_real_session
def test_t24_luminance_group_is_single_exptime() -> None:
    """Verified-safe precondition the plan relies on: every existing real
    T24 group is single-exptime, so deriving exptime from the lights
    changes nothing for it."""
    report = scan_session(REAL_SESSION_DIR)
    lights, _ = resolve_lights(report, "T24", "M51", "Luminance", 1)
    assert {f.exptime for f in lights} == {300.0}


@requires_real_session
def test_t21_luminance_group_is_mixed_exptime() -> None:
    """The actual case Slice 1 exists to handle: T21's real Luminance group
    spans two exposure times in one contributor."""
    report = scan_session(REAL_SESSION_DIR)
    lights, _ = resolve_lights(report, "T21", "M51", "Luminance", 1)
    assert {f.exptime for f in lights} == {600.0, 300.0}
    assert len(lights) == 2


# --- Slice 2: L-source selection, surfaced not automated --------------------


def _write_solved_master(path: Path, cdelt_deg: float = 0.0005) -> None:
    """A minimal plate-solved-looking FITS: just enough WCS for
    checkpoints._pixel_scale_arcsec() to return a real pixel scale.
    `cdelt_deg` of 0.0005 deg/px = 1.8"/px, chosen to make hand arithmetic
    easy in the tests that use it."""
    header = fits.Header()
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRVAL1"] = 180.0
    header["CRVAL2"] = 30.0
    header["CRPIX1"] = 5.0
    header["CRPIX2"] = 5.0
    header["CDELT1"] = -cdelt_deg
    header["CDELT2"] = cdelt_deg
    header["PLTSOLVD"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=np.zeros((10, 10), dtype=np.float32), header=header).writeto(path)


def _write_seq(path: Path, fwhm_x_values: list[float]) -> None:
    """A minimal r_pp_lights_.seq with one R0 line per given FWHM value --
    real format verified against the actual files under this project's
    _pipeline/ tree (see contributor_fwhm_arcsec's docstring): `R0 <fwhm_x>
    <fwhm_y> <roundness> <?> <background> <nstars> H ...`. Only the first
    field after R0 (fwhm_x) is read."""
    lines = [
        "#Siril sequence file. Contains list of images, selection, registration data and statistics",
        f"S 'r_pp_lights_' 1 {len(fwhm_x_values)} {len(fwhm_x_values)} 5 1 6 0 0 0",
        "L 1",
        *(f"I {i + 1} 1" for i in range(len(fwhm_x_values))),
        *(
            f"R0 {fwhm:.5f} {fwhm:.5f} 0.9 0 0.1 100 H 1 0 0 0 1 0 0 0 1"
            for fwhm in fwhm_x_values
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_contributor_fwhm_arcsec_parses_median_and_converts(tmp_path: Path) -> None:
    """Median (not mean) of the R0 lines' first field, converted to arcsec
    via the master's own WCS pixel scale -- the exact mechanics
    contributor_fwhm_arcsec's docstring claims, verified here on synthetic
    data with a known answer rather than trusted from the real fixture
    alone."""
    work_dir = tmp_path / "T99-user-M51-Luminance-bin1" / "lights"
    master_path = work_dir / "master_luminance.fit"
    _write_solved_master(master_path, cdelt_deg=0.0005)  # 1.8"/px
    _write_seq(work_dir / "r_pp_lights_.seq", [2.0, 4.0, 6.0])  # median 4.0px

    notes: list[str] = []
    fwhm = contributor_fwhm_arcsec(master_path, notes)
    assert fwhm == pytest.approx(4.0 * 1.8, rel=1e-6)


def test_contributor_fwhm_arcsec_missing_seq_returns_none_not_raises(tmp_path: Path) -> None:
    """A missing .seq file is a real, survivable degraded mode (see the
    function's docstring) -- must degrade Slice 2's selection, not blow up
    the whole run."""
    work_dir = tmp_path / "T99-user-M51-Luminance-bin1" / "lights"
    master_path = work_dir / "master_luminance.fit"
    _write_solved_master(master_path)
    # deliberately no r_pp_lights_.seq written

    notes: list[str] = []
    assert contributor_fwhm_arcsec(master_path, notes) is None
    assert any("cannot measure FWHM" in n for n in notes)


def test_contributor_fwhm_arcsec_no_r0_lines_returns_none(tmp_path: Path) -> None:
    work_dir = tmp_path / "T99-user-M51-Luminance-bin1" / "lights"
    master_path = work_dir / "master_luminance.fit"
    _write_solved_master(master_path)
    _write_seq(work_dir / "r_pp_lights_.seq", [])

    notes: list[str] = []
    assert contributor_fwhm_arcsec(master_path, notes) is None


def test_contributor_fwhm_arcsec_no_wcs_returns_none(tmp_path: Path) -> None:
    """The pp_/r_pp_ frames the .seq file measures carry no WCS -- this
    checks the OTHER side of that: a master with no usable WCS either
    (e.g. plate solving never ran) can't be converted to arcsec, and must
    degrade rather than raise."""
    work_dir = tmp_path / "T99-user-M51-Luminance-bin1" / "lights"
    master_path = work_dir / "master_luminance.fit"
    header = fits.Header()  # no CTYPE/WCS keywords at all
    work_dir.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=np.zeros((10, 10), dtype=np.float32), header=header).writeto(master_path)
    _write_seq(work_dir / "r_pp_lights_.seq", [4.0])

    notes: list[str] = []
    assert contributor_fwhm_arcsec(master_path, notes) is None


@requires_real_session
def test_contributor_fwhm_arcsec_real_data_t24_sharper_than_t21() -> None:
    """The actual claim Slice 2 exists to surface: on the real M51 data,
    T24's Luminance is measurably sharper (lower FWHM in arcsec) than
    T21's, off Siril's own real per-frame registration data -- not
    synthetic, not assumed. The MAGNITUDE of the gap is deliberately not
    asserted tightly here: the plan that specified this slice claimed a
    much larger ~2.4x gap (~4.5" vs ~11-14") from an earlier, different
    measurement; the real numbers this function actually produces off the
    real .seq files are T24 ~3.4" and T21 ~3.9" -- T24 IS sharper, as
    required, but only by roughly 13%, not 2.4x. That discrepancy is
    real and reported (see the Slice 2 handoff notes), not hidden behind a
    loose assertion range chosen to make a stale number pass."""
    t24_master = PIPELINE_DIR / f"T24-{LUM_USER}-M51-Luminance-bin1" / "lights" / "master_luminance.fit"
    t21_master = PIPELINE_DIR / f"T21-{RGB_USER}-M51-Luminance-bin1" / "lights" / "master_luminance.fit"
    if not t24_master.exists() or not t21_master.exists():
        pytest.skip("real Luminance masters not built yet -- run scripts/run_m51.py")

    notes: list[str] = []
    t24_fwhm = contributor_fwhm_arcsec(t24_master, notes)
    t21_fwhm = contributor_fwhm_arcsec(t21_master, notes)
    assert t24_fwhm is not None and t21_fwhm is not None
    assert t24_fwhm < t21_fwhm


def _candidates(
    entries: list[tuple[str, int, float | None]],
) -> list[tuple[tuple[str, int], Path, float | None]]:
    return [((telescope, binning), Path(f"{telescope}-bin{binning}.fit"), fwhm) for telescope, binning, fwhm in entries]


def test_select_luminance_source_sharpest_wins_by_default() -> None:
    candidates = _candidates([("T24", 1, 3.44), ("T21", 1, 3.88)])
    notes: list[str] = []
    selected_key, selected_path, selected_fwhm = select_luminance_source(
        candidates, None, ("T24", 1), notes
    )
    assert selected_key == ("T24", 1)
    assert selected_fwhm == 3.44
    assert any("sharpest" in n for n in notes)
    assert any("NOT selected" in n and "T21" in n for n in notes)


def test_select_luminance_source_explicit_override_wins_even_when_not_sharpest() -> None:
    """The whole point of Slice 2: this is a measured RECOMMENDATION, not
    an automatic rule -- an explicit lum_source must be able to name the
    less-sharp contributor and have it win anyway."""
    candidates = _candidates([("T24", 1, 3.44), ("T21", 1, 3.88)])
    notes: list[str] = []
    selected_key, _selected_path, _selected_fwhm = select_luminance_source(
        candidates, ("T21", 1), ("T24", 1), notes
    )
    assert selected_key == ("T21", 1)
    assert any("EXPLICIT OVERRIDE" in n for n in notes)


def test_select_luminance_source_override_unknown_combo_raises() -> None:
    candidates = _candidates([("T24", 1, 3.44), ("T21", 1, 3.88)])
    with pytest.raises(ValueError):
        select_luminance_source(candidates, ("T99", 9), ("T24", 1), [])


def test_select_luminance_source_falls_back_when_nothing_measured() -> None:
    """Every contributor's FWHM was unmeasurable (e.g. missing .seq files)
    -- must degrade to the caller's own (telescope, lum_binning), the
    pre-Slice-2 behaviour, rather than raising or picking arbitrarily."""
    candidates = _candidates([("T24", 1, None), ("T21", 1, None)])
    notes: list[str] = []
    selected_key, _selected_path, selected_fwhm = select_luminance_source(
        candidates, None, ("T24", 1), notes
    )
    assert selected_key == ("T24", 1)
    assert selected_fwhm is None
    assert any("falling back" in n for n in notes)


# --- Slice 3.1: unknown telescope never silently gets T24's SPCC profile ---


def test_resolve_instrument_profile_known_telescope_returns_profile() -> None:
    profile = resolve_instrument_profile("T24")
    assert profile.mono_sensor == "KAF16803"


def test_resolve_instrument_profile_unknown_telescope_raises() -> None:
    """The actual Slice 3.1 defect: INSTRUMENT_PROFILES.get(telescope,
    T24_PROFILE) used to silently mis-profile ANY unrecognized telescope
    as T24's sensor/filters. Must raise instead of guessing."""
    with pytest.raises(UnknownInstrumentError):
        resolve_instrument_profile("T99")


# --- Slice 2.2: per-telescope FlatPolicy, inferred from data presence, not
# hardcoded by telescope name (mirrors resolve_instrument_profile's own
# preference above, but a distinct mechanism -- inferred from presence in
# flat_index(), not a lookup into a curated registry). ----------------------


class _FakeFlatReport:
    """Just enough of IngestReport's shape for infer_flat_policy -- a
    fixed flat_index() return value, no real ingest/FITS needed."""

    def __init__(self, flat_index: dict) -> None:
        self._flat_index = flat_index

    def flat_index(self):
        return self._flat_index


def test_infer_flat_policy_requires_for_telescope_with_any_flat_entry() -> None:
    """T21's real shape: flats for every filter at BIN1 -- REQUIRE, so a
    light group missing one it actually needs is a real, surfaced gap
    (raises), not a silent skip."""
    report = _FakeFlatReport({("T21", 1, "Luminance"): ["flat"], ("T21", 1, "Red"): ["flat"]})
    assert infer_flat_policy(report, "T21") == FlatPolicy.REQUIRE


def test_infer_flat_policy_skip_if_missing_for_telescope_with_no_flats() -> None:
    """T24's permanent real situation for this delivery: zero flats of any
    kind -- SKIP_IF_MISSING, so existing T24 groups keep calibrating
    without a flat exactly as before this slice."""
    report = _FakeFlatReport({("T21", 1, "Luminance"): ["flat"]})
    assert infer_flat_policy(report, "T24") == FlatPolicy.SKIP_IF_MISSING


def test_infer_flat_policy_empty_flat_index_is_skip_if_missing_for_any_telescope() -> None:
    report = _FakeFlatReport({})
    assert infer_flat_policy(report, "T21") == FlatPolicy.SKIP_IF_MISSING
    assert infer_flat_policy(report, "T24") == FlatPolicy.SKIP_IF_MISSING


@requires_real_session
def test_infer_flat_policy_real_data_t21_requires_t24_skips() -> None:
    """The actual real-data claim Slice 2.2 exists to satisfy: T21 ships
    330 real flats (11 filters x 30 at BIN1) -> REQUIRE; T24 ships zero
    flats of any kind in this delivery, structurally, not temporarily ->
    SKIP_IF_MISSING."""
    report = scan_session(REAL_SESSION_DIR)
    assert infer_flat_policy(report, "T21") == FlatPolicy.REQUIRE
    assert infer_flat_policy(report, "T24") == FlatPolicy.SKIP_IF_MISSING


# --- infer_calibration_mode (precalibrated-path plan, 2026-09) -------------


class _FakeCalReport:
    """Just enough of IngestReport's shape for infer_calibration_mode --
    fixed calibration_index()/calibrated_instrument_groups() return
    values, no real ingest/FITS needed."""

    def __init__(self, cal_index: dict, calibrated_groups: dict) -> None:
        self._cal_index = cal_index
        self._calibrated_groups = calibrated_groups

    def calibration_index(self):
        return self._cal_index

    def calibrated_instrument_groups(self):
        return self._calibrated_groups


def test_infer_calibration_mode_raw_local_when_bias_and_dark_present() -> None:
    """T24/T21's real shape: real Bias+Dark -> RAW_LOCAL, unconditionally,
    regardless of whether calibrated-provenance lights also happen to
    exist (a real delivery bundles both)."""
    report = _FakeCalReport(
        cal_index={("T24", "Bias", 1, 0.0): ["b"], ("T24", "Dark", 1, 300.0): ["d"]},
        calibrated_groups={("T24", "M51", "Luminance", 1): ["light"]},
    )
    assert infer_calibration_mode(report, "T24") == CalibrationMode.RAW_LOCAL


def test_infer_calibration_mode_precalibrated_when_bias_missing_and_calibrated_lights_present() -> None:
    """T73's real shape: zero Bias, zero Dark, real calibrated-provenance
    lights present -> PRECALIBRATED."""
    report = _FakeCalReport(
        cal_index={},
        calibrated_groups={("T73", "NGC 3628", "Luminance", 1): ["light"]},
    )
    assert infer_calibration_mode(report, "T73") == CalibrationMode.PRECALIBRATED


def test_infer_calibration_mode_raw_local_when_neither_local_nor_calibrated_data_exists() -> None:
    """No local bias/dark AND no calibrated-provenance lights either -- an
    incomplete delivery with nothing safe to infer from, not the same as
    T73's real situation. Stays RAW_LOCAL deliberately, so it fails loudly
    (CalibrationFramesMissingError downstream) rather than silently
    resolving to a mode with no real data behind it."""
    report = _FakeCalReport(cal_index={}, calibrated_groups={})
    assert infer_calibration_mode(report, "T99") == CalibrationMode.RAW_LOCAL


def test_infer_calibration_mode_raw_local_when_only_dark_missing() -> None:
    """Bias present but Dark missing, no calibrated-provenance fallback --
    still RAW_LOCAL (fails loudly downstream via select_dark, not a silent
    mode switch) since there's nothing precalibrated to fall back to."""
    report = _FakeCalReport(
        cal_index={("T99", "Bias", 1, 0.0): ["b"]},
        calibrated_groups={},
    )
    assert infer_calibration_mode(report, "T99") == CalibrationMode.RAW_LOCAL


@requires_ngc3628_project
def test_infer_calibration_mode_real_data_t73_precalibrated_t24_raw_local() -> None:
    """The actual real-data claim this plan exists to satisfy: T73 (NGC
    3628, Feb 2025) ships zero recognized Bias/Dark and real calibrated-
    provenance lights -> PRECALIBRATED. T24 (M51, real Bias+Dark, a
    different project folder) -> RAW_LOCAL, unaffected."""
    ngc3628_report = scan_session(NGC3628_PROJECT_DIR)
    assert infer_calibration_mode(ngc3628_report, "T73") == CalibrationMode.PRECALIBRATED

    m51_report = scan_session(REAL_SESSION_DIR)
    assert infer_calibration_mode(m51_report, "T24") == CalibrationMode.RAW_LOCAL


# --- Slice 3.2: a partial R/G/B set logs and skips, does not abort the run -


class _FakeReportNoLights:
    """instrument_groups() with no entries at all -- every filter's
    resolve_lights() call returns an empty list, so _build_colour_
    contributor should stop at the very first filter (Red) without
    invoking build_master (no calibrate/stack/solve -- this test must stay
    fast and not touch Siril)."""

    def instrument_groups(self):
        return {}

    def calibration_index(self):
        return {}


def test_build_colour_contributor_partial_rgb_logs_and_skips(tmp_path: Path) -> None:
    notes: list[str] = []
    result = _build_colour_contributor(
        tmp_path, tmp_path / "contrib", _FakeReportNoLights(), "T24", "M51", 2,
        13.4980, 47.1953, notes,
    )
    assert result is None
    assert any("skip" in n.lower() and "Red" in n for n in notes)
    assert any("Red/Green/Blue" in n for n in notes)


class _FakeLightFrame:
    """Just enough of ingest.LightFrame's shape for resolve_lights() to
    read `.user` off it."""

    def __init__(self, user: str = "observer1") -> None:
        self.user = user


class _FakeReportGreenBlueOnly:
    """Red is present but Green is missing -- the skip must trigger on
    whichever filter is actually absent, not just the first one checked,
    and the log line must name it correctly."""

    def instrument_groups(self):
        return {("T24", "M51", "Red", 2): [_FakeLightFrame()]}

    def calibration_index(self):
        return {}


class _FakeReportOneLightPerFilter:
    """All three RGB filters present, but only 1 light each -- too few for
    Siril to form a sequence (MIN_SEQUENCE_FRAMES=2), a real failure mode
    found on T72's single-frame NGC 3628 Luminance group (first real
    precalibrated-path run). Must be caught and skipped-and-logged BEFORE
    build_group_master()/register_and_stack() would crash on it."""

    def instrument_groups(self):
        return {
            ("T24", "M51", "Red", 2): [_FakeLightFrame()],
            ("T24", "M51", "Green", 2): [_FakeLightFrame()],
            ("T24", "M51", "Blue", 2): [_FakeLightFrame()],
        }

    def calibration_index(self):
        return {}


def test_build_colour_contributor_skips_on_too_few_frames_for_a_sequence(tmp_path: Path) -> None:
    notes: list[str] = []
    result = _build_colour_contributor(
        tmp_path, tmp_path / "contrib", _FakeReportOneLightPerFilter(), "T24", "M51", 2,
        13.4980, 47.1953, notes,
    )
    assert result is None
    assert any("skip" in n.lower() and "at least 2" in n for n in notes)


def test_build_colour_contributor_skips_on_missing_green_not_just_red(tmp_path: Path) -> None:
    """resolve_lights() for Red returns a non-empty list here, so the
    function would proceed to build_group_master() for Red -- which needs real
    calibration frames/Siril and isn't appropriate for this fast unit
    test. So this only checks that the missing-filter detection itself
    (in RGB_FILTERS order) would name Green, not Red, by inspecting
    resolve_lights directly rather than running the full function."""
    from astro_pipeline.pipeline import resolve_lights

    report = _FakeReportGreenBlueOnly()
    red_lights, _ = resolve_lights(report, "T24", "M51", "Red", 2)
    green_lights, _ = resolve_lights(report, "T24", "M51", "Green", 2)
    assert red_lights  # present
    assert not green_lights  # missing -- this is the one that should skip


# --- capability B (OSC + local raw calibration, 2026-09): build_master's -
# --- debayer=True + RAW_LOCAL + no-real-dark path (formerly a real,       -
# --- deliberate NotImplementedError -- see calibration.py's real T68     -
# --- validation for the underlying calibrate_lights()/run_calibration()  -
# --- claim; this test covers build_master's own orchestration logic      -
# --- (dark-selection bypass) with everything below it mocked out, since  -
# --- that part needs no real Siril to verify). ------------------------------


def test_build_master_debayer_with_no_real_dark_skips_select_dark_and_requires_bias_only(
    tmp_path: Path, monkeypatch
) -> None:
    """The real, new logic: for a debayer=True RAW_LOCAL group with NO dark
    at all in cal_index (T68's real shape), select_dark() must NOT be
    called (it would raise CalibrationFramesMissingError for this exact
    case -- correct for a mono telescope, wrong here), and run_calibration
    must receive dark_frames=[] and require_dark=False."""
    import astro_pipeline.master_builder as master_builder_module

    captured = {}

    def fake_run_calibration(light_frames, bias, dark_frames, **kwargs):
        captured["dark_frames"] = dark_frames
        captured["require_dark"] = kwargs.get("require_dark")
        captured["debayer"] = kwargs.get("debayer")

        class _FakeCalResult:
            flat_corrected = False

        return _FakeCalResult()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("select_dark() must not be called when no dark exists for a debayer group")

    monkeypatch.setattr(master_builder_module, "run_calibration", fake_run_calibration)
    monkeypatch.setattr(master_builder_module, "select_dark", fail_if_called)

    stub_master = tmp_path / "stub_master.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.float32)).writeto(stub_master)

    class _FakeStackResult:
        master_path = stub_master

    monkeypatch.setattr(
        master_builder_module, "register_and_stack", lambda *a, **k: _FakeStackResult()
    )
    monkeypatch.setattr(master_builder_module, "solve", lambda *a, **k: None)

    cal_index = {("T68", "Bias", 1, 0.0): [_FakeLightFrame()]}  # no "Dark" key at all

    class _FakeLightFrameWithExptime:
        def __init__(self) -> None:
            self.user = "observer1"
            self.exptime = 240.0

    build_group_master(
        tmp_path, [_FakeLightFrameWithExptime(), _FakeLightFrameWithExptime()], cal_index, "group", "Color",
        "T68", 1, 5.588, -5.391, [],
        flat_frames=[], flat_policy=FlatPolicy.SKIP_IF_MISSING,
        debayer=True, bayer_pattern=0,
    )

    assert captured["dark_frames"] == []
    assert captured["require_dark"] is False
    assert captured["debayer"] is True


def test_build_master_debayer_with_a_real_dark_present_still_uses_it(tmp_path: Path, monkeypatch) -> None:
    """If a dark DOES exist for this (telescope, binning) even with
    debayer=True, it must still be used normally (select_dark() called,
    require_dark stays True) -- a future OSC delivery might have real
    darks; this must not silently ignore data that exists."""
    import astro_pipeline.master_builder as master_builder_module
    from astro_pipeline.calibration import DarkSelection

    captured = {}

    def fake_select_dark(cal_index, telescope, binning, light_exptimes):
        captured["select_dark_called"] = True
        return DarkSelection([_FakeLightFrame()], exptime=240.0, scaled=False)

    def fake_run_calibration(light_frames, bias, dark_frames, **kwargs):
        captured["require_dark"] = kwargs.get("require_dark")

        class _FakeCalResult:
            flat_corrected = False

        return _FakeCalResult()

    monkeypatch.setattr(master_builder_module, "select_dark", fake_select_dark)
    monkeypatch.setattr(master_builder_module, "run_calibration", fake_run_calibration)

    stub_master = tmp_path / "stub_master.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.float32)).writeto(stub_master)

    class _FakeStackResult:
        master_path = stub_master

    monkeypatch.setattr(master_builder_module, "register_and_stack", lambda *a, **k: _FakeStackResult())
    monkeypatch.setattr(master_builder_module, "solve", lambda *a, **k: None)

    cal_index = {
        ("T68", "Bias", 1, 0.0): [_FakeLightFrame()],
        ("T68", "Dark", 1, 240.0): [_FakeLightFrame()],
    }

    class _FakeLightFrameWithExptime:
        def __init__(self) -> None:
            self.user = "observer1"
            self.exptime = 240.0

    build_group_master(
        tmp_path, [_FakeLightFrameWithExptime(), _FakeLightFrameWithExptime()], cal_index, "group", "Color",
        "T68", 1, 5.588, -5.391, [],
        flat_frames=[], flat_policy=FlatPolicy.SKIP_IF_MISSING,
        debayer=True, bayer_pattern=0,
    )

    assert captured["select_dark_called"] is True
    assert captured["require_dark"] is True


# --- capability A (narrowband, 2026-09): _build_colour_contributor's new --
# --- filters/run_colour_calibration parameters ------------------------------


class _FakeReportNarrowband:
    """Real T20/M42 narrowband shape (Ha/OIII/SII), 3 lights each -- enough
    to clear MIN_SEQUENCE_FRAMES and reach the alignment/rgbcomp logic."""

    def instrument_groups(self):
        return {
            ("T20", "M42", "Ha", 2): [_FakeLightFrame(), _FakeLightFrame()],
            ("T20", "M42", "OIII", 2): [_FakeLightFrame(), _FakeLightFrame()],
        }

    def calibration_index(self):
        return {}

    def flat_index(self):
        return {}


def _write_stub_master(path: Path, fill: float) -> Path:
    fits.PrimaryHDU(data=np.full((8, 8), fill, dtype=np.float32)).writeto(path, overwrite=True)
    return path


def test_build_colour_contributor_hoo_duplicates_repeated_filter_and_adds_nosum(
    tmp_path: Path, monkeypatch
) -> None:
    """The real, new logic this capability adds: HOO's (Ha, OIII, OIII)
    triplet must build only 2 real masters (Ha, OIII -- deduplicated),
    but produce 3 on-disk channel files for rgbcomp (oiii.fit duplicated
    to oiii_2.fit rather than colliding), and the rgbcomp command must
    include -nosum (only added because of the real repeat), while never
    running SPCC (run_colour_calibration=False)."""
    import astro_pipeline.pipeline as pipeline_module

    contrib_dir = tmp_path / "contrib"

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *args, **kwargs):
        # One real, tiny, readable FITS master per unique filter.
        return _write_stub_master(tmp_path / f"master_{filter_name}.fit", fill=0.3)

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)

    def fake_reproject(source, reference, out_path):
        _write_stub_master(out_path, fill=0.3)

        class _FakeReconResult:
            footprint_mean = 1.0

        return _FakeReconResult()

    monkeypatch.setattr(pipeline_module, "reproject_to_reference", fake_reproject)
    monkeypatch.setattr(pipeline_module, "crop_to_common_coverage", lambda paths, out_dir: None)

    captured_rgbcomp_command = {}

    def fake_run_script(commands, workdir=None, **kwargs):
        captured_rgbcomp_command["command"] = commands[0]
        _write_stub_master(Path(workdir) / "rgb_native.fit", fill=0.3)

        class _FakeResult:
            log_lines: list[str] = []

        return _FakeResult()

    monkeypatch.setattr(pipeline_module, "run_script", fake_run_script)
    monkeypatch.setattr(
        pipeline_module, "run_graxpert_background_extraction",
        lambda fits_path, output_stem: _write_stub_master(Path(fits_path).parent / f"{output_stem}.fits", fill=0.3),
    )

    result = _build_colour_contributor(
        tmp_path, contrib_dir, _FakeReportNarrowband(), "T20", "M42", 2,
        5.588, -5.391, [],
        filters=("Ha", "OIII", "OIII"),
        run_colour_calibration=False,
    )

    assert result is not None
    # The repeated filter (OIII) must be duplicated under a distinct name,
    # not silently collided on disk.
    assert (contrib_dir / "ha.fit").exists()
    assert (contrib_dir / "oiii.fit").exists()
    assert (contrib_dir / "oiii_2.fit").exists()
    # -nosum must be present -- only because of the real repeated filter.
    assert "-nosum" in captured_rgbcomp_command["command"]
    assert captured_rgbcomp_command["command"] == "rgbcomp ha oiii oiii_2 -out=rgb_native -nosum"
    # SPCC skipped entirely -- the final composite is the background-
    # extracted output copied straight through, no colour_calibrated
    # inprogress staging file left behind.
    assert not (contrib_dir / "rgb_colour_calibrated__inprogress.fit").exists()
    assert result.composite_path == contrib_dir / "rgb_colour_calibrated.fit"
    assert result.composite_path.exists()


def test_build_colour_contributor_rgb_default_produces_no_nosum(tmp_path: Path, monkeypatch) -> None:
    """Regression guard: the default (no repeated filter) RGB path must
    NEVER get -nosum appended -- verifies has_repeated_filter is actually
    False for the unchanged default case, not just narrowband."""
    import astro_pipeline.pipeline as pipeline_module

    contrib_dir = tmp_path / "contrib_rgb"

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *args, **kwargs):
        return _write_stub_master(tmp_path / f"master_{filter_name}.fit", fill=0.3)

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)

    def fake_reproject(source, reference, out_path):
        _write_stub_master(out_path, fill=0.3)

        class _FakeReconResult:
            footprint_mean = 1.0

        return _FakeReconResult()

    monkeypatch.setattr(pipeline_module, "reproject_to_reference", fake_reproject)
    monkeypatch.setattr(pipeline_module, "crop_to_common_coverage", lambda paths, out_dir: None)

    captured = {}

    def fake_run_script(commands, workdir=None, **kwargs):
        captured["command"] = commands[0]
        _write_stub_master(Path(workdir) / "rgb_native.fit", fill=0.3)

        class _FakeResult:
            log_lines: list[str] = []

        return _FakeResult()

    monkeypatch.setattr(pipeline_module, "run_script", fake_run_script)
    monkeypatch.setattr(
        pipeline_module, "run_graxpert_background_extraction",
        lambda fits_path, output_stem: _write_stub_master(Path(fits_path).parent / f"{output_stem}.fits", fill=0.3),
    )

    class _FakeReportRGB:
        def instrument_groups(self):
            return {
                ("T24", "M51", "Red", 2): [_FakeLightFrame(), _FakeLightFrame()],
                ("T24", "M51", "Green", 2): [_FakeLightFrame(), _FakeLightFrame()],
                ("T24", "M51", "Blue", 2): [_FakeLightFrame(), _FakeLightFrame()],
            }

        def calibration_index(self):
            return {}

        def flat_index(self):
            return {}

    result = _build_colour_contributor(
        tmp_path, contrib_dir, _FakeReportRGB(), "T24", "M51", 2,
        13.4980, 47.1953, [],
        run_colour_calibration=False,
    )

    assert result is not None
    assert captured["command"] == "rgbcomp red green blue -out=rgb_native"
    assert "-nosum" not in captured["command"]


# --- capability A (narrowband, 2026-09): filter-name normalization --------


def test_narrowband_palettes_are_the_real_verified_mappings() -> None:
    assert NARROWBAND_PALETTES["sho"] == ("SII", "Ha", "OIII")
    assert NARROWBAND_PALETTES["hoo"] == ("Ha", "OIII", "OIII")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Ha", "Ha"), ("ha", "Ha"), ("H-Alpha", "Ha"), ("Halpha", "Ha"), ("HYDROGEN-ALPHA", "Ha"),
        ("OIII", "OIII"), ("oiii", "OIII"), ("O3", "OIII"), ("O-III", "OIII"), ("Oxygen-III", "OIII"),
        ("SII", "SII"), ("sii", "SII"), ("S2", "SII"), ("S-II", "SII"), ("Sulfur-II", "SII"),
    ],
)
def test_normalize_narrowband_filter_name_handles_real_world_spelling_variants(raw: str, expected: str) -> None:
    assert normalize_narrowband_filter_name(raw) == expected


def test_normalize_narrowband_filter_name_leaves_non_narrowband_names_unchanged() -> None:
    """This is deliberately NOT a general-purpose filter normalizer --
    Red/Green/Blue/Luminance must pass through untouched."""
    for name in ("Red", "Green", "Blue", "Luminance", "Color"):
        assert normalize_narrowband_filter_name(name) == name


# --- capability A (narrowband, 2026-09): _NarrowbandNormalizingReport -----


class _FakeRawReport:
    """A real-world-shaped report using NON-canonical narrowband spelling
    (H-Alpha instead of Ha) -- exactly the case
    _NarrowbandNormalizingReport exists to fix."""

    def instrument_groups(self):
        return {
            ("T20", "M42", "H-Alpha", 2): ["ha_light_1"],
            ("T20", "M42", "O-III", 2): ["oiii_light_1"],
            ("T20", "M42", "Red", 2): ["red_light_1"],
        }

    def calibrated_instrument_groups(self):
        return {("T20", "M42", "S-II", 2): ["sii_calibrated_light_1"]}

    def flat_index(self):
        return {("T20", 2, "H-Alpha"): ["ha_flat_1"]}

    def calibration_index(self):
        return {"sentinel": "delegated-through-unchanged"}


def test_narrowband_normalizing_report_normalizes_instrument_groups() -> None:
    report = _NarrowbandNormalizingReport(_FakeRawReport())
    groups = report.instrument_groups()
    assert groups[("T20", "M42", "Ha", 2)] == ["ha_light_1"]
    assert groups[("T20", "M42", "OIII", 2)] == ["oiii_light_1"]
    # Non-narrowband filters pass through unchanged.
    assert groups[("T20", "M42", "Red", 2)] == ["red_light_1"]


def test_narrowband_normalizing_report_normalizes_calibrated_instrument_groups() -> None:
    report = _NarrowbandNormalizingReport(_FakeRawReport())
    groups = report.calibrated_instrument_groups()
    assert groups[("T20", "M42", "SII", 2)] == ["sii_calibrated_light_1"]


def test_narrowband_normalizing_report_normalizes_flat_index() -> None:
    report = _NarrowbandNormalizingReport(_FakeRawReport())
    flats = report.flat_index()
    assert flats[("T20", 2, "Ha")] == ["ha_flat_1"]


def test_narrowband_normalizing_report_delegates_unknown_methods() -> None:
    """calibration_index (and every other real IngestReport method) is
    delegated through __getattr__ unchanged -- this proxy only touches
    the three methods that carry a filter name."""
    report = _NarrowbandNormalizingReport(_FakeRawReport())
    assert report.calibration_index() == {"sentinel": "delegated-through-unchanged"}


def test_narrowband_normalizing_report_merges_on_real_collision() -> None:
    """Two real spellings for the same filter (Ha and H-Alpha) at the
    same (telescope, target, binning) must merge their light lists, not
    silently drop one."""

    class _FakeCollisionReport:
        def instrument_groups(self):
            return {
                ("T20", "M42", "Ha", 2): ["light_a"],
                ("T20", "M42", "H-Alpha", 2): ["light_b"],
            }

    report = _NarrowbandNormalizingReport(_FakeCollisionReport())
    groups = report.instrument_groups()
    assert set(groups[("T20", "M42", "Ha", 2)]) == {"light_a", "light_b"}


# --- capability A (narrowband, 2026-09): equalize_narrowband_channels -----


def test_equalize_narrowband_channels_normalizes_each_channel_independently(tmp_path: Path) -> None:
    """Real, load-bearing claim: each channel's own median->0, own
    high-percentile->~1, independent of the other channels' scale -- the
    actual fix for SII being intrinsically much fainter than Ha/OIII."""
    data = np.zeros((3, 10, 10), dtype=np.float32)
    data[0] = 0.01  # SII-like: faint background
    data[0, 5, 5] = 0.02  # a single bright SII pixel
    data[1] = 0.5  # Ha-like: bright background
    data[1, 5, 5] = 1.0
    data[2] = 0.1
    data[2, 5, 5] = 0.3
    src = tmp_path / "narrowband_composite.fit"
    fits.writeto(src, data)

    out_path = tmp_path / "equalized.fit"
    stats = equalize_narrowband_channels(src, out_path, high_percentile=99.0)

    result = fits.getdata(out_path, memmap=False)
    # Each channel's background (the dominant value) should now sit near
    # 0, regardless of that channel's original absolute brightness.
    assert abs(float(np.median(result[0])) - 0.0) < 0.01
    assert abs(float(np.median(result[1])) - 0.0) < 0.01
    assert abs(float(np.median(result[2])) - 0.0) < 0.01
    assert "ch0_median" in stats and "ch1_median" in stats and "ch2_median" in stats


def test_equalize_narrowband_channels_preserves_nan(tmp_path: Path) -> None:
    data = np.full((3, 8, 8), 0.3, dtype=np.float32)
    data[0, 0, 0] = np.nan
    src = tmp_path / "with_nan.fit"
    fits.writeto(src, data)

    out_path = tmp_path / "equalized_nan.fit"
    equalize_narrowband_channels(src, out_path)

    result = fits.getdata(out_path, memmap=False)
    assert np.isnan(result[0, 0, 0])


def test_equalize_narrowband_channels_handles_degenerate_all_zero_channel(tmp_path: Path) -> None:
    """A channel with zero real signal (median == high percentile) must
    not raise a divide-by-zero -- it should come back near-zero, not
    inventing signal that isn't there."""
    data = np.zeros((3, 8, 8), dtype=np.float32)
    data[1] = 0.5  # only one real channel
    src = tmp_path / "degenerate.fit"
    fits.writeto(src, data)

    out_path = tmp_path / "equalized_degenerate.fit"
    equalize_narrowband_channels(src, out_path)  # must not raise

    result = fits.getdata(out_path, memmap=False)
    assert np.all(result[0] == 0.0)


def test_equalize_narrowband_channels_rejects_non_3channel_input(tmp_path: Path) -> None:
    src = tmp_path / "mono.fit"
    fits.writeto(src, np.ones((8, 8), dtype=np.float32))
    with pytest.raises(ValueError, match="3-channel"):
        equalize_narrowband_channels(src, tmp_path / "out.fit")


# --- capability A (narrowband, 2026-09): run_narrowband orchestration -----


def test_run_narrowband_rejects_unknown_palette(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="palette"):
        run_narrowband(tmp_path, "T20", "M42", 5.588, -5.391, palette="not-a-real-palette")


def test_run_narrowband_raises_with_real_filter_diagnostic_when_no_match(tmp_path: Path, monkeypatch) -> None:
    """The real fix this orchestration adds: when the requested palette's
    filters aren't found, the error names the REAL distinct FILTER
    strings actually seen at this telescope/binning, not just a bare
    'no lights found'."""
    import astro_pipeline.pipeline as pipeline_module

    class _FakeReportWrongSpelling:
        def instrument_groups(self):
            # Real narrowband data present, but under a spelling this
            # test deliberately does NOT put in NARROWBAND_FILTER_ALIASES,
            # so normalization can't rescue it -- exercising the genuine
            # "nothing matched" diagnostic path.
            return {("T20", "M42", "SomeWeirdSpelling", 2): ["light1", "light2"]}

        def calibration_index(self):
            return {}

        def flat_index(self):
            return {}

        def calibrated_instrument_groups(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReportWrongSpelling())

    with pytest.raises(RuntimeError, match="SomeWeirdSpelling"):
        run_narrowband(tmp_path, "T20", "M42", 5.588, -5.391, palette="sho")


def test_run_narrowband_exports_with_palette_named_stem(tmp_path: Path, monkeypatch) -> None:
    """End-to-end mocked run: confirms the real call sequence (contributor
    build -> equalization -> stretch -> export) reaches export() with the
    palette-named stem, not '_lrgb'/'_rgb'."""
    import astro_pipeline.pipeline as pipeline_module
    from astro_pipeline.workspace import pipeline_dir

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class _FakeLightFrame:
        def __init__(self) -> None:
            self.user = "observer1"
            self.exptime = 300.0

    class _FakeReportSHO:
        def instrument_groups(self):
            return {
                ("T20", "M42", f, 2): [_FakeLightFrame(), _FakeLightFrame()]
                for f in ("SII", "Ha", "OIII")
            }

        def calibration_index(self):
            return {}

        def flat_index(self):
            return {}

        def calibrated_instrument_groups(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda pd: _FakeReportSHO())

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *args, **kwargs):
        p = tmp_path / f"master_{filter_name}.fit"
        fits.PrimaryHDU(data=np.full((16, 16), 0.3, dtype=np.float32)).writeto(p, overwrite=True)
        return p

    def fake_reproject(source, reference, out_path):
        fits.PrimaryHDU(data=np.full((16, 16), 0.3, dtype=np.float32)).writeto(out_path, overwrite=True)

        class _R:
            footprint_mean = 1.0

        return _R()

    captured = {}

    def fake_run_script(commands, workdir=None, **kwargs):
        captured["rgbcomp_command"] = commands[0]
        fits.PrimaryHDU(data=np.full((3, 16, 16), 0.3, dtype=np.float32)).writeto(
            Path(workdir) / "rgb_native.fit", overwrite=True
        )

        class _R:
            log_lines: list = []

        return _R()

    def fake_bg_extraction(fits_path, output_stem):
        out = Path(fits_path).parent / f"{output_stem}.fits"
        fits.PrimaryHDU(data=np.full((3, 16, 16), 0.3, dtype=np.float32)).writeto(out, overwrite=True)
        return out

    def fake_stretch_rgb(rgb_path, work_dir, output_stem, method):
        out = Path(work_dir) / f"{output_stem}.fit"
        fits.PrimaryHDU(data=np.full((3, 16, 16), 0.5, dtype=np.float32)).writeto(out, overwrite=True)

        class _R:
            composite_path = out

        return _R()

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)
    monkeypatch.setattr(pipeline_module, "reproject_to_reference", fake_reproject)
    monkeypatch.setattr(pipeline_module, "crop_to_common_coverage", lambda paths, out_dir: None)
    monkeypatch.setattr(pipeline_module, "run_script", fake_run_script)
    monkeypatch.setattr(pipeline_module, "run_graxpert_background_extraction", fake_bg_extraction)
    monkeypatch.setattr(pipeline_module, "stretch_rgb", fake_stretch_rgb)

    result = run_narrowband(project_dir, "T20", "M42", 5.588, -5.391, palette="sho", binning=2)

    assert result.export_result is not None
    assert result.export_result.tiff_path.name == "M42_sho.tif"
    assert "-nosum" not in captured["rgbcomp_command"]  # sho has no repeated filter


# --- narrowband-boost plan (2026-09): build_single_filter_master ----------


def test_build_single_filter_master_skips_when_no_lights(tmp_path: Path) -> None:
    class _FakeReportEmpty:
        def instrument_groups(self):
            return {}

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {}

        def flat_index(self):
            return {}

    notes: list[str] = []
    result = build_single_filter_master(
        tmp_path, _FakeReportEmpty(), "T20", "M42", "Ha", 2, 5.588, -5.391, notes,
    )
    assert result is None
    assert any("no Ha lights found" in n for n in notes)


def test_build_single_filter_master_skips_on_too_few_frames(tmp_path: Path) -> None:
    class _FakeLightFrame:
        user = "observer1"
        exptime = 300.0

    class _FakeReportOneLight:
        def instrument_groups(self):
            return {("T20", "M42", "Ha", 2): [_FakeLightFrame()]}

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {}

        def flat_index(self):
            return {}

    notes: list[str] = []
    result = build_single_filter_master(
        tmp_path, _FakeReportOneLight(), "T20", "M42", "Ha", 2, 5.588, -5.391, notes,
    )
    assert result is None
    assert any("at least" in n for n in notes)


def test_build_single_filter_master_precalibrated_calls_build_master_with_placeholders(
    tmp_path: Path, monkeypatch
) -> None:
    import astro_pipeline.master_builder as master_builder_module

    class _FakeLightFrame:
        user = "observer1"
        exptime = 300.0

    class _FakeReportPrecalibrated:
        def calibrated_instrument_groups(self):
            return {("T20", "M42", "Ha", 2): [_FakeLightFrame(), _FakeLightFrame()]}

        def calibration_index(self):
            return {"sentinel": "should not be read on the PRECALIBRATED path"}

        def flat_index(self):
            raise AssertionError("flat_index() must not be consulted on the PRECALIBRATED path")

    captured = {}

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *args, **kwargs):
        captured["cal_index"] = cal_index
        captured["flat_frames"] = kwargs.get("flat_frames")
        captured["calibration_mode"] = kwargs.get("calibration_mode")
        return tmp_path / "master_ha.fit"

    monkeypatch.setattr(master_builder_module, "build_group_master", fake_build_master)

    notes: list[str] = []
    result = build_single_filter_master(
        tmp_path, _FakeReportPrecalibrated(), "T20", "M42", "Ha", 2, 5.588, -5.391, notes,
        calibration_mode=CalibrationMode.PRECALIBRATED,
    )

    assert result == tmp_path / "master_ha.fit"
    assert captured["cal_index"] == {}
    assert captured["flat_frames"] == []
    assert captured["calibration_mode"] == CalibrationMode.PRECALIBRATED


# --- Slice 3.3: contributor naming is telescope-explicit and keyed, -------
# --- not positional ---------------------------------------------------------


def test_contributor_dir_primary_keeps_legacy_top_level_path(tmp_path: Path) -> None:
    """The PRIMARY contributor (binning == rgb_binning) must keep the
    legacy top-level `final` directory -- renaming it would trigger the
    hours-of-recompute cost usable()'s resume gating is meant to avoid."""
    final = tmp_path / "final"
    assert contributor_dir(final, "T24", 2, rgb_binning=2) == final


def test_contributor_dir_secondary_is_telescope_explicit(tmp_path: Path) -> None:
    final = tmp_path / "final"
    assert contributor_dir(final, "T24", 1, rgb_binning=2) == final / "contrib_T24_bin1"


def test_contributor_dir_does_not_collide_across_telescopes(tmp_path: Path) -> None:
    """The actual defect Slice 3.3 fixes: the old `contrib_bin{n}` naming
    was telescope-blind, so a second telescope shooting the same
    non-primary binning would collide with an existing directory. The new
    naming must not."""
    final = tmp_path / "final"
    t24_path = contributor_dir(final, "T24", 1, rgb_binning=2)
    t21_path = contributor_dir(final, "T21", 1, rgb_binning=2)
    assert t24_path != t21_path


def test_contributor_dir_is_pure_not_positional(tmp_path: Path) -> None:
    """A pure function of (telescope, binning, rgb_binning) -- calling it
    for the same combo must give the same path regardless of what order a
    caller's discovery loop happens to visit binnings in (the old
    `rgb_reconciled_contrib{i}.fit` positional naming's actual failure
    mode: sorted(rgb_binnings) put BIN1 at loop index 0 even though BIN2
    was the primary contributor)."""
    final = tmp_path / "final"
    # Simulate two different discovery orders finding the same binning.
    order_a = [contributor_dir(final, "T24", b, rgb_binning=2) for b in [1, 2]]
    order_b = [contributor_dir(final, "T24", b, rgb_binning=2) for b in [2, 1]]
    assert order_a[0] == order_b[1]  # BIN1's path is identical either way
    assert order_a[1] == order_b[0]  # BIN2's path is identical either way


def test_colour_contributor_key_identifies_by_telescope_and_binning() -> None:
    contributor = ColourContributor(
        telescope="T24", binning=1, composite_path=Path("x.fit"), sub_count=38, stack_total=27,
    )
    assert contributor.key == "T24_bin1"


# --- Slice 4.3: stop_after + force validation -------------------------------


def test_run_lrgb_rejects_unknown_stop_after(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stop_after"):
        run_lrgb(tmp_path, "T24", "M51", 13.0, 47.0, stop_after="not_a_real_stage")


def test_run_lrgb_rejects_unknown_force_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="force"):
        run_lrgb(tmp_path, "T24", "M51", 13.0, 47.0, force={"not_a_real_stage"})


# --- Slice 4.3: stop_after against real M51 data ----------------------------
# These calls hit zero Siril/GraXpert/SPCC work (everything is already
# usable() on this fixture, built by Slices 1-3) -- the only real cost is
# checkpoint statistics/star-detection, which run regardless of stop_after
# (see pipeline.run_lrgb's inline checkpoint emission, Slice 4.2). They are
# the actual claim Slice 4.3 exists to satisfy: a staged run resumes rather
# than re-doing earlier work, and stopping early leaves no partial/broken
# state.

M51_RA_HOURS = 13.4980
M51_DEC_DEG = 47.1953


def _run_m51(**kwargs):
    return run_lrgb(REAL_SESSION_DIR, telescope="T24", target="M51", ra_hours=M51_RA_HOURS, dec_deg=M51_DEC_DEG, **kwargs)


@requires(FINAL_DIR / "lrgb_final.fit")
def test_run_lrgb_stop_after_masters_stops_before_reconciliation() -> None:
    """The real Slice 4.3 claim: stop_after='masters' returns honestly,
    with `result.masters` populated and NEITHER reconciliation nor
    stretch/export attempted -- no exception, no partial/broken files."""
    reconciled_before = (FINAL_DIR / "rgb_reconciled.fit").stat().st_mtime
    final_before = (FINAL_DIR / "lrgb_final.fit").stat().st_mtime

    result = _run_m51(stop_after="masters")

    assert "Luminance" in result.masters
    assert result.composite_path is None
    assert result.export_result is None
    # Untouched: stopping at "masters" must not regenerate anything past it.
    assert (FINAL_DIR / "rgb_reconciled.fit").stat().st_mtime == reconciled_before
    assert (FINAL_DIR / "lrgb_final.fit").stat().st_mtime == final_before


@requires(FINAL_DIR / "lrgb_final.fit")
def test_run_lrgb_stop_after_reconciled_resumes_without_rebuilding_masters() -> None:
    """A second call with stop_after='reconciled' after a first call with
    stop_after='masters' must actually RESUME -- not redo the masters
    stage -- verified via mtimes on real per-contributor build products
    that only the (expensive, Siril-driven) masters stage would touch."""
    contributor_product = FINAL_DIR / "rgb_colour_calibrated.fit"
    lum_bg_before_first_call = None
    if (FINAL_DIR / "lum_bg.fits").exists():
        lum_bg_before_first_call = (FINAL_DIR / "lum_bg.fits").stat().st_mtime

    _run_m51(stop_after="masters")
    contributor_mtime_after_masters = contributor_product.stat().st_mtime

    result = _run_m51(stop_after="reconciled")

    assert result.composite_path is None
    assert result.export_result is None
    # The masters-stage build product must be untouched by the second call.
    assert contributor_product.stat().st_mtime == contributor_mtime_after_masters
    # L background extraction and reconciliation must actually have run
    # (or been resumed/skipped) by this point -- both files exist.
    assert (FINAL_DIR / "lum_bg.fits").exists()
    assert (FINAL_DIR / "rgb_reconciled.fit").exists()
    if lum_bg_before_first_call is not None:
        # Already existed and unaffected by anything upstream -- resumed,
        # not regenerated.
        assert (FINAL_DIR / "lum_bg.fits").stat().st_mtime == lum_bg_before_first_call


@requires(FINAL_DIR / "lrgb_final.fit")
def test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output() -> None:
    """Slice 4 must not change what's rendered -- only Slice 3 was allowed
    to do that. A full (stop_after=None) run after the staged calls above
    must reproduce the exact same lrgb_final.fit/TIFF bytes, not a new
    render."""
    import hashlib

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    lrgb_final = FINAL_DIR / "lrgb_final.fit"
    tiff = FINAL_DIR / "M51_lrgb.tif"
    before_final, before_tiff = sha(lrgb_final), sha(tiff)

    result = _run_m51()

    assert result.composite_path is not None
    assert sha(lrgb_final) == before_final
    assert sha(tiff) == before_tiff


@requires(FINAL_DIR / "lrgb_final.fit")
def test_run_lrgb_force_final_only_touches_final_not_reconciled_or_masters() -> None:
    """force={"final"} must invalidate exactly lrgb_final.fit -- NOT
    rgb_reconciled.fit or lum_bg.fits, which sit at earlier stages in the
    masters -> reconciled -> final dependency order (run_signature.py's
    cascade_from) and must be left alone by a "final"-only force."""
    reconciled_before = (FINAL_DIR / "rgb_reconciled.fit").stat().st_mtime
    lum_bg_before = (FINAL_DIR / "lum_bg.fits").stat().st_mtime
    final_before = (FINAL_DIR / "lrgb_final.fit").stat().st_mtime

    result = _run_m51(force={"final"})

    assert result.composite_path is not None
    assert (FINAL_DIR / "lrgb_final.fit").stat().st_mtime != final_before
    assert (FINAL_DIR / "rgb_reconciled.fit").stat().st_mtime == reconciled_before
    assert (FINAL_DIR / "lum_bg.fits").stat().st_mtime == lum_bg_before


# --- RGB-only mode (2026-09): a target with zero Luminance data on any -----
# --- telescope (real case: Abell 6 and HFG1, T02, one-shot-colour) --------


@requires_siril
def test_run_lrgb_rgb_only_full_run_no_luminance_no_crash(tmp_path: Path, monkeypatch) -> None:
    """Real integration-style test, exercising the exact code path that had
    THREE real crash bugs found by adversarial review before this test was
    written (see plan-rgb-only-mode.md ??6b): a bare TypeError in
    RunSignature construction (`selected_key[0]` on None), a NameError on
    `lum_for_compose_path` (referenced outside both of its guarding
    branches), and the original IndexError in select_luminance_source
    itself. A fake report with zero Luminance data and one mono RGB
    contributor (monkeypatched _build_colour_contributor -- avoids real
    Siril/GraXpert/SPCC for the colour BUILD step, but stretch_rgb() at
    the end still runs a REAL Siril autostretch call on a tiny synthetic
    composite, so this is gated on requires_siril, not a pure unit test)
    drives a full run_lrgb() call end to end and asserts the RGB-only
    control flow (skip checkpoints 01/03, single-contributor rgb_reconciled
    = direct copy, checkpoint 05_rgb_final not 05_lrgb_final, export stem
    "<target>_rgb" not "<target>_lrgb") all actually happened.
    """
    import astro_pipeline.pipeline as pipeline_module

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            # No Luminance filter group at all for this target -- the real
            # Abell 6/T02 shape.
            return {
                ("T02", "Abell 6 and HFG1", "Red", 1): [_FakeLightFrame("r.fit")],
                ("T02", "Abell 6 and HFG1", "Green", 1): [_FakeLightFrame("g.fit")],
                ("T02", "Abell 6 and HFG1", "Blue", 1): [_FakeLightFrame("b.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            # Real Bias+Dark present -> infer_calibration_mode resolves
            # RAW_LOCAL for T02 in this fake -- irrelevant to what this test
            # actually exercises (the RGB-only control flow), since
            # _build_colour_contributor is monkeypatched wholesale below and
            # never reads this.
            return {("T02", "Bias", 1, 0.0): ["b"], ("T02", "Dark", 1, 300.0): ["d"]}

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    # A real, tiny, readable 3-channel FITS -- what a real ColourContributor's
    # composite_path would point to (rgb_colour_calibrated.fit shape).
    stub_composite = tmp_path / "stub_rgb_colour_calibrated.fit"
    data = np.random.default_rng(0).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
    fits.PrimaryHDU(data=data).writeto(stub_composite)

    def fake_build_colour_contributor(project_dir, contrib_dir, report, telescope, target, binning, *a, **k):
        # Real _build_colour_contributor writes rgb_colour_calibrated.fit
        # into contrib_dir before returning -- checkpoint 02's emission is
        # gated on that file existing on disk (primary_calibrated.exists()),
        # so the mock must reproduce that side effect, not just the return
        # value.
        contrib_dir.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.copy2(stub_composite, contrib_dir / "rgb_colour_calibrated.fit")
        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=stub_composite,
            sub_count=7, stack_total=6,
        )

    monkeypatch.setattr(pipeline_module, "_build_colour_contributor", fake_build_colour_contributor)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = run_lrgb(
        project_dir, telescope="T02", target="Abell 6 and HFG1", ra_hours=3.02, dec_deg=64.70,
        lum_binning=1, rgb_binning=1,
    )

    assert "Luminance" not in result.masters
    checkpoint_labels = [cp.label for cp in result.checkpoints]
    assert "01_master_luminance" not in checkpoint_labels
    assert "03_lum_background_extracted" not in checkpoint_labels
    assert "02_primary_rgb_colour_calibrated" in checkpoint_labels
    assert "04_rgb_reconciled" in checkpoint_labels
    assert "05_rgb_final" in checkpoint_labels
    assert "05_lrgb_final" not in checkpoint_labels

    assert result.composite_path is not None
    assert result.composite_path.name == "rgb_final.fit"
    assert result.export_result is not None
    assert result.export_result.tiff_path.name == "Abell 6 and HFG1_rgb.tif"

    signature_path = project_dir / "_pipeline" / "run_signature.json"
    assert signature_path.exists()
    import json
    persisted = json.loads(signature_path.read_text(encoding="utf-8"))
    assert persisted["luminance_selected"] == ""
    assert persisted["luminance"] == {}


def test_run_lrgb_rgb_only_multi_contributor_raises_not_implemented(tmp_path: Path, monkeypatch) -> None:
    """The deferred multi-contributor RGB-only case (plan-rgb-only-mode.md
    ??7 non-goal) must fail loudly, not silently attempt an unverified
    combine. Pure unit test -- no real Siril needed, since the
    NotImplementedError fires before any reprojection/stretch call."""
    import astro_pipeline.pipeline as pipeline_module

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            return {
                ("T02", "Fake Target", "Red", 1): [_FakeLightFrame("r1.fit")],
                ("T02", "Fake Target", "Green", 1): [_FakeLightFrame("g1.fit")],
                ("T02", "Fake Target", "Blue", 1): [_FakeLightFrame("b1.fit")],
                ("T02", "Fake Target", "Red", 2): [_FakeLightFrame("r2.fit")],
                ("T02", "Fake Target", "Green", 2): [_FakeLightFrame("g2.fit")],
                ("T02", "Fake Target", "Blue", 2): [_FakeLightFrame("b2.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {("T02", "Bias", 1, 0.0): ["b"], ("T02", "Dark", 1, 300.0): ["d"],
                     ("T02", "Bias", 2, 0.0): ["b"], ("T02", "Dark", 2, 300.0): ["d"]}

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    stub_composite = tmp_path / "stub.fit"
    data = np.zeros((3, 4, 4), dtype=np.float32)
    fits.PrimaryHDU(data=data).writeto(stub_composite)

    def fake_build_colour_contributor(project_dir, contrib_dir, report, telescope, target, binning, *a, **k):
        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=stub_composite,
            sub_count=1, stack_total=1,
        )

    monkeypatch.setattr(pipeline_module, "_build_colour_contributor", fake_build_colour_contributor)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    with pytest.raises(NotImplementedError):
        run_lrgb(
            project_dir, telescope="T02", target="Fake Target", ra_hours=1.0, dec_deg=1.0,
            lum_binning=1, rgb_binning=1,
        )


def test_run_lrgb_mixed_osc_and_rgb_same_binning_raises_not_implemented(tmp_path: Path, monkeypatch) -> None:
    """The real mixed-shape guard trigger: a telescope with genuine R/G/B
    AND genuine Color data at the SAME binning must raise, not silently
    combine them (unverified channel-order parity between debayer output
    and rgbcomp -- see plan-rgb-only-mode.md ??7). Distinguishes the real
    trigger from the false-positive case (a target with NEITHER real R/G/B
    NOR real Color data at the caller's own rgb_binning, which must NOT
    raise -- both discovery lists unconditionally include the caller's own
    combo regardless of real data, so a naive intersection of the padded
    lists would misfire on every real OSC-only target; caught during real
    testing, not by adversarial review, and covered separately by
    test_run_lrgb_rgb_only_full_run_no_luminance_no_crash actually passing)."""
    import astro_pipeline.pipeline as pipeline_module

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            return {
                ("T02", "Mixed Target", "Red", 1): [_FakeLightFrame("r.fit")],
                ("T02", "Mixed Target", "Green", 1): [_FakeLightFrame("g.fit")],
                ("T02", "Mixed Target", "Blue", 1): [_FakeLightFrame("b.fit")],
                ("T02", "Mixed Target", "Color", 1): [_FakeLightFrame("c1.fit"), _FakeLightFrame("c2.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {("T02", "Bias", 1, 0.0): ["b"], ("T02", "Dark", 1, 300.0): ["d"]}

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    with pytest.raises(NotImplementedError, match="both full R/G/B and one-shot-colour"):
        run_lrgb(
            project_dir, telescope="T02", target="Mixed Target", ra_hours=1.0, dec_deg=1.0,
            lum_binning=1, rgb_binning=1,
        )


# --- Task 5, Step 0 (scratch/task5-oop-refactor-plan.md): FAST, MOCKED ------
# --- coverage of run_lrgb's own staged-orchestration control flow, which ---
# --- previously only had coverage gated on a personal, non-committable   ---
# --- M51 project folder (see the plan's Section 0/4b). No real Siril/    ---
# --- GraXpert is used by any test below -- every Siril/GraXpert-touching ---
# --- internal is monkeypatched directly, the same technique already      ---
# --- proven by test_run_lrgb_rgb_only_full_run_no_luminance_no_crash     ---
# --- above (minus that test's own @requires_siril, since stretch_rgb/    ---
# --- stretch_and_compose are mocked here too).                           ---


def _write_fake_master(path: Path, stackcnt: int = 10, seed: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.random.default_rng(seed).uniform(0.05, 0.5, size=(16, 16)).astype(np.float32)
    hdu = fits.PrimaryHDU(data=data)
    hdu.header["PLTSOLVD"] = True
    hdu.header["STACKCNT"] = stackcnt
    hdu.writeto(path, overwrite=True)


def test_run_lrgb_stop_after_masters_returns_before_reconciliation_MOCKED(
    tmp_path: Path, monkeypatch
) -> None:
    """FAST, MOCKED counterpart of
    test_run_lrgb_stop_after_masters_stops_before_reconciliation (which is
    gated on a real, personal M51 project folder that isn't present even in
    this repo's own dev environment -- see the refactor plan's Section 0
    finding 2/3a). Asserts stop_after="masters" really does return before
    ANY reconciliation-stage code runs, by making
    run_graxpert_background_extraction raise if it's ever called."""
    import astro_pipeline.pipeline as pipeline_module

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            return {
                ("T99", "Fake Target", "Luminance", 1): [
                    _FakeLightFrame("l1.fit"), _FakeLightFrame("l2.fit"),
                ],
                ("T99", "Fake Target", "Red", 1): [_FakeLightFrame("r.fit")],
                ("T99", "Fake Target", "Green", 1): [_FakeLightFrame("g.fit")],
                ("T99", "Fake Target", "Blue", 1): [_FakeLightFrame("b.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {("T99", "Bias", 1, 0.0): ["b"], ("T99", "Dark", 1, 300.0): ["d"]}

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    build_master_calls = {"n": 0}

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *a, **k):
        build_master_calls["n"] += 1
        master_path = (
            pipeline_module.pipeline_dir(project_dir) / group_name / "lights"
            / f"master_{filter_name.lower()}.fit"
        )
        _write_fake_master(master_path)
        return master_path

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)

    stub_composite = tmp_path / "stub_rgb_colour_calibrated.fit"
    data = np.random.default_rng(1).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
    fits.PrimaryHDU(data=data).writeto(stub_composite)

    def fake_build_colour_contributor(project_dir, contrib_dir, report, telescope, target, binning, *a, **k):
        import shutil as _shutil

        contrib_dir.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(stub_composite, contrib_dir / "rgb_colour_calibrated.fit")
        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=stub_composite,
            sub_count=1, stack_total=1,
        )

    monkeypatch.setattr(pipeline_module, "_build_colour_contributor", fake_build_colour_contributor)

    def fail_if_called(*a, **k):
        raise AssertionError("must not run reconciliation-stage code when stop_after='masters'")

    monkeypatch.setattr(pipeline_module, "run_graxpert_background_extraction", fail_if_called)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = run_lrgb(
        project_dir, telescope="T99", target="Fake Target", ra_hours=1.0, dec_deg=1.0,
        lum_binning=1, rgb_binning=1, stop_after="masters",
    )

    assert build_master_calls["n"] == 1
    assert "Luminance" in result.masters
    assert result.composite_path is None
    assert result.export_result is None
    assert [cp.label for cp in result.checkpoints] == [
        "01_master_luminance", "02_primary_rgb_colour_calibrated",
    ]


def test_run_lrgb_stop_after_reconciled_then_final_does_not_rebuild_masters_MOCKED(
    tmp_path: Path, monkeypatch
) -> None:
    """FAST, MOCKED counterpart of
    test_run_lrgb_stop_after_reconciled_resumes_without_rebuilding_masters.
    build_master/_build_colour_contributor are mocked to replicate their
    OWN real resumability contract (skip and don't increment a call
    counter if the output file already exists) -- run_lrgb itself always
    calls them unconditionally every invocation; the skip-on-resume
    behavior lives inside those functions, not in run_lrgb's own loop, so
    a bare no-resumability mock would (wrongly) look like "masters get
    rebuilt on every call" no matter what run_lrgb actually does."""
    import astro_pipeline.pipeline as pipeline_module
    from astro_pipeline.reconciliation import ReconciliationResult
    from astro_pipeline.stretch_compose import ComposeResult

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            return {
                ("T99", "Fake Target", "Luminance", 1): [
                    _FakeLightFrame("l1.fit"), _FakeLightFrame("l2.fit"),
                ],
                ("T99", "Fake Target", "Red", 1): [_FakeLightFrame("r.fit")],
                ("T99", "Fake Target", "Green", 1): [_FakeLightFrame("g.fit")],
                ("T99", "Fake Target", "Blue", 1): [_FakeLightFrame("b.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {("T99", "Bias", 1, 0.0): ["b"], ("T99", "Dark", 1, 300.0): ["d"]}

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    build_master_calls = {"n": 0}

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *a, **k):
        master_path = (
            pipeline_module.pipeline_dir(project_dir) / group_name / "lights"
            / f"master_{filter_name.lower()}.fit"
        )
        if master_path.exists():
            return master_path
        build_master_calls["n"] += 1
        _write_fake_master(master_path)
        return master_path

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)

    stub_composite = tmp_path / "stub_rgb_colour_calibrated.fit"
    data = np.random.default_rng(1).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
    fits.PrimaryHDU(data=data).writeto(stub_composite)
    build_colour_calls = {"n": 0}

    def fake_build_colour_contributor(project_dir, contrib_dir, report, telescope, target, binning, *a, **k):
        import shutil as _shutil

        calibrated_path = contrib_dir / "rgb_colour_calibrated.fit"
        if not calibrated_path.exists():
            build_colour_calls["n"] += 1
            contrib_dir.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(stub_composite, calibrated_path)
        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=stub_composite,
            sub_count=1, stack_total=1,
        )

    monkeypatch.setattr(pipeline_module, "_build_colour_contributor", fake_build_colour_contributor)

    def fake_graxpert_bg(input_path, output_stem):
        output_path = Path(input_path).parent / f"{output_stem}.fits"
        _write_fake_master(output_path, seed=2)
        return output_path

    monkeypatch.setattr(pipeline_module, "run_graxpert_background_extraction", fake_graxpert_bg)

    def fake_reproject_to_reference(source_path, reference_path, output_path):
        import shutil as _shutil

        _shutil.copy2(source_path, output_path)
        return ReconciliationResult(
            source_path=Path(source_path), output_path=Path(output_path),
            footprint_min=1.0, footprint_mean=1.0, nan_fraction=0.0,
        )

    monkeypatch.setattr(pipeline_module, "reproject_to_reference", fake_reproject_to_reference)

    def fake_stretch_and_compose(lum_path, rgb_path, output_dir, output_stem, method="autostretch", **kwargs):
        composite_path = Path(output_dir) / f"{output_stem}.fit"
        data = np.random.default_rng(3).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
        fits.PrimaryHDU(data=data).writeto(composite_path, overwrite=True)
        return ComposeResult(
            composite_path=composite_path, lum_stretch_log=None, rgb_stretch_log=None, compose_log=None,
        )

    monkeypatch.setattr(pipeline_module, "stretch_and_compose", fake_stretch_and_compose)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result1 = run_lrgb(
        project_dir, telescope="T99", target="Fake Target", ra_hours=1.0, dec_deg=1.0,
        lum_binning=1, rgb_binning=1, stop_after="reconciled",
    )
    assert result1.composite_path is None
    assert build_master_calls["n"] == 1
    assert build_colour_calls["n"] == 1

    result2 = run_lrgb(
        project_dir, telescope="T99", target="Fake Target", ra_hours=1.0, dec_deg=1.0,
        lum_binning=1, rgb_binning=1, stop_after="final",
    )
    assert result2.composite_path is not None
    assert result2.export_result is not None
    # The Luminance master genuinely isn't rebuilt on the resumed call.
    assert build_master_calls["n"] == 1
    # The colour contributor genuinely isn't rebuilt on the resumed call
    # either. (Previously asserted `== 2` here, documenting a real,
    # pre-existing bug found while writing this test: run_lrgb's OSC loop
    # unconditionally overwrote colour_frame_hashes[contributor_key] for
    # the same key the mono-RGB loop just wrote, because
    # discover_osc_contributors() always pads its result with the caller's
    # own (telescope, rgb_binning) even with zero real Color/OSC data --
    # poisoning contributor_stale()'s comparison and forcing a full
    # rebuild on every resumed call, for any mono-RGB target. Fixed
    # 2026-09-16 in run_lrgb's OSC loop by skipping binnings with no real
    # OSC data -- see the fix's own comment in pipeline.py.)
    assert build_colour_calls["n"] == 1


def test_run_lrgb_multi_contributor_reconciliation_end_to_end_MOCKED(tmp_path: Path, monkeypatch) -> None:
    """Multi-contributor reconciliation (2+ RGB binnings combined via
    match_gain_offset + combine_same_grid), driven through run_lrgb itself
    -- not the underlying reconciliation.py functions in isolation, which
    are already covered by test_reconciliation.py. Before this test, this
    control flow (which contributor becomes the STACKCNT-weighted
    reference, how many times match_gain_offset/combine_same_grid are
    actually called and with what arguments, RunSignature.colour_reference)
    had zero coverage without a real multi-binning project folder (see the
    refactor plan's Section 4b, item 3). reproject_to_reference/
    crop_to_common_coverage/match_gain_offset/combine_same_grid are all
    mocked here -- this test is about run_lrgb's ORCHESTRATION of them,
    not their own internal correctness (already covered elsewhere)."""
    import shutil

    import astro_pipeline.pipeline as pipeline_module
    from astro_pipeline.reconciliation import GainOffsetFit, ReconciliationResult

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            return {
                ("T99", "Fake Target", "Luminance", 1): [
                    _FakeLightFrame("l1.fit"), _FakeLightFrame("l2.fit"),
                ],
                ("T99", "Fake Target", "Red", 1): [_FakeLightFrame("r1.fit")],
                ("T99", "Fake Target", "Green", 1): [_FakeLightFrame("g1.fit")],
                ("T99", "Fake Target", "Blue", 1): [_FakeLightFrame("b1.fit")],
                ("T99", "Fake Target", "Red", 2): [_FakeLightFrame("r2.fit")],
                ("T99", "Fake Target", "Green", 2): [_FakeLightFrame("g2.fit")],
                ("T99", "Fake Target", "Blue", 2): [_FakeLightFrame("b2.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {
                ("T99", "Bias", 1, 0.0): ["b"], ("T99", "Dark", 1, 300.0): ["d"],
                ("T99", "Bias", 2, 0.0): ["b"], ("T99", "Dark", 2, 300.0): ["d"],
            }

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *a, **k):
        master_path = (
            pipeline_module.pipeline_dir(project_dir) / group_name / "lights"
            / f"master_{filter_name.lower()}.fit"
        )
        _write_fake_master(master_path)
        return master_path

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)

    # BIN1 is the weaker contributor (stack_total=6), BIN2 the stronger one
    # (stack_total=20) -- BIN2 must be picked as the gain/offset reference,
    # NOT BIN1, even though BIN1 is the caller's own primary `rgb_binning`.
    stub_bin1 = tmp_path / "stub_bin1.fit"
    fits.PrimaryHDU(
        data=np.random.default_rng(1).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
    ).writeto(stub_bin1)
    stub_bin2 = tmp_path / "stub_bin2.fit"
    fits.PrimaryHDU(
        data=np.random.default_rng(2).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
    ).writeto(stub_bin2)
    stack_totals = {1: 6, 2: 20}

    def fake_build_colour_contributor(project_dir, contrib_dir, report, telescope, target, binning, *a, **k):
        stub = stub_bin1 if binning == 1 else stub_bin2
        contrib_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stub, contrib_dir / "rgb_colour_calibrated.fit")
        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=stub,
            sub_count=stack_totals[binning], stack_total=stack_totals[binning],
        )

    monkeypatch.setattr(pipeline_module, "_build_colour_contributor", fake_build_colour_contributor)

    def fake_graxpert_bg(input_path, output_stem):
        output_path = Path(input_path).parent / f"{output_stem}.fits"
        _write_fake_master(output_path, seed=3)
        return output_path

    monkeypatch.setattr(pipeline_module, "run_graxpert_background_extraction", fake_graxpert_bg)

    reproject_calls: list[Path] = []

    def fake_reproject_to_reference(source_path, reference_path, output_path):
        reproject_calls.append(Path(source_path))
        shutil.copy2(source_path, output_path)
        return ReconciliationResult(
            source_path=Path(source_path), output_path=Path(output_path),
            footprint_min=1.0, footprint_mean=1.0, nan_fraction=0.0,
        )

    monkeypatch.setattr(pipeline_module, "reproject_to_reference", fake_reproject_to_reference)

    def fake_crop_to_common_coverage(paths, output_dir):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_paths = []
        for i, p in enumerate(paths):
            out_path = output_dir / f"cropped_{i}.fit"
            shutil.copy2(p, out_path)
            out_paths.append(out_path)
        return out_paths

    monkeypatch.setattr(pipeline_module, "crop_to_common_coverage", fake_crop_to_common_coverage)

    match_gain_offset_calls: list[tuple[Path, Path]] = []

    def fake_match_gain_offset(path, reference_path, output_path):
        match_gain_offset_calls.append((Path(path), Path(reference_path)))
        shutil.copy2(path, output_path)
        return [GainOffsetFit(channel=0, gain=1.0, reference_background=0.1, contributor_background=0.1, n_pixels=100)]

    monkeypatch.setattr(pipeline_module, "match_gain_offset", fake_match_gain_offset)

    combine_calls: list[dict] = []

    def fake_combine_same_grid(paths, output_path, weights=None, reference_index=0):
        combine_calls.append(
            {"paths": [Path(p) for p in paths], "weights": weights, "reference_index": reference_index}
        )
        shutil.copy2(paths[reference_index], output_path)
        return Path(output_path)

    monkeypatch.setattr(pipeline_module, "combine_same_grid", fake_combine_same_grid)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = run_lrgb(
        project_dir, telescope="T99", target="Fake Target", ra_hours=1.0, dec_deg=1.0,
        lum_binning=1, rgb_binning=1, stop_after="reconciled",
    )

    assert result.composite_path is None  # stop_after="reconciled"
    assert len(reproject_calls) == 2  # both contributors reprojected onto L
    # BIN2 (stack_total=20) is the reference -> only BIN1 needs gain-matching.
    assert len(match_gain_offset_calls) == 1
    # match_gain_offset is called with the CROPPED path (crop_to_common_
    # coverage's own output), not the raw contributor composite -- cropped[0]
    # is L, cropped[1:] aligned 1:1 with `contributors` ([bin1, bin2]), so
    # index 1 (cropped_1.fit) is bin1, the non-reference contributor.
    assert match_gain_offset_calls[0][0].name == "cropped_1.fit"
    assert match_gain_offset_calls[0][1].name == "cropped_2.fit"  # reference (bin2)
    assert len(combine_calls) == 1
    assert combine_calls[0]["reference_index"] == 1  # BIN2 is contributors[1]
    assert combine_calls[0]["weights"] == [6.0, 20.0]  # STACKCNT order, not sorted by value

    signature_path = project_dir / "_pipeline" / "run_signature.json"
    import json

    persisted = json.loads(signature_path.read_text(encoding="utf-8"))
    assert persisted["colour_reference"] == "T99_bin2"


def test_run_lrgb_force_cascade_deletes_expected_top_level_files_MOCKED(
    tmp_path: Path, monkeypatch
) -> None:
    """Extends test_run_signature.py's
    test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale
    technique from "one call site, one argument" to the full top-level
    masters/reconciled/final cascade-delete block (pipeline.py's `if
    "masters"/"reconciled"/"final" in stages_to_invalidate:
    _delete_if_exists(...)`) -- the exact code Risk 3 of
    scratch/task5-oop-refactor-plan.md names as most likely to silently
    regress during the module split (a moved call site quietly losing an
    argument or a branch). Deliberately scoped to the three explicitly
    named top-level files plus the Luminance master (all reached via
    _delete_if_exists, which this test spies on) -- NOT the additional
    per-contributor files _clear_colour_contributor_products deletes via
    its own direct unlink() calls, which is a real but separate code path
    already exercised by its own dedicated tests elsewhere in this file.
    """
    import shutil

    import astro_pipeline.pipeline as pipeline_module
    from astro_pipeline.reconciliation import ReconciliationResult
    from astro_pipeline.stretch_compose import ComposeResult

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    class _FakeReport:
        def instrument_groups(self):
            return {
                ("T99", "Fake Target", "Luminance", 1): [
                    _FakeLightFrame("l1.fit"), _FakeLightFrame("l2.fit"),
                ],
                ("T99", "Fake Target", "Red", 1): [_FakeLightFrame("r.fit")],
                ("T99", "Fake Target", "Green", 1): [_FakeLightFrame("g.fit")],
                ("T99", "Fake Target", "Blue", 1): [_FakeLightFrame("b.fit")],
            }

        def calibrated_instrument_groups(self):
            return {}

        def calibration_index(self):
            return {("T99", "Bias", 1, 0.0): ["b"], ("T99", "Dark", 1, 300.0): ["d"]}

        def flat_index(self):
            return {}

    monkeypatch.setattr(pipeline_module, "scan_session", lambda project_dir: _FakeReport())

    call_counts = {"build_master": 0, "build_colour": 0, "graxpert": 0, "reproject": 0, "stretch": 0}

    def fake_build_master(project_dir, lights, cal_index, group_name, filter_name, *a, **k):
        master_path = (
            pipeline_module.pipeline_dir(project_dir) / group_name / "lights"
            / f"master_{filter_name.lower()}.fit"
        )
        if master_path.exists():
            return master_path
        call_counts["build_master"] += 1
        _write_fake_master(master_path)
        return master_path

    monkeypatch.setattr(pipeline_module, "build_group_master", fake_build_master)

    stub_composite = tmp_path / "stub_rgb_colour_calibrated.fit"
    fits.PrimaryHDU(
        data=np.random.default_rng(1).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
    ).writeto(stub_composite)

    def fake_build_colour_contributor(project_dir, contrib_dir, report, telescope, target, binning, *a, **k):
        calibrated_path = contrib_dir / "rgb_colour_calibrated.fit"
        if not calibrated_path.exists():
            call_counts["build_colour"] += 1
            contrib_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(stub_composite, calibrated_path)
        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=stub_composite,
            sub_count=1, stack_total=1,
        )

    monkeypatch.setattr(pipeline_module, "_build_colour_contributor", fake_build_colour_contributor)

    def fake_graxpert_bg(input_path, output_stem):
        call_counts["graxpert"] += 1
        output_path = Path(input_path).parent / f"{output_stem}.fits"
        _write_fake_master(output_path, seed=2)
        return output_path

    monkeypatch.setattr(pipeline_module, "run_graxpert_background_extraction", fake_graxpert_bg)

    def fake_reproject_to_reference(source_path, reference_path, output_path):
        call_counts["reproject"] += 1
        shutil.copy2(source_path, output_path)
        return ReconciliationResult(
            source_path=Path(source_path), output_path=Path(output_path),
            footprint_min=1.0, footprint_mean=1.0, nan_fraction=0.0,
        )

    monkeypatch.setattr(pipeline_module, "reproject_to_reference", fake_reproject_to_reference)

    def fake_stretch_and_compose(lum_path, rgb_path, output_dir, output_stem, method="autostretch", **kwargs):
        call_counts["stretch"] += 1
        composite_path = Path(output_dir) / f"{output_stem}.fit"
        fits.PrimaryHDU(
            data=np.random.default_rng(3).uniform(0.05, 0.5, size=(3, 16, 16)).astype(np.float32)
        ).writeto(composite_path, overwrite=True)
        return ComposeResult(
            composite_path=composite_path, lum_stretch_log=None, rgb_stretch_log=None, compose_log=None,
        )

    monkeypatch.setattr(pipeline_module, "stretch_and_compose", fake_stretch_and_compose)

    real_delete_if_exists = pipeline_module._delete_if_exists
    deleted_names: list[str] = []

    def spy_delete_if_exists(path, reason, notes):
        if Path(path).exists():
            deleted_names.append(Path(path).name)
        real_delete_if_exists(path, reason, notes)

    monkeypatch.setattr(pipeline_module, "_delete_if_exists", spy_delete_if_exists)

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def _run(**kwargs):
        return run_lrgb(
            project_dir, telescope="T99", target="Fake Target", ra_hours=1.0, dec_deg=1.0,
            lum_binning=1, rgb_binning=1, **kwargs,
        )

    result = _run()
    assert result.export_result is not None
    assert call_counts == {"build_master": 1, "build_colour": 1, "graxpert": 1, "reproject": 1, "stretch": 1}

    # --- force={"final"}: only the final composite is stale -----------------
    deleted_names.clear()
    before = dict(call_counts)
    _run(force={"final"})
    assert set(deleted_names) == {"lrgb_final.fit"}
    assert call_counts["build_master"] == before["build_master"]
    # Colour is genuinely untouched by force={"final"}. (Previously
    # asserted `+ 1` here, documenting the same pre-existing OSC-loop bug
    # described in test_run_lrgb_stop_after_reconciled_then_final_does_not_rebuild_masters_MOCKED
    # -- fixed 2026-09-16.)
    assert call_counts["build_colour"] == before["build_colour"]
    assert call_counts["graxpert"] == before["graxpert"]
    assert call_counts["reproject"] == before["reproject"]
    assert call_counts["stretch"] == before["stretch"] + 1

    # --- force={"reconciled"}: reconciliation + final stale, masters aren't -
    deleted_names.clear()
    before = dict(call_counts)
    _run(force={"reconciled"})
    assert set(deleted_names) == {"rgb_reconciled.fit", "lrgb_final.fit"}
    assert call_counts["build_master"] == before["build_master"]
    assert call_counts["build_colour"] == before["build_colour"]  # bug fixed 2026-09-16, see note above
    assert call_counts["graxpert"] == before["graxpert"]
    assert call_counts["reproject"] == before["reproject"] + 1
    assert call_counts["stretch"] == before["stretch"] + 1

    # --- force={"masters"}: everything downstream, including the Luminance --
    # --- master itself, is stale ---------------------------------------------
    deleted_names.clear()
    before = dict(call_counts)
    _run(force={"masters"})
    assert set(deleted_names) == {"master_luminance.fit", "lum_bg.fits", "rgb_reconciled.fit", "lrgb_final.fit"}
    assert call_counts["build_master"] == before["build_master"] + 1
    assert call_counts["build_colour"] == before["build_colour"] + 1
    assert call_counts["graxpert"] == before["graxpert"] + 1
    assert call_counts["reproject"] == before["reproject"] + 1
    assert call_counts["stretch"] == before["stretch"] + 1
