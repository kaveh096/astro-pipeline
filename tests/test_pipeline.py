from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.background_color import UnknownInstrumentError
from astro_pipeline.calibration import FlatPolicy
from astro_pipeline.ingest import scan_session
from astro_pipeline.pipeline import (
    ColourContributor,
    _build_colour_contributor,
    contributor_dir,
    contributor_fwhm_arcsec,
    discover_luminance_contributors,
    infer_flat_policy,
    resolve_instrument_profile,
    resolve_lights,
    run_lrgb,
    select_luminance_source,
)

from conftest import FINAL_DIR, PIPELINE_DIR, PROJECT_DIR as REAL_SESSION_DIR, requires

requires_real_session = pytest.mark.skipif(
    not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine"
)


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
    t24_master = PIPELINE_DIR / "T24-jmwill+kaveh096-M51-Luminance-bin1" / "lights" / "master_luminance.fit"
    t21_master = PIPELINE_DIR / "T21-kaveh096-M51-Luminance-bin1" / "lights" / "master_luminance.fit"
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

    def __init__(self, user: str = "kaveh096") -> None:
        self.user = user


class _FakeReportGreenBlueOnly:
    """Red is present but Green is missing -- the skip must trigger on
    whichever filter is actually absent, not just the first one checked,
    and the log line must name it correctly."""

    def instrument_groups(self):
        return {("T24", "M51", "Red", 2): [_FakeLightFrame()]}

    def calibration_index(self):
        return {}


def test_build_colour_contributor_skips_on_missing_green_not_just_red(tmp_path: Path) -> None:
    """resolve_lights() for Red returns a non-empty list here, so the
    function would proceed to build_master() for Red -- which needs real
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
