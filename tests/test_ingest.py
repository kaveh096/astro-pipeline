from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.ingest import (
    CalibrationFrame,
    LightFrame,
    UnrecognizedFrame,
    classify_frame,
    classify_tree,
    scan_session,
)

# Real filenames observed in an actual iTelescope T24 delivery (M51, Jan 2025).
LIGHT_NAME = "raw-T24-observer1-M51-20250123-021344-Blue-BIN2-E-300-001.fit"
BIAS_NAME = "T24-observer1-Bias-000-LD20250203-LT171434-BIN1.fit"
DARK_NAME = "T24-observer1-Dark-300-LD20250203-LT155037-BIN1.fit"

from astro_pipeline.contributor_staleness import _flat_frame_hash

from conftest import LUM_USERS, RGB_USER
from conftest import PROJECT_DIR as REAL_SESSION_DIR
from conftest import ABELL31_PROJECT_DIR, requires_abell31_project
from conftest import M42_PROJECT_DIR, requires_m42_project
from conftest import M31_PROJECT_DIR, requires_m31_project
from conftest import NGC3628_PROJECT_DIR, requires_ngc3628_project
from conftest import ABELL6_PROJECT_DIR, requires_abell6_project
from conftest import IC1396_PROJECT_DIR, requires_ic1396_project

# The other real collaborator on T24's real multi-user Luminance group --
# whichever of the two real configured usernames isn't RGB_USER (T24's
# BIN2 RGB and T21's Luminance are both single-user: RGB_USER only).
COLLABORATOR_USER = next(iter(LUM_USERS - {RGB_USER}), "collaborator1")


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
    name = "raw-T24-collaborator1-M51-20250225-030711-Luminance-BIN1-W-300-001.fit"
    frame = classify_frame(touch(tmp_path, name))
    assert isinstance(frame, LightFrame)
    assert frame.side == "W"


def test_classify_calibrated_provenance_light_frame(tmp_path: Path) -> None:
    name = "calibrated-T21-observer1-M51-20250113-045216-Luminance-BIN1-E-600-001.fit"
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
    # The filter is parsed from _FLAT_RE_SKYFLAT's existing named group and
    # must actually reach CalibrationFrame -- this was the real bug fixed
    # in flats Slice 1 (the group was parsed then discarded before ever
    # reaching the dataclass).
    assert frame.filter_name == "Luminance"


def test_classify_t24_style_flat_has_no_filter_token(tmp_path: Path) -> None:
    """_CAL_RE_T24's Flat branch (<telescope>-<user>-Flat-<exptime>-LD...-
    LT...-BIN<n>) syntactically matches frame_type="Flat" but the filename
    has no filter token at all -- T24 ships zero flats of any kind in real
    data, so this is a hypothetical/constructed filename, not something
    seen for real. classify_frame must still produce a CalibrationFrame
    with filter_name=None (not crash, not guess) -- flat_index() is what
    actually drops it (see test_flat_index_drops_flat_with_no_filter_name).
    """
    name = "T24-observer1-Flat-000-LD20250203-LT171434-BIN1.fit"
    frame = classify_frame(touch(tmp_path, name))
    assert isinstance(frame, CalibrationFrame)
    assert frame.frame_type == "Flat"
    assert frame.telescope == "T24"
    assert frame.filter_name is None


def test_flat_index_groups_by_telescope_binning_filter(tmp_path: Path) -> None:
    flat_dir = tmp_path / "Calibrations" / "T21" / "Flats" / "2024 06" / "raw flats"
    flat_dir.mkdir(parents=True)
    (flat_dir / "scope_Luminance_1x1_skyflat0.fit").write_bytes(b"")
    (flat_dir / "scope_Luminance_1x1_skyflat1.fit").write_bytes(b"")
    (flat_dir / "scope_Red_1x1_skyflat0.fit").write_bytes(b"")

    report = scan_session(tmp_path)
    index = report.flat_index()

    assert set(index.keys()) == {("T21", 1, "Luminance"), ("T21", 1, "Red")}
    assert len(index[("T21", 1, "Luminance")]) == 2
    assert len(index[("T21", 1, "Red")]) == 1


def test_flat_index_drops_flat_with_no_filter_name(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A flat classified under a filename convention with no filter token
    (see test_classify_t24_style_flat_has_no_filter_token) must be dropped
    by flat_index(), not silently mis-bucketed under a guessed filter --
    "refuse rather than guess", matching this project's established
    preference (e.g. resolve_instrument_profile's UnknownInstrumentError).
    A logged reason is required so the drop is discoverable, not silent.
    """
    touch(tmp_path, "T24-observer1-Flat-000-LD20250203-LT171434-BIN1.fit")

    report = scan_session(tmp_path)
    assert len(report.calibration) == 1
    assert report.calibration[0].frame_type == "Flat"
    assert report.calibration[0].filter_name is None

    with caplog.at_level("WARNING"):
        index = report.flat_index()

    assert index == {}
    assert any("filter" in record.message.lower() for record in caplog.records)


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
    touch(tmp_path, "raw-T24-observer1-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, "raw-T24-observer1-M51-20250123-023017-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, "raw-T24-observer1-M51-20250123-021344-Blue-BIN2-E-300-001.fit")
    touch(tmp_path, BIAS_NAME)

    report = scan_session(tmp_path)
    assert len(report.lights) == 3
    assert len(report.calibration) == 1

    groups = report.light_groups()
    lum_key = ("T24", "observer1", "M51", "Luminance", 1)
    blue_key = ("T24", "observer1", "M51", "Blue", 2)
    assert len(groups[lum_key]) == 2
    assert len(groups[blue_key]) == 1


def test_scan_ignores_pipeline_generated_output(tmp_path: Path) -> None:
    """The pipeline writes into <project>/_pipeline/, which sits inside the
    scanned tree. Without an exclusion a second run re-ingests its own
    staged copies and calibrated output as if they were new raw frames --
    verified real: a re-run counted 26 lights for a 13-light group.
    """
    touch(tmp_path, "raw-T24-observer1-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")

    generated = tmp_path / "_pipeline" / "T24-observer1-M51-Luminance-bin1" / "lights"
    generated.mkdir(parents=True)
    # A staged copy of the same raw frame, plus a calibrated derivative --
    # both would classify happily if they were not excluded.
    (generated / "raw-T24-observer1-M51-20250115-045740-Luminance-BIN1-E-300-001.fit").write_bytes(b"")
    (generated / "pp_lights_00001.fit").write_bytes(b"")

    report = scan_session(tmp_path)

    assert len(report.lights) == 1
    assert "_pipeline" not in report.lights[0].path.parts
    assert report.unrecognized == []


def test_light_groups_keeps_different_users_separate(tmp_path: Path) -> None:
    """Two collaborators shooting the same target/telescope/filter/binning
    must land in different groups -- confirmed real (observer1 and collaborator1
    both imaged M51 on T24), and silently merging them was a real bug."""
    touch(tmp_path, "raw-T24-observer1-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, "raw-T24-collaborator1-M51-20250226-020311-Luminance-BIN1-E-300-001.fit")

    report = scan_session(tmp_path)
    groups = report.light_groups()

    assert len(groups[("T24", "observer1", "M51", "Luminance", 1)]) == 1
    assert len(groups[("T24", "collaborator1", "M51", "Luminance", 1)]) == 1


def test_missing_calibration_warnings_flags_missing_flats_and_darks(tmp_path: Path) -> None:
    touch(tmp_path, "raw-T24-observer1-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
    touch(tmp_path, BIAS_NAME)
    touch(tmp_path, DARK_NAME)
    # no flats at all -- matches the real sample session exactly

    report = scan_session(tmp_path)
    warnings = report.missing_calibration_warnings()
    assert any("no bias" in w.lower() for w in warnings) is False  # bias IS present
    assert any("no flat" in w.lower() for w in warnings)


def test_missing_calibration_warnings_flags_missing_bias_and_dark(tmp_path: Path) -> None:
    touch(tmp_path, "raw-T24-observer1-M51-20250115-045740-Luminance-BIN1-E-300-001.fit")
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
# multiple filename conventions, zip-wrapped exposures). scan_session
# extracts zip-wrapped raw lights into _pipeline/_extracted_zips/ so they
# become real, pipeline-usable files -- non-FITS files (previews, master
# calibration TIFFs) remain index.py's job, covered in test_index.py.


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_scan_real_multi_telescope_session() -> None:
    report = scan_session(REAL_SESSION_DIR)

    raw_lights = [f for f in report.lights if f.provenance == "raw"]

    # T24 raw lights: 95, from TWO different real iTelescope users who
    # independently imaged the same target on the same telescope with
    # different binning choices for RGB (see conftest.LUM_USERS).
    t24_lights = [f for f in raw_lights if f.telescope == "T24"]
    assert len(t24_lights) == 95
    assert {f.user for f in t24_lights} == set(LUM_USERS)

    # T21 raw lights: only 2 in this delivery, both zip-wrapped and both
    # Luminance -- but at two different exposure lengths (600s and 300s),
    # so they land in the same light_groups() bucket (grouping is by
    # binning, not exptime) while still needing separate dark matching at
    # calibration time (calibration_index is keyed by exptime too).
    t21_lights = [f for f in raw_lights if f.telescope == "T21"]
    assert len(t21_lights) == 2
    assert {f.exptime for f in t21_lights} == {600.0, 300.0}
    assert all(f.user == RGB_USER for f in t21_lights)
    # Extracted to real files, not the zip path -- calibration.py needs an
    # actual FITS file it can copy/stage into a Siril sequence directory.
    assert all(f.path.suffix.lower() == ".fit" and f.path.exists() for f in t21_lights)

    # iTelescope-side-calibrated duplicates of the same exposures are present
    # too, and must NOT show up in light_groups() (which only ever returns
    # raw provenance) -- conflating them would double-process or silently
    # prefer one over the other.
    calibrated_lights = [f for f in report.lights if f.provenance == "calibrated"]
    assert len(calibrated_lights) > 0
    groups = report.light_groups()
    for frames in groups.values():
        assert all(f.provenance == "raw" for f in frames)

    # calibrated_light_groups() is the mirror view (Slice 1 of the
    # precalibrated-path plan): every "calibrated" provenance frame must be
    # retrievable there, and light_groups() (raw-only) must never include
    # them -- both views partition self.lights strictly by frame.provenance.
    calibrated_groups = report.calibrated_light_groups()
    for frames in calibrated_groups.values():
        assert all(f.provenance == "calibrated" for f in frames)
    assert sum(len(v) for v in calibrated_groups.values()) == len(calibrated_lights)

    calibrated_merged = report.calibrated_instrument_groups()
    assert sum(len(v) for v in calibrated_merged.values()) == len(calibrated_lights)

    # The two real users' data must land in SEPARATE groups, not merged --
    # confirmed real: without `user` in the grouping key, RGB_USER's 13
    # Luminance/BIN1 subs and COLLABORATOR_USER's 8 would have silently
    # combined into one group of 21.
    assert ("T24", RGB_USER, "M51", "Luminance", 1) in groups
    assert len(groups[("T24", RGB_USER, "M51", "Luminance", 1)]) == 13
    assert ("T24", COLLABORATOR_USER, "M51", "Luminance", 1) in groups
    assert len(groups[("T24", COLLABORATOR_USER, "M51", "Luminance", 1)]) == 8

    assert ("T24", RGB_USER, "M51", "Red", 2) in groups
    assert ("T24", RGB_USER, "M51", "Green", 2) in groups
    assert ("T24", RGB_USER, "M51", "Blue", 2) in groups
    # The collaborator shoots RGB at BIN1 (matching L directly -- no
    # drizzle/reproject reconciliation needed for their own contributed
    # frames, unlike RGB_USER's own BIN2 RGB).
    assert ("T24", COLLABORATOR_USER, "M51", "Red", 1) in groups
    assert ("T24", COLLABORATOR_USER, "M51", "Green", 1) in groups
    assert ("T24", COLLABORATOR_USER, "M51", "Blue", 1) in groups

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


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_flat_index_real_t21_has_all_eleven_filters_t24_has_none() -> None:
    """Real-scan verification (flats Slice 1): T21 shipped 330 real raw
    flat frames, 30 each across 11 filters, all BIN1 -- before this fix
    they all collapsed into one filter-blind calibration_index() bucket
    (fact 2 of plan-flats-v3.md); flat_index() must recover the real
    per-filter structure. T24 has zero flats of any kind anywhere in this
    delivery -- flat_index() must have zero entries for it.
    """
    report = scan_session(REAL_SESSION_DIR)
    index = report.flat_index()

    t21_bin1_filters = {filt for (telescope, binning, filt) in index if telescope == "T21" and binning == 1}
    assert t21_bin1_filters == {
        "B", "Blue", "Green", "Ha", "I", "Luminance", "OIII", "R", "Red", "SII", "V",
    }
    for filt in t21_bin1_filters:
        assert len(index[("T21", 1, filt)]) == 30

    assert not any(telescope == "T24" for (telescope, _binning, _filt) in index)


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_missing_calibration_warnings_real_per_filter_flat_check() -> None:
    """Real-scan property check (fact 3 / Slice 1.3): before this fix, T21
    having a flat for ANY filter silenced the "no flat" warning for every
    filter -- since T21 genuinely has all 11 filters, that bug was
    invisible on this data. After the fix, flat presence is checked per
    (telescope, binning, filter) actually needed by a real light group;
    T21 should show zero flat warnings (it genuinely has everything it
    needs) and T24 should keep warning for every filter/binning it needs,
    unchanged, since it has zero flats of any kind.
    """
    report = scan_session(REAL_SESSION_DIR)
    warnings = report.missing_calibration_warnings()

    assert not any("no flat" in w.lower() and "t21" in w.lower() for w in warnings)

    # NOT deduplicated into a set: T24's Luminance BIN1 group is shared by
    # two users (observer1 + collaborator1, see light_groups()'s user-keyed
    # grouping), so its identical-text warning legitimately appears twice.
    t24_flat_warnings = [w for w in warnings if "no flat" in w.lower() and "t24" in w.lower()]
    # T24's real light groups need: Luminance BIN1 (two user groups,
    # observer1 + collaborator1, each independently missing a flat), Red/Green/Blue
    # BIN1 (collaborator1) and BIN2 (observer1) -- matches this file's own real
    # T24 group assertions in test_scan_real_multi_telescope_session /
    # test_instrument_groups_merges_users_sharing_telescope_and_binning.
    assert len(t24_flat_warnings) == 8
    for binning, filt in [
        (1, "Luminance"), (1, "Red"), (1, "Green"), (1, "Blue"),
        (2, "Red"), (2, "Green"), (2, "Blue"),
    ]:
        assert any(f"BIN{binning}" in w and f"/{filt})" in w for w in t24_flat_warnings)


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_instrument_groups_merges_users_sharing_telescope_and_binning() -> None:
    """The unit build_master actually stacks from: the two real users'
    T24/BIN1 Luminance subs share a telescope and binning, so they must
    merge into ONE group (13 + 8 = 21) for raw-sub-level combining --
    better outlier rejection than averaging two separately-stacked
    masters. Their RGB, at different binnings (BIN2 vs BIN1), must NOT
    merge -- Siril can't register/stack frames of different pixel scale
    together at all.
    """
    report = scan_session(REAL_SESSION_DIR)
    groups = report.instrument_groups()

    assert ("T24", "M51", "Luminance", 1) in groups
    assert len(groups[("T24", "M51", "Luminance", 1)]) == 21
    assert {f.user for f in groups[("T24", "M51", "Luminance", 1)]} == set(LUM_USERS)

    assert ("T24", "M51", "Red", 2) in groups
    assert {f.user for f in groups[("T24", "M51", "Red", 2)]} == {RGB_USER}
    assert ("T24", "M51", "Red", 1) in groups
    assert {f.user for f in groups[("T24", "M51", "Red", 1)]} == {COLLABORATOR_USER}


# --- plan-flats-v4.md Step 1a: data invariants (test-only, behaviour-free).
# Gate (D): flat_index()/calibration_index() keys+counts, flat
# unique-basename counts, _flat_frame_hash(T21 L), and instrument_groups()
# counts, for every one of the plan's seven gate-(D) projects. All of these
# use TODAY's scan_session() with no fallback flag -- that parameter does
# not exist until Step 4b; this step only pins down today's real,
# unparameterized behaviour so 4a/4b have a real-data regression net to
# extend rather than invent from scratch. ------------------------------


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_flat_index_real_t21_luminance_unique_basenames_and_hash() -> None:
    """G1's own real numbers (plan §1.3.5/§1.5): T21's real Luminance
    flat set spans two twilight sessions with 10 colliding generic
    'skyflat<N>' basenames -- 30 matched CalibrationFrames collapse to only
    20 UNIQUE basenames. The existing frame-identity hash is blind to this
    (it hashes basenames, duplicates included) -- pinned here at its real,
    persisted value so Step 3b's fix has a documented before/after."""
    report = scan_session(REAL_SESSION_DIR)
    t21_l_flats = report.flat_index()[("T21", 1, "Luminance")]
    assert len(t21_l_flats) == 30
    assert len({f.path.name for f in t21_l_flats}) == 20
    assert _flat_frame_hash(t21_l_flats) == "349469062e57762c"


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_calibration_index_real_m51_snapshot() -> None:
    """A coarse calibration_index() snapshot for M51 -- both real
    telescopes have recognised Bias+Dark (RAW_LOCAL structurally possible
    for both), and T24 ships zero Flat of any kind (see
    test_t24_real_groups_have_no_matched_flats_and_command_is_unaffected
    in test_calibration.py -- the same fact from the flat_index() side)."""
    report = scan_session(REAL_SESSION_DIR)
    cal_index = report.calibration_index()
    telescopes_with_bias = {t for (t, ftype, _b, _e) in cal_index if ftype == "Bias"}
    telescopes_with_dark = {t for (t, ftype, _b, _e) in cal_index if ftype == "Dark"}
    assert telescopes_with_bias == {"T21", "T24"}
    assert telescopes_with_dark == {"T21", "T24"}
    assert not any(t == "T24" for (t, b, filt) in report.flat_index())


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_instrument_groups_real_m51_post_deletion_counts() -> None:
    """Pinned against the POST-DELETION tree (plan §4.0's event), per the
    plan's own Step 1a text -- T21 L bin1 = 2, T24 L bin1 = 21. The manual
    deletion of the byte-identical duplicate M51 folders
    (Uncalibrated Lights - Jan 2025/, calibrated Lights - T24 - Feb 2025/)
    is explicitly OUT OF SCOPE for the coding task that added this test
    (handled separately, outside this branch's own commits) -- until that
    deletion happens on this machine, this assertion is expected to be RED
    for the identical, already-documented reason as this file's own
    test_scan_real_multi_telescope_session /
    test_instrument_groups_merges_users_sharing_telescope_and_binning
    (§1.5: today's tree gives T21=4, T24=42, not 2/21). It turns green
    with zero code change the moment the deletion happens."""
    report = scan_session(REAL_SESSION_DIR)
    groups = report.instrument_groups()
    assert len(groups.get(("T21", "M51", "Luminance", 1), [])) == 2
    assert len(groups.get(("T24", "M51", "Luminance", 1), [])) == 21


@requires_ngc3628_project
def test_scan_real_ngc3628_no_calibration_frames_recognized_today() -> None:
    """NGC 3628/T73 (Feb 2025): PRECALIBRATED, CALSTAT='BF' -- no local
    Bias/Dark of any kind, so calibration_index() must be empty for T73."""
    report = scan_session(NGC3628_PROJECT_DIR)
    assert not any(t == "T73" for (t, _ftype, _b, _e) in report.calibration_index())
    assert not any(t == "T73" for (t, _b, _filt) in report.flat_index())


@requires_abell6_project
def test_scan_real_abell6_no_calibration_frames_recognized_today() -> None:
    """Abell 6 and HFG1/T02 (Dec 2022): PRECALIBRATED, CALSTAT='BDF' -- no
    local Bias/Dark of any kind."""
    report = scan_session(ABELL6_PROJECT_DIR)
    assert not any(t == "T02" for (t, _ftype, _b, _e) in report.calibration_index())
    assert not any(t == "T02" for (t, _b, _filt) in report.flat_index())


@requires_abell31_project
def test_scan_real_abell31_no_calibration_frames_recognized_today() -> None:
    """Abell 31/T59 (Mar 2023), a NEW gate-(D) fixture added by Step 1a:
    PRECALIBRATED, CALSTAT='BDF', no unrecognised calibration frames of any
    kind (plan §2.1) -- included for completeness even though it has
    nothing local to recognise."""
    report = scan_session(ABELL31_PROJECT_DIR)
    raw_lights = [f for f in report.lights if f.provenance == "raw"]
    assert len(raw_lights) == 30
    assert not any(t == "T59" for (t, _ftype, _b, _e) in report.calibration_index())
    assert not any(t == "T59" for (t, _b, _filt) in report.flat_index())
    assert len(report.unrecognized) == 0


@requires_m42_project
def test_scan_real_m42_flats_and_bias_dark_unrecognized_today() -> None:
    """M42/T20 (Jan 2022), a NEW gate-(D) fixture added by Step 1a: real
    raw lights across 7 filters (plan §2.1's own counts), a real 70-file
    flat library and real bias/dark -- NONE of the calibration frames are
    recognised today (no 'T20' folder token anywhere in their path; only
    the LIGHT filenames carry a T20 token) -- the real fixture Step 4b's
    header-based fallback recognition targets."""
    report = scan_session(M42_PROJECT_DIR)
    raw_lights = [f for f in report.lights if f.provenance == "raw"]
    assert len(raw_lights) == 73
    counts = {}
    for f in raw_lights:
        counts[(f.telescope, f.filter_name, f.binning)] = counts.get((f.telescope, f.filter_name, f.binning), 0) + 1
    assert counts == {
        ("T20", "Luminance", 1): 10,
        ("T20", "Red", 2): 8,
        ("T20", "Green", 2): 6,
        ("T20", "Blue", 2): 7,
        ("T20", "Ha", 2): 15,
        ("T20", "OIII", 2): 14,
        ("T20", "SII", 2): 13,
    }
    assert not any(t == "T20" for (t, _ftype, _b, _e) in report.calibration_index())
    assert not any(t == "T20" for (t, _b, _filt) in report.flat_index())
    assert len(report.unrecognized) == 200


@requires_m31_project
def test_scan_real_m31_bias_dark_unrecognized_today() -> None:
    """M31/T05 (Aug 2021), a NEW gate-(D) fixture added by Step 1a: real
    folder/user token literally 'T5' (not 'T05') -- the real fixture for
    Step 4b's T5->T05 telescope-token normalisation. R/G/B lights ARE
    recognised today (the light-filename convention already embeds a
    telescope token); bias/dark/flat are NOT (their folder is 'T5', which
    the strict ^T\\d+$ directory rule uppercases to 'T5', not 'T05' -- and
    T5 never even matches the light-side telescope token in the first
    place, so they land in calibration_index() under nothing at all today)."""
    report = scan_session(M31_PROJECT_DIR)
    raw_lights = [f for f in report.lights if f.provenance == "raw"]
    assert len(raw_lights) == 21
    counts = {}
    for f in raw_lights:
        counts[(f.telescope, f.filter_name, f.binning)] = counts.get((f.telescope, f.filter_name, f.binning), 0) + 1
    assert counts == {
        ("T05", "Red", 1): 7,
        ("T05", "Green", 1): 7,
        ("T05", "Blue", 1): 7,
    }
    assert not any(t in ("T05", "T5") for (t, _ftype, _b, _e) in report.calibration_index())
    assert not any(t in ("T05", "T5") for (t, _b, _filt) in report.flat_index())
    assert len(report.unrecognized) == 151


@requires_ic1396_project
def test_scan_real_ic1396_flats_and_bias_unrecognized_today() -> None:
    """IC 1396/T68 (Sep 2021), extending the existing IC1396 fixture
    (already used in test_calibration.py) to ingest.py's own gate-(D)
    checks: real OSC (Color) lights recognised, real 88-frame flat library
    and real 48-frame bias NOT recognised at all today (no 'T68' folder
    token in their path)."""
    report = scan_session(IC1396_PROJECT_DIR)
    raw_lights = [f for f in report.lights if f.provenance == "raw"]
    assert len(raw_lights) == 25
    assert not any(t == "T68" for (t, _ftype, _b, _e) in report.calibration_index())
    assert not any(t == "T68" for (t, _b, _filt) in report.flat_index())


# --- plan-flats-v4.md Step 4a: classify_tree -- pure, zip-aware refactor
# extracted out of scan_session, with an equality gate against
# scan_session's own real output (behaviour-preserving). --------------


def test_classify_tree_replace_not_append_zip_peeked_vs_extracted(tmp_path: Path) -> None:
    """The dedicated mocked test for the replace-not-append rule (Step
    4a): a zip whose peeked light and extracted light must not both end
    up in report.lights -- exactly the double-count class of bug
    documented in §1.5 (two zips of the same lights each contributing a
    LightFrame, inflating a real group from 21 to 42)."""
    import zipfile

    project = tmp_path / "project"
    project.mkdir()
    light_name = "raw-T24-observer1-M51-20250123-021344-Luminance-BIN1-E-300-001.fit"
    zip_path = project / "Lights.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(light_name, b"not a real fits file, never read by classify_filename")

    peeked_report = classify_tree(project)
    assert len(peeked_report.lights) == 1
    assert peeked_report.lights[0].path.parent.suffix.lower() == ".zip"  # a placeholder, not a real file

    real_report = scan_session(project)
    assert len(real_report.lights) == 1  # NOT 2 -- replaced, not appended
    assert real_report.lights[0].path.exists()
    assert real_report.lights[0].path.parent.suffix.lower() != ".zip"
    assert real_report.lights[0].path.name == light_name


def test_scan_session_classifies_each_zip_entry_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """The real, unconditional double-work the reviewer flagged:
    scan_session() used to re-open every zip and re-run
    classify_filename() on every entry a second time (via
    _extract_zipped_lights), AFTER classify_tree() had already opened and
    classified the exact same zip once (via _peek_zipped_lights) --
    zf.namelist()/classify_filename() ran twice per zip, once discarded.

    Now classify_tree() hands scan_session() the real (zip_path,
    inner_name, fields) tuples it already computed
    (IngestReport.zip_peeks), so scan_session() only ever extracts, never
    re-classifies. Verified directly: classify_filename() (imported into
    this module and called by both the old and new code paths) must be
    called exactly once per real zip entry across one scan_session() call,
    not twice."""
    import zipfile

    import astro_pipeline.ingest as ingest_module

    project = tmp_path / "project"
    project.mkdir()
    light_names = [
        "raw-T24-observer1-M51-20250123-021344-Luminance-BIN1-E-300-001.fit",
        "raw-T24-observer1-M51-20250123-021400-Luminance-BIN1-E-300-002.fit",
    ]
    zip_path = project / "Lights.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in light_names:
            zf.writestr(name, b"not a real fits file, never read by classify_filename")

    call_count = 0
    real_classify_filename = ingest_module.classify_filename

    def counting_classify_filename(basename, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_classify_filename(basename, **kwargs)

    monkeypatch.setattr(ingest_module, "classify_filename", counting_classify_filename)

    report = scan_session(project)

    assert len(report.lights) == len(light_names)
    assert all(f.path.exists() for f in report.lights)
    # One classify_filename() call per real zip entry -- NOT doubled by a
    # second, redundant re-scan of the same zip.
    assert call_count == len(light_names)


def test_classify_tree_calibration_and_unrecognized_untouched_by_zip_logic(tmp_path: Path) -> None:
    """Bare-file calibration/unrecognized classification is completely
    unaffected by the zip-peeking half of classify_tree -- only raw-
    provenance LIGHT entries inside a zip are ever peeked (mirroring
    _materialize_zipped_light's own real, narrower scope: no real
    delivery ships bias/dark/flat inside a zip)."""
    project = tmp_path / "project"
    (project / "T24").mkdir(parents=True)
    bias_path = project / "T24" / "T24-observer1-Bias-000-LD20250203-LT171434-BIN1.fit"
    bias_path.write_bytes(b"")
    report = classify_tree(project)
    assert len(report.calibration) == 1
    assert report.calibration[0].frame_type == "Bias"
    assert report.unrecognized == []


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_classify_tree_real_m51_calibration_and_unrecognized_match_scan_session() -> None:
    """Gate (D): classify_tree's own calibration/unrecognized classification
    (unaffected by zip-extraction, which only ever touches raw-provenance
    lights) is byte-identical to scan_session's, on the real M51 tree --
    including its own real zips."""
    via_classify = classify_tree(REAL_SESSION_DIR)
    via_scan = scan_session(REAL_SESSION_DIR)
    assert via_classify.calibration == via_scan.calibration
    assert via_classify.unrecognized == via_scan.unrecognized
    # Light COUNTS match -- each zip-peeked placeholder in classify_tree's
    # own output corresponds 1:1 to a real extracted light in
    # scan_session's (replace, not append/drop); at least one real zip
    # placeholder actually exists on this tree, so this is a real check,
    # not vacuously true.
    assert len(via_classify.lights) == len(via_scan.lights)
    zip_placeholders = [f for f in via_classify.lights if f.path.parent.suffix.lower() == ".zip"]
    assert len(zip_placeholders) > 0
    # Every genuinely bare-file light (present via the SAME FIT_GLOB_PATTERNS
    # loop in both classify_tree and the original scan_session) is
    # IDENTICAL between the two, in the same relative order.
    bare_classify = [f for f in via_classify.lights if f.path.parent.suffix.lower() != ".zip"]
    assert all(f in via_scan.lights for f in bare_classify)


@requires_ngc3628_project
@requires_abell6_project
def test_classify_tree_real_light_counts_match_scan_session_ngc3628_and_abell6() -> None:
    """Gate (D), extended to two more real gate-(D) projects: neither has
    any real zips, so classify_tree's output must be BYTE-IDENTICAL to
    scan_session's (no placeholders to replace at all)."""
    for project_dir in (NGC3628_PROJECT_DIR, ABELL6_PROJECT_DIR):
        via_classify = classify_tree(project_dir)
        via_scan = scan_session(project_dir)
        assert via_classify.lights == via_scan.lights
        assert via_classify.calibration == via_scan.calibration
        assert via_classify.unrecognized == via_scan.unrecognized


# --- plan-flats-v4.md Step 4b: opt-in header-based calibration
# recognition, without a mode flip (G4/G5). Mocked rule-by-rule tests,
# then real gate-(D) flag-on checks. -------------------------------------


def _write_cal_fits(
    path: Path,
    *,
    imagetyp: str | None = None,
    xbinning: int | None = None,
    ybinning: int | None = None,
    exptime: float | None = None,
    filter_name: str | None = None,
    instrume: str | None = None,
    naxis1: int = 8,
    naxis2: int = 8,
    calstat: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(data=np.zeros((naxis2, naxis1), dtype=np.uint16))
    if imagetyp is not None:
        hdu.header["IMAGETYP"] = imagetyp
    if xbinning is not None:
        hdu.header["XBINNING"] = xbinning
    if ybinning is not None:
        hdu.header["YBINNING"] = ybinning
    if exptime is not None:
        hdu.header["EXPTIME"] = exptime
    if filter_name is not None:
        hdu.header["FILTER"] = filter_name
    if instrume is not None:
        hdu.header["INSTRUME"] = instrume
    if calstat is not None:
        hdu.header["CALSTAT"] = calstat
    hdu.writeto(path)


def _setup_project_with_one_raw_light(tmp_path: Path, telescope: str = "T20", instrume: str = "CAM1") -> Path:
    """A minimal project with exactly one bare raw light for `telescope`
    (used as the cross-check reference every fallback candidate needs) --
    no filename-recognised calibration frames of any kind, so nothing is
    ever shadowed."""
    project = tmp_path / "project"
    light_path = project / "lights" / f"raw-{telescope}-observer1-Target-20260101-000000-Luminance-BIN1-E-300-001.fit"
    _write_cal_fits(light_path, instrume=instrume, xbinning=1, ybinning=1, naxis1=8, naxis2=8)
    return project


def test_classify_tree_fallback_off_by_default_leaves_unrecognized(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    unrec_path = project / "cal" / "unknown_bias.fit"
    _write_cal_fits(unrec_path, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report_off = classify_tree(project)
    assert not any(f.frame_type == "Bias" for f in report_off.calibration)

    report_on = classify_tree(project, calibration_header_fallback=True)
    assert any(f.frame_type == "Bias" and f.source == "header" for f in report_on.calibration)


def test_classify_tree_fallback_imagetyp_normalises_bias_dark_flat(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    for i, (imagetyp, expected) in enumerate([
        ("Bias Frame", "Bias"), ("BIAS", "Bias"),
        ("Dark Frame", "Dark"), ("dark", "Dark"),
        ("FLAT", "Flat"), ("Flat Field", "Flat"),
    ]):
        p = project / "cal" / f"frame_{i}.fit"
        kwargs = dict(imagetyp=imagetyp, xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)
        if expected == "Dark":
            kwargs["exptime"] = 300.0
        if expected == "Flat":
            kwargs["filter_name"] = "Luminance"
        _write_cal_fits(p, **kwargs)

    report = classify_tree(project, calibration_header_fallback=True)
    types = {f.path.name: f.frame_type for f in report.calibration if f.source == "header"}
    assert types["frame_0.fit"] == "Bias"
    assert types["frame_1.fit"] == "Bias"
    assert types["frame_2.fit"] == "Dark"
    assert types["frame_3.fit"] == "Dark"
    assert types["frame_4.fit"] == "Flat"
    assert types["frame_5.fit"] == "Flat"


def test_classify_tree_fallback_never_classifies_a_light_frame(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "sneaky.fit"
    _write_cal_fits(p, imagetyp="Light Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert report.calibration == [] or all(f.path != p for f in report.calibration)
    assert any(f.path == p for f in report.unrecognized)


def test_classify_tree_fallback_rejects_calstat_containing_m(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "master_bias.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8, calstat="M")

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)
    assert any(f.path == p for f in report.unrecognized)


def test_classify_tree_fallback_rejects_unreadable_header(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "corrupt.fit"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not a real fits file")

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


def test_classify_tree_fallback_rejects_xbinning_ybinning_mismatch(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "asym_bias.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=2, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)
    assert any(f.path == p for f in report.unrecognized)


def test_classify_tree_fallback_bias_exptime_forced_to_zero(tmp_path: Path) -> None:
    """A non-zero-exptime bias (real cameras sometimes record a nonzero
    EXPTIME on a bias frame) still gets exptime=0.0 -- matching the exact
    key master_builder.py's bias lookup uses."""
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "bias_nonzero.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, exptime=0.037, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    matches = [f for f in report.calibration if f.path == p]
    assert len(matches) == 1
    assert matches[0].exptime == 0.0


def test_classify_tree_fallback_dark_exptime_rounds_to_one_decimal(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "dark_239_999.fit"
    _write_cal_fits(p, imagetyp="Dark Frame", xbinning=1, ybinning=1, exptime=239.999, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    matches = [f for f in report.calibration if f.path == p]
    assert len(matches) == 1
    assert matches[0].exptime == 240.0


def test_classify_tree_fallback_dark_malformed_exptime_skipped_not_crashed(tmp_path: Path) -> None:
    """A Dark frame's header EXPTIME can be malformed/non-numeric (empty
    string, 'N/A', an astropy Undefined) -- must be treated like every
    other malformed-header case in this function (skip just this one
    frame into report.unrecognized) rather than raising an unhandled
    ValueError/TypeError that crashes the whole classify_tree call. Before
    the fix, this raised; every sibling FITS-header-to-float conversion in
    this diff (e.g. flat_sanity_notes() in calibration.py) already guarded
    against exactly this."""
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "dark_malformed_exptime.fit"
    _write_cal_fits(p, imagetyp="Dark Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)
    # _write_cal_fits' own exptime param is float-typed -- write the
    # malformed, non-numeric value directly to express this case.
    with fits.open(p, mode="update") as hdul:
        hdul[0].header["EXPTIME"] = "N/A"

    report = classify_tree(project, calibration_header_fallback=True)  # must not raise
    assert not any(f.path == p for f in report.calibration)
    assert any(f.path == p for f in report.unrecognized)


def test_classify_tree_fallback_flat_requires_filter(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "flat_no_filter.fit"
    _write_cal_fits(p, imagetyp="FLAT", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


def test_classify_tree_fallback_flat_filter_from_header(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path)
    p = project / "cal" / "flat_ok.fit"
    _write_cal_fits(p, imagetyp="FLAT", xbinning=1, ybinning=1, filter_name="Ha", instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    matches = [f for f in report.calibration if f.path == p]
    assert len(matches) == 1
    assert matches[0].filter_name == "Ha"
    assert matches[0].source == "header"


def test_classify_tree_fallback_no_readable_raw_light_to_cross_check(tmp_path: Path) -> None:
    """A telescope attributable via rule (c) (the project's only raw
    lights) but at a BINNING with no raw light at all -- refused, with the
    specific documented reason."""
    project = _setup_project_with_one_raw_light(tmp_path)  # BIN1 only
    p = project / "cal" / "bias_bin2.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=2, ybinning=2, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)
    match = next(f for f in report.unrecognized if f.path == p)
    assert "no readable raw light to cross-check" in match.reason


def test_classify_tree_fallback_instrume_mismatch_rejected(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path, instrume="CAM1")
    p = project / "cal" / "bias_wrong_cam.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM2", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


def test_classify_tree_fallback_naxis_mismatch_rejected(tmp_path: Path) -> None:
    """The real T68 2021-vs-2023 case (§2.1): identical INSTRUME string,
    genuinely different sensor resolution -- NAXIS is the discriminator."""
    project = _setup_project_with_one_raw_light(tmp_path, instrume="CAM1")
    p = project / "cal" / "bias_wrong_size.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=16, naxis2=16)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


def test_classify_tree_fallback_telescope_attribution_rule_a_strict_folder(tmp_path: Path) -> None:
    project = _setup_project_with_one_raw_light(tmp_path, telescope="T99")
    p = project / "T99" / "Bias" / "bias0.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    match = next((f for f in report.calibration if f.path == p), None)
    assert match is not None
    assert match.telescope == "T99"


def test_classify_tree_fallback_telescope_attribution_rule_b_ancestor_token_normalised(tmp_path: Path) -> None:
    """M31/T05's real shape: the project ROOT name itself carries a 'T5'
    token (surrounded by non-alphanumerics) that must normalise to 'T05',
    matching the same telescope its raw lights use."""
    project = tmp_path / "M31 - Andromeda - T5 - RGB"
    light_path = project / "lights" / "raw-T05-observer1-M31-20260101-000000-Red-BIN1-E-300-001.fit"
    _write_cal_fits(light_path, instrume="CAM1", xbinning=1, ybinning=1, naxis1=8, naxis2=8)
    p = project / "bias" / "bias0.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    match = next((f for f in report.calibration if f.path == p), None)
    assert match is not None
    assert match.telescope == "T05"


def test_classify_tree_fallback_telescope_attribution_rule_b_ambiguous_multi_token_refused(tmp_path: Path) -> None:
    """M51's real shape: a root name with TWO distinct T<digits> tokens
    ("... T24 & T21 ...") is ambiguous -- refused, not guessed."""
    project = tmp_path / "M51 - Whirlpool - T24 & T21"
    light_path = project / "lights" / "raw-T24-observer1-M51-20260101-000000-Red-BIN1-E-300-001.fit"
    _write_cal_fits(light_path, instrume="CAM1", xbinning=1, ybinning=1, naxis1=8, naxis2=8)
    p = project / "cal" / "bias0.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


def test_classify_tree_fallback_telescope_attribution_rule_c_multiple_telescopes_refused(tmp_path: Path) -> None:
    """Rule (c) needs EXACTLY one telescope's worth of raw lights -- two
    telescopes present with no folder/token attribution is ambiguous."""
    project = tmp_path / "project"
    _write_cal_fits(
        project / "lights" / "raw-T20-observer1-X-20260101-000000-Red-BIN1-E-300-001.fit",
        instrume="CAM1", xbinning=1, ybinning=1, naxis1=8, naxis2=8,
    )
    _write_cal_fits(
        project / "lights" / "raw-T21-observer1-X-20260101-000000-Red-BIN1-E-300-001.fit",
        instrume="CAM1", xbinning=1, ybinning=1, naxis1=8, naxis2=8,
    )
    p = project / "cal" / "bias0.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


def test_classify_tree_fallback_shadowing_rule(tmp_path: Path) -> None:
    """A telescope with a REAL filename-recognised Bias must never absorb
    a header-only Bias candidate too -- shadowed, listed unrecognized with
    the documented reason, never merged into the same index entry."""
    project = tmp_path / "project"
    light_path = project / "lights" / "raw-T24-observer1-M51-20260101-000000-Red-BIN1-E-300-001.fit"
    _write_cal_fits(light_path, instrume="CAM1", xbinning=1, ybinning=1, naxis1=8, naxis2=8)
    # A real, filename-recognised T24 bias (T24-style convention).
    filename_bias = project / "T24-observer1-Bias-000-LD20260101-LT000000-BIN1.fit"
    _write_cal_fits(filename_bias, instrume="CAM1", xbinning=1, ybinning=1, naxis1=8, naxis2=8)
    # A header-only candidate bias for the SAME telescope+type.
    header_bias = project / "cal" / "extra_bias.fit"
    _write_cal_fits(header_bias, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == header_bias for f in report.calibration)
    match = next(f for f in report.unrecognized if f.path == header_bias)
    assert "shadowed" in match.reason


def test_classify_tree_fallback_filename_vs_header_disagreement_refused(tmp_path: Path) -> None:
    """A telescope-less camera-model filename says 'dark', but the header
    says 'Bias Frame' -- the filename wins; disagreement leaves it
    unrecognized rather than trusting the header."""
    project = _setup_project_with_one_raw_light(tmp_path, telescope="T99")
    # "<camera>-<index>dark<exptime>secBin<n>.fit" -- matches _CAL_RE_CAMERA
    # as a Dark, but with no telescope hint (no T<digits> folder above it).
    p = project / "cal" / "CAM1-0001dark300secBin1.fit"
    _write_cal_fits(p, imagetyp="Bias Frame", xbinning=1, ybinning=1, instrume="CAM1", naxis1=8, naxis2=8)

    report = classify_tree(project, calibration_header_fallback=True)
    assert not any(f.path == p for f in report.calibration)


@requires_m42_project
def test_classify_tree_fallback_real_m42_counts_match_known_numbers() -> None:
    """Real gate (D), flag-on: M42/T20's real bias/dark/flat counts,
    exactly as plan-flats-v4.md §2.1 documents (verified directly against
    this real delivery before writing this test): bias 50 (BIN1) + 49
    (BIN2, +1 truncated/unreadable), darks 10 (BIN1, 180s) + 10 (BIN2,
    180s) + 10 (BIN2, 300s), flats 10 each of L/R/G/B/Ha/SII (BIN1 or
    BIN2) + 9 OIII (+1 truncated)."""
    report = classify_tree(M42_PROJECT_DIR, calibration_header_fallback=True)
    cal_index = report.calibration_index()
    assert len(cal_index.get(("T20", "Bias", 1, 0.0), [])) == 50
    assert len(cal_index.get(("T20", "Bias", 2, 0.0), [])) == 49
    assert len(cal_index.get(("T20", "Dark", 1, 180.0), [])) == 10
    assert len(cal_index.get(("T20", "Dark", 2, 180.0), [])) == 10
    assert len(cal_index.get(("T20", "Dark", 2, 300.0), [])) == 10
    assert all(getattr(f, "source", "filename") == "header" for f in cal_index[("T20", "Bias", 1, 0.0)])

    flat_index = report.flat_index()
    for filt in ("Luminance", "Red", "Green", "Blue", "Ha", "SII"):
        binning = 1 if filt == "Luminance" else 2
        assert len(flat_index.get(("T20", binning, filt), [])) == 10
    assert len(flat_index.get(("T20", 2, "OIII"), [])) == 9


@requires_ic1396_project
def test_classify_tree_fallback_real_ic1396_counts_match_known_numbers() -> None:
    """Real gate (D), flag-on: IC 1396/T68's real bias 48, darks 50, flats
    88 (all Color, BIN1) -- plan §2.1's own numbers."""
    report = classify_tree(IC1396_PROJECT_DIR, calibration_header_fallback=True)
    cal_index = report.calibration_index()
    assert len(cal_index.get(("T68", "Bias", 1, 0.0), [])) == 48
    assert len(cal_index.get(("T68", "Dark", 1, 240.0), [])) == 50
    flat_index = report.flat_index()
    assert len(flat_index.get(("T68", 1, "Color"), [])) == 88


@requires_m31_project
def test_classify_tree_fallback_real_m31_counts_match_known_numbers() -> None:
    """Real gate (D), flag-on: M31/T05's real bias 15, darks 16, flats 40
    each of R/G/B (BIN1) -- plan §2.1's own numbers. The T5->T05
    ancestor-token normalisation is load-bearing here (the project root is
    literally named "M31 - Andromeda - T5 - RGB - Aug 2021")."""
    report = classify_tree(M31_PROJECT_DIR, calibration_header_fallback=True)
    cal_index = report.calibration_index()
    assert len(cal_index.get(("T05", "Bias", 1, 0.0), [])) == 15
    assert len(cal_index.get(("T05", "Dark", 1, 180.0), [])) == 16
    flat_index = report.flat_index()
    assert len(flat_index.get(("T05", 1, "Red"), [])) == 40
    assert len(flat_index.get(("T05", 1, "Green"), [])) == 40
    assert len(flat_index.get(("T05", 1, "Blue"), [])) == 40


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
def test_classify_tree_fallback_real_m51_indexes_unchanged() -> None:
    """Behaviour change: none by default, and none in MODE even with the
    flag on (Decision Q2). M51's own indexes (already fully filename-
    recognised) must be byte-identical with the flag on or off."""
    report_off = scan_session(REAL_SESSION_DIR)
    report_on = scan_session(REAL_SESSION_DIR, calibration_header_fallback=True)
    assert report_off.calibration_index() == report_on.calibration_index()
    assert report_off.flat_index() == report_on.flat_index()


@requires_ngc3628_project
def test_classify_tree_fallback_real_ngc3628_indexes_unchanged() -> None:
    report_off = scan_session(NGC3628_PROJECT_DIR)
    report_on = scan_session(NGC3628_PROJECT_DIR, calibration_header_fallback=True)
    assert report_off.calibration_index() == report_on.calibration_index()
    assert report_off.flat_index() == report_on.flat_index()


@pytest.mark.skipif(not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine")
@requires_ngc3628_project
@requires_abell6_project
@requires_abell31_project
@requires_m42_project
@requires_ic1396_project
@requires_m31_project
def test_infer_calibration_mode_unchanged_flag_on_for_every_telescope_all_seven_projects() -> None:
    """Decision Q2 ("Keep", permanent): with the flag on, infer_calibration_mode
    must be UNCHANGED for every telescope in all seven gate-(D) projects --
    T20/T68/T05 stay PRECALIBRATED by default, exactly as with the flag off."""
    from astro_pipeline.calibration_policy import infer_calibration_mode

    projects = [
        REAL_SESSION_DIR, NGC3628_PROJECT_DIR, ABELL6_PROJECT_DIR, ABELL31_PROJECT_DIR,
        M42_PROJECT_DIR, IC1396_PROJECT_DIR, M31_PROJECT_DIR,
    ]
    for project_dir in projects:
        report_off = scan_session(project_dir)
        report_on = scan_session(project_dir, calibration_header_fallback=True)
        telescopes = {k[0] for k in report_off.instrument_groups()} | {
            k[0] for k in report_off.calibrated_instrument_groups()
        }
        for telescope in telescopes:
            assert infer_calibration_mode(report_off, telescope) == infer_calibration_mode(report_on, telescope), (
                f"{project_dir.name}/{telescope}: infer_calibration_mode changed when the "
                "calibration_header_fallback flag was toggled on -- Decision Q2 requires it "
                "stay unchanged, permanently."
            )
