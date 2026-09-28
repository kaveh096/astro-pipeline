"""Slice 4.1: run-signature mismatch invalidates exactly the expected
downstream stages.

Verified with SYNTHETIC fixtures (two contributors, then three; then one
contributor's sub silently replaced) rather than the real T21 transition,
per plan-rev4.md's own instruction: under the user's colour-only rule, T21
contributes no RGB data at all, so the real T24/T21 transition changes
zero COLOUR contributor keys and cannot exercise this path. These tests
operate directly on run_signature.py's pure comparison functions -- no
FITS files, no Siril -- mirroring this project's existing convention of
testing select_luminance_source/resolve_instrument_profile/contributor_dir
in isolation from the full calibrate/stack/solve/SPCC chain.
"""

from __future__ import annotations

from astro_pipeline.run_signature import (
    STAGE_ORDER,
    ContributorSignature,
    RunSignature,
    cascade_from,
    diff_invalidation,
    frame_identity_hash,
    load_run_signature,
    save_run_signature,
)
from astro_pipeline.calibration import CalibrationMode
from astro_pipeline.contributor_staleness import calibration_recipe_parts


def _sig(
    luminance: dict[str, tuple[int, str]],
    colour: dict[str, tuple[int, str]],
    stretch_method: str = "autostretch",
    pedestal: float = 0.1,
    luminance_selected: str = "T24_bin1",
    colour_reference: str = "T24_bin2",
) -> RunSignature:
    return RunSignature(
        stretch_method=stretch_method,
        pedestal=pedestal,
        luminance_selected=luminance_selected,
        luminance={k: ContributorSignature(key=k, stackcnt=v[0], frame_hash=v[1]) for k, v in luminance.items()},
        colour_reference=colour_reference,
        colour={k: ContributorSignature(key=k, stackcnt=v[0], frame_hash=v[1]) for k, v in colour.items()},
    )


# --- cascade_from: the dependency-order primitive both diff_invalidation ---
# --- and lrgb_orchestrator.run_lrgb's `force` parameter go through --------


def test_cascade_from_masters_includes_everything_after_it() -> None:
    assert cascade_from("masters") == {"masters", "reconciled", "final"}


def test_cascade_from_reconciled_excludes_masters() -> None:
    assert cascade_from("reconciled") == {"reconciled", "final"}


def test_cascade_from_final_is_just_final() -> None:
    assert cascade_from("final") == {"final"}


def test_stage_order_matches_run_lrgb_vocabulary() -> None:
    assert STAGE_ORDER == ("masters", "reconciled", "final")


# --- diff_invalidation: no persisted signature yet -> nothing invalidated -


def test_first_run_no_persisted_signature_invalidates_nothing() -> None:
    new = _sig({"T24_bin1": (32, "h1")}, {"T24_bin2": (78, "h2")})
    assert diff_invalidation(None, new) == set()


def test_identical_signature_invalidates_nothing() -> None:
    old = _sig({"T24_bin1": (32, "h1")}, {"T24_bin2": (78, "h2")})
    new = _sig({"T24_bin1": (32, "h1")}, {"T24_bin2": (78, "h2")})
    assert diff_invalidation(old, new) == set()


# --- the actual Slice 4.1 claim: two-then-three contributors ---------------


def test_colour_contributor_added_invalidates_reconciled_onward_not_masters() -> None:
    """Two contributors, then a third one appears (e.g. a new binning's
    RGB data becomes available) -- the Luminance side is untouched, so
    only "reconciled" and "final" should invalidate, not "masters"."""
    old = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA"), "T24_bin1": (42, "hB")},
    )
    new = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA"), "T24_bin1": (42, "hB"), "T21_bin1": (10, "hC")},
    )
    assert diff_invalidation(old, new) == {"reconciled", "final"}


def test_luminance_contributor_added_invalidates_from_masters() -> None:
    """A new Luminance contributor appears (e.g. T21's master gets built
    for the first time) -- must invalidate from "masters" onward, i.e.
    everything, even though no colour contributor changed at all."""
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")})
    new = _sig(
        {"T24_bin1": (32, "hL"), "T21_bin1": (2, "hT21")},
        {"T24_bin2": (78, "hA")},
    )
    assert diff_invalidation(old, new) == {"masters", "reconciled", "final"}


# --- the actual Slice 4.1 claim: a sub silently replaced --------------------


def test_colour_contributor_sub_replaced_without_count_changing_is_caught() -> None:
    """The exact defect usable() cannot see: STACKCNT stays the same (a
    sub was swapped, not added/removed) but frame_hash changes -- this
    must still invalidate, which plain STACKCNT/count-based comparison
    alone could never catch."""
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA-original")})
    new = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA-replaced")})
    assert diff_invalidation(old, new) == {"reconciled", "final"}


def test_luminance_sub_replaced_without_count_changing_is_caught() -> None:
    old = _sig({"T24_bin1": (32, "hL-original")}, {"T24_bin2": (78, "hA")})
    new = _sig({"T24_bin1": (32, "hL-replaced")}, {"T24_bin2": (78, "hA")})
    assert diff_invalidation(old, new) == {"masters", "reconciled", "final"}


# --- the Slice 3 STACKCNT-reference-selection nuance ------------------------


def test_reference_contributor_flip_invalidates_reconciled_even_with_identical_frames() -> None:
    """The nuance flagged by the Slice 3 implementation: the gain-fit/
    weighting reference is picked dynamically each run as whichever
    contributor has the highest STACKCNT. If a resumed run's STACKCNT
    values shift enough to flip which contributor is the reference, that
    must invalidate the reconciled combine even though NO light frame
    changed at all (same frame_hash, same stackcnt values even -- only
    WHICH one is picked as reference differs here, e.g. a quality-
    filtering change on the caller's side)."""
    old = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA"), "T24_bin1": (42, "hB")},
        colour_reference="T24_bin2",
    )
    new = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA"), "T24_bin1": (42, "hB")},
        colour_reference="T24_bin1",
    )
    assert diff_invalidation(old, new) == {"reconciled", "final"}


def test_reference_flip_together_with_stackcnt_change_still_only_reconciled_tier() -> None:
    """A STACKCNT change on a colour contributor (not a frame_hash change)
    still shows up as `old.colour != new.colour` -- also reconciled-tier,
    not masters-tier (STACKCNT differences don't imply the Luminance side
    changed)."""
    old = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA"), "T24_bin1": (40, "hB")},
        colour_reference="T24_bin2",
    )
    new = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA"), "T24_bin1": (81, "hB")},
        colour_reference="T24_bin1",
    )
    assert diff_invalidation(old, new) == {"reconciled", "final"}


# --- stretch_method: final-tier only ---------------------------------------


def test_stretch_method_change_invalidates_final_only() -> None:
    """The real, already-existing bug plan-rev4.md's Slice 4.1 names:
    stretch_method was a run_lrgb parameter but not part of any resume
    gate. Must invalidate exactly "final", not "masters"/"reconciled"."""
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, stretch_method="autostretch")
    new = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, stretch_method="ght")
    assert diff_invalidation(old, new) == {"final"}


# --- pedestal: masters-tier (affects both L and colour calibration) --------


def test_pedestal_change_invalidates_from_masters_even_with_identical_contributors() -> None:
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, pedestal=0.1)
    new = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, pedestal=0.05)
    assert diff_invalidation(old, new) == {"masters", "reconciled", "final"}


def test_contributor_stale_true_on_pedestal_change_for_any_key() -> None:
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, pedestal=0.1)
    assert old.contributor_stale("luminance", "T24_bin1", "hL", pedestal=0.05) is True
    assert old.contributor_stale("colour", "T24_bin2", "hA", pedestal=0.05) is True


def test_contributor_stale_false_when_nothing_changed() -> None:
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, pedestal=0.1)
    assert old.contributor_stale("luminance", "T24_bin1", "hL", pedestal=0.1) is False


def test_contributor_stale_true_for_a_brand_new_key() -> None:
    old = _sig({"T24_bin1": (32, "hL")}, {"T24_bin2": (78, "hA")}, pedestal=0.1)
    assert old.contributor_stale("luminance", "T21_bin1", "hNew", pedestal=0.1) is True


# --- Slice 2.3: flat_frame_hash -- both the dataclass-comparison mechanism
# AND (the actually critical part, per round 2's finding) the exact way
# lrgb_orchestrator.run_lrgb's own two real call sites invoke contributor_stale. --


def test_contributor_stale_true_on_flat_frame_hash_change_direct_call() -> None:
    """Direct check of the mechanism itself: same light frame_hash, same
    pedestal, only flat_frame_hash differs -- must be caught."""
    old = RunSignature(
        stretch_method="autostretch",
        pedestal=0.1,
        luminance_selected="T21_bin1",
        luminance={
            "T21_bin1": ContributorSignature(
                key="T21_bin1", stackcnt=2, frame_hash="hL", flat_frame_hash="flat-v1",
            )
        },
    )
    assert old.contributor_stale("luminance", "T21_bin1", "hL", pedestal=0.1, flat_frame_hash="flat-v1") is False
    assert old.contributor_stale("luminance", "T21_bin1", "hL", pedestal=0.1, flat_frame_hash="flat-v2") is True


def test_contributor_stale_defaults_flat_frame_hash_to_empty_string() -> None:
    """A caller that doesn't pass flat_frame_hash at all (e.g. an old test,
    or code that hasn't been updated) must not raise -- the parameter has
    a default, for backward compatibility -- but that default is exactly
    the trap: see the call-site-exercising test below for why a default
    alone is NOT sufficient for lrgb_orchestrator.run_lrgb's real call sites."""
    old = RunSignature(
        stretch_method="autostretch",
        pedestal=0.1,
        luminance_selected="T21_bin1",
        luminance={
            "T21_bin1": ContributorSignature(key="T21_bin1", stackcnt=2, frame_hash="hL"),
        },
    )
    assert old.contributor_stale("luminance", "T21_bin1", "hL", pedestal=0.1) is False


def test_old_format_run_signature_json_without_flat_frame_hash_still_loads(tmp_path) -> None:
    """The real, currently-persisted _pipeline/run_signature.json on the
    M51 project predates this field entirely. Write out exactly that
    shape (no "flat_frame_hash" key anywhere) and confirm from_dict() both
    loads it AND that the resulting ContributorSignature reads back
    flat_frame_hash="" -- which then correctly mismatches any real,
    freshly-computed hash on the next run, forcing exactly one rebuild (a
    one-time, correct-not-silent transition per the field's own
    docstring), not a load failure."""
    import json

    old_format = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T21_bin1",
        "luminance": {
            "T21_bin1": {
                "key": "T21_bin1",
                "stackcnt": 2,
                "frame_hash": "c2b76b333110b4a6",
                "spcc_profile": None,
                # deliberately no "flat_frame_hash" key -- matches the real,
                # currently-persisted file on disk before this slice.
            }
        },
        "colour_reference": "T24_bin2",
        "colour": {},
        "quality_filter_policy": "filter_fwhm_pct=filter_round_pct=90.0 if n>=10 else None",
    }
    path = tmp_path / "run_signature.json"
    path.write_text(json.dumps(old_format), encoding="utf-8")

    loaded = load_run_signature(path)
    assert loaded is not None
    assert loaded.luminance["T21_bin1"].flat_frame_hash == ""
    # And staleness correctly fires the first time a real flat hash exists,
    # even though the light frame_hash itself is unchanged.
    assert loaded.contributor_stale(
        "luminance", "T21_bin1", "c2b76b333110b4a6", pedestal=0.1, flat_frame_hash="real-flat-hash-abc123"
    ) is True


def test_old_format_run_signature_json_without_calibration_mode_still_loads(tmp_path) -> None:
    """calibration_mode (precalibrated-path plan, 2026-09) is purely
    diagnostic, NOT load-bearing for staleness (frame_hash already covers
    a mode switch for free, since raw- vs calibrated- provenance changes
    every light's filename). Confirms a pre-existing run_signature.json
    with no "calibration_mode" key at all still loads and reads back the
    documented default "raw_local"."""
    import json

    old_format = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T21_bin1",
        "luminance": {
            "T21_bin1": {
                "key": "T21_bin1",
                "stackcnt": 2,
                "frame_hash": "c2b76b333110b4a6",
                "spcc_profile": None,
                "flat_frame_hash": "",
                # deliberately no "calibration_mode" key.
            }
        },
        "colour_reference": "T24_bin2",
        "colour": {},
        "quality_filter_policy": "filter_fwhm_pct=filter_round_pct=90.0 if n>=10 else None",
    }
    path = tmp_path / "run_signature.json"
    path.write_text(json.dumps(old_format), encoding="utf-8")

    loaded = load_run_signature(path)
    assert loaded is not None
    assert loaded.luminance["T21_bin1"].calibration_mode == "raw_local"


# --- THE call-site-exercising test: round 2's actual finding was that
# giving contributor_stale a defaulted flat_frame_hash parameter WITHOUT
# updating lrgb_orchestrator.run_lrgb's two real (pure-positional) call sites would
# leave the whole feature silently dead -- compiling, running, and passing
# `pytest tests/ -q` cleanly, with the comparison simply never firing. This
# test exercises the REAL call sites (via a minimal run_lrgb invocation
# against synthetic fixtures), not just the dataclass comparison in
# isolation above, which would pass even with that exact bug present. -----


def test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale(
    tmp_path, monkeypatch
) -> None:
    """Exercises lrgb_orchestrator.run_lrgb's real Luminance-loop call site the same
    way it is actually invoked in production: monkeypatches
    RunSignature.contributor_stale itself to record every call's
    arguments, then drives just enough of run_lrgb (via light monkeypatching
    of the Siril/GraXpert/SPCC-touching internals, none of which this test
    needs) to reach that call site with a persisted OLD signature already
    on disk (predating flat_frame_hash, exactly like the real file), and a
    contributor whose ONLY change is its matched flat set. If lrgb_orchestrator.py's
    call site were reverted to the pre-Slice-2.3 positional call (no
    flat_frame_hash argument), this test goes red -- proving it actually
    catches the regression, not just the dataclass field in isolation.
    """
    import astro_pipeline.run_signature as run_signature_module

    calls: list[tuple] = []
    real_contributor_stale = run_signature_module.RunSignature.contributor_stale

    def spy(self, section, key, frame_hash, pedestal, *args, **kwargs):
        calls.append((section, key, frame_hash, pedestal, args, kwargs))
        return real_contributor_stale(self, section, key, frame_hash, pedestal, *args, **kwargs)

    monkeypatch.setattr(run_signature_module.RunSignature, "contributor_stale", spy)

    # A minimal persisted signature: matches the real T21_bin1 Luminance
    # entry's shape (frame_hash present, no flat_frame_hash recorded --
    # i.e. it defaults to "" on load), so contributor_stale's comparison
    # would find frame_hash UNCHANGED but flat_frame_hash mismatched (new
    # flats matched where there were none/different before) if and only if
    # the real call site actually passes the freshly-computed flat hash.
    import json
    from astro_pipeline.workspace import pipeline_dir

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    out = pipeline_dir(project_dir)
    out.mkdir(parents=True, exist_ok=True)

    import astro_pipeline.lrgb_orchestrator as lrgb_orchestrator_module

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    # Two lights, not one: a later slice added a MIN_SEQUENCE_FRAMES=2
    # skip in run_lrgb's Luminance loop (Siril refuses to form a sequence
    # from a single frame) -- a single fake light here would be silently
    # skipped before ever reaching the contributor_stale call site this
    # test exists to exercise, which is exactly what happened when this
    # test regressed: it kept passing for the wrong reason (RGB-only mode
    # raising early) until a real assertion caught the mismatch.
    lum_light = _FakeLightFrame("SAME-LIGHTS-UNCHANGED.fit")
    lum_light2 = _FakeLightFrame("SAME-LIGHTS-UNCHANGED-2.fit")
    # The persisted frame_hash must be the REAL computed hash for this
    # light set, not an arbitrary string -- otherwise the light-set
    # comparison alone would already report stale, and this test would no
    # longer isolate "only the flat set changed" as the actual cause.
    real_light_frame_hash = frame_identity_hash([lum_light.path.name, lum_light2.path.name])
    persisted = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T99_bin1",
        "luminance": {
            "T99_bin1": {
                "key": "T99_bin1",
                "stackcnt": 2,
                "frame_hash": real_light_frame_hash,
                "spcc_profile": None,
                # no flat_frame_hash key -- old format.
            }
        },
        "colour_reference": "",
        "colour": {},
        "quality_filter_policy": "x",
    }
    (out / "run_signature.json").write_text(json.dumps(persisted), encoding="utf-8")

    class _FakeFlatFrame:
        def __init__(self, path_name: str) -> None:
            self.path = tmp_path / path_name

    new_flats = [_FakeFlatFrame("new_flat_1.fit"), _FakeFlatFrame("new_flat_2.fit")]

    class _FakeReport:
        def calibration_index(self):
            return {}

        def instrument_groups(self):
            return {("T99", "M51", "Luminance", 1): [lum_light, lum_light2]}

        def calibrated_instrument_groups(self):
            return {}

        def flat_index(self):
            return {("T99", 1, "Luminance"): new_flats}

    monkeypatch.setattr(lrgb_orchestrator_module, "scan_session", lambda project_dir, **kw: _FakeReport())

    def fake_build_master(*args, **kwargs):
        # Stop run_lrgb right after the Luminance-loop call site we care
        # about has run -- no Siril/register/solve needed for this test.
        raise RuntimeError("stop-after-build-master-call-site")

    monkeypatch.setattr(lrgb_orchestrator_module, "build_group_master", fake_build_master)

    try:
        lrgb_orchestrator_module.run_lrgb(
            project_dir, telescope="T99", target="M51", ra_hours=1.0, dec_deg=1.0,
        )
    except RuntimeError as exc:
        assert "stop-after-build-master-call-site" in str(exc)

    lum_calls = [c for c in calls if c[0] == "luminance" and c[1] == "T99_bin1"]
    assert lum_calls, "contributor_stale was never called for the Luminance contributor at all"
    # The real call site's 5th positional/keyword argument must be the
    # freshly-computed flat_frame_hash, not omitted (which would leave
    # args/kwargs empty and the comparison always see flat_frame_hash="").
    section, key, frame_hash, pedestal, extra_args, extra_kwargs = lum_calls[0]
    passed_flat_hash = extra_args[0] if extra_args else extra_kwargs.get("flat_frame_hash")
    assert passed_flat_hash not in (None, ""), (
        "contributor_stale's real Luminance-loop call site did not pass a real "
        "flat_frame_hash -- this is exactly the round-2 regression: the parameter "
        "compiles and runs but the freshly-computed hash never reaches the comparison."
    )
    # And, since frame_hash is unchanged but flat_frame_hash is new, the
    # comparison itself must actually have returned True (stale).
    assert frame_hash == real_light_frame_hash  # sanity: light set really is unchanged
    assert real_contributor_stale(
        load_run_signature(out / "run_signature.json"),
        "luminance", "T99_bin1", real_light_frame_hash, 0.1, passed_flat_hash,
    ) is True


# --- luminance_selected: masters-tier ---------------------------------------


def test_luminance_selection_flip_invalidates_from_masters() -> None:
    old = _sig(
        {"T24_bin1": (32, "hL"), "T21_bin1": (2, "hT21")},
        {"T24_bin2": (78, "hA")},
        luminance_selected="T24_bin1",
    )
    new = _sig(
        {"T24_bin1": (32, "hL"), "T21_bin1": (2, "hT21")},
        {"T24_bin2": (78, "hA")},
        luminance_selected="T21_bin1",
    )
    assert diff_invalidation(old, new) == {"masters", "reconciled", "final"}


# --- frame_identity_hash ----------------------------------------------------


def test_frame_identity_hash_is_order_independent() -> None:
    a = frame_identity_hash(["b.fit", "a.fit", "c.fit"])
    b = frame_identity_hash(["c.fit", "b.fit", "a.fit"])
    assert a == b


def test_frame_identity_hash_changes_when_a_sub_is_replaced() -> None:
    original = frame_identity_hash(["raw-T24-observer1-M51-Red-20250101-010101-Red-BIN2-E-300-1.fit"])
    replaced = frame_identity_hash(["raw-T24-observer1-M51-Red-20250101-020202-Red-BIN2-E-300-1.fit"])
    assert original != replaced


def test_frame_identity_hash_stable_for_same_names() -> None:
    names = ["a.fit", "b.fit"]
    assert frame_identity_hash(names) == frame_identity_hash(list(names))


# --- round-trip persistence -------------------------------------------------


def test_save_and_load_round_trips(tmp_path) -> None:
    sig = _sig(
        {"T24_bin1": (32, "hL")},
        {"T24_bin2": (78, "hA", ), "T24_bin1": (42, "hB")},
    )
    # add an SPCC profile to one colour contributor to exercise that field too
    sig = RunSignature(
        stretch_method=sig.stretch_method,
        pedestal=sig.pedestal,
        luminance_selected=sig.luminance_selected,
        luminance=sig.luminance,
        colour_reference=sig.colour_reference,
        colour={
            **sig.colour,
            "T24_bin2": ContributorSignature(
                key="T24_bin2", stackcnt=78, frame_hash="hA",
                spcc_profile=("KAF16803", "Astrodon Red (E series)", "Astrodon Green (E series)", "Astrodon Blue (E / I series)"),
            ),
        },
    )
    path = tmp_path / "run_signature.json"
    save_run_signature(sig, path)
    loaded = load_run_signature(path)
    assert loaded == sig


def test_load_missing_file_returns_none(tmp_path) -> None:
    assert load_run_signature(tmp_path / "does_not_exist.json") is None


def test_load_corrupt_file_returns_none_not_raises(tmp_path) -> None:
    path = tmp_path / "run_signature.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    assert load_run_signature(path) is None


# --- the SECOND real call site (lrgb_orchestrator.py's colour loop) -- a
# review round's finding named both the Luminance and colour call sites
# as pure-positional call sites that each needed the fix; the test
# above exercises the Luminance one, this exercises the colour one. -------


def test_run_lrgb_colour_call_site_actually_passes_flat_frame_hash_to_contributor_stale(
    tmp_path, monkeypatch
) -> None:
    """Same shape as the Luminance-loop test above, aimed at the colour
    loop's own `old_signature.contributor_stale("colour", ...)` call site.
    A minimal Luminance contributor is allowed to build successfully
    (via a stubbed build_group_master returning a real, tiny, readable FITS
    file, so lrgb_orchestrator.py's own `fits.getheader(...).get("STACKCNT", ...)`
    call right after it doesn't blow up), so control actually reaches the
    colour loop; `_build_colour_contributor` is then stubbed to stop
    execution right after the colour call site we care about has run.
    """
    import json

    import numpy as np
    from astropy.io import fits as fits_module

    import astro_pipeline.run_signature as run_signature_module
    import astro_pipeline.lrgb_orchestrator as lrgb_orchestrator_module
    from astro_pipeline.workspace import pipeline_dir

    calls: list[tuple] = []
    real_contributor_stale = run_signature_module.RunSignature.contributor_stale

    def spy(self, section, key, frame_hash, pedestal, *args, **kwargs):
        calls.append((section, key, frame_hash, pedestal, args, kwargs))
        return real_contributor_stale(self, section, key, frame_hash, pedestal, *args, **kwargs)

    monkeypatch.setattr(run_signature_module.RunSignature, "contributor_stale", spy)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    out = pipeline_dir(project_dir)
    out.mkdir(parents=True, exist_ok=True)

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    lum_light = _FakeLightFrame("lum_unchanged.fit")

    # No RGB lights at all in this fake report -> both the persisted and
    # freshly-computed colour LIGHT frame_hash are the hash of an empty
    # name list -- identical, isolating the flat set as the only change.
    real_colour_frame_hash = frame_identity_hash([])

    class _FakeFlatFrame:
        def __init__(self, path_name: str) -> None:
            self.path = tmp_path / path_name

    colour_flats = {
        ("T99", 2, "Red"): [_FakeFlatFrame("new_red_flat.fit")],
        ("T99", 2, "Green"): [_FakeFlatFrame("new_green_flat.fit")],
        ("T99", 2, "Blue"): [_FakeFlatFrame("new_blue_flat.fit")],
    }

    class _FakeReport:
        def calibration_index(self):
            return {}

        def instrument_groups(self):
            return {("T99", "M51", "Luminance", 1): [lum_light]}

        def calibrated_instrument_groups(self):
            return {}

        def flat_index(self):
            return colour_flats

    persisted = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T99_bin1",
        "luminance": {
            "T99_bin1": {
                "key": "T99_bin1", "stackcnt": 1,
                "frame_hash": frame_identity_hash([lum_light.path.name]),
                "spcc_profile": None,
            }
        },
        "colour_reference": "T99_bin2",
        "colour": {
            "T99_bin2": {
                "key": "T99_bin2", "stackcnt": 0,
                "frame_hash": real_colour_frame_hash,
                "spcc_profile": None,
                # no flat_frame_hash key -- old format, defaults to "" on load.
            }
        },
        "quality_filter_policy": "x",
    }
    (out / "run_signature.json").write_text(json.dumps(persisted), encoding="utf-8")

    monkeypatch.setattr(lrgb_orchestrator_module, "scan_session", lambda project_dir, **kw: _FakeReport())

    # A real, tiny, readable FITS file so the Luminance loop's own
    # `fits.getheader(lum_master_path).get("STACKCNT", ...)` call (right
    # after build_group_master returns) doesn't fail -- this test's target
    # is the COLOUR call site, so the Luminance path just needs to complete.
    stub_master = tmp_path / "stub_master.fit"
    fits_module.PrimaryHDU(data=np.zeros((4, 4), dtype=np.float32)).writeto(stub_master)
    monkeypatch.setattr(lrgb_orchestrator_module, "build_group_master", lambda *a, **k: stub_master)

    def fake_build_rgb(self, *args, **kwargs):
        raise RuntimeError("stop-after-colour-call-site")

    from astro_pipeline.colour_contributor import ColourContributorBuilder

    monkeypatch.setattr(ColourContributorBuilder, "build_rgb", fake_build_rgb)

    try:
        lrgb_orchestrator_module.run_lrgb(
            project_dir, telescope="T99", target="M51", ra_hours=1.0, dec_deg=1.0,
        )
    except RuntimeError as exc:
        assert "stop-after-colour-call-site" in str(exc)

    colour_calls = [c for c in calls if c[0] == "colour" and c[1] == "T99_bin2"]
    assert colour_calls, "contributor_stale was never called for the colour contributor at all"
    section, key, frame_hash, pedestal, extra_args, extra_kwargs = colour_calls[0]
    passed_flat_hash = extra_args[0] if extra_args else extra_kwargs.get("flat_frame_hash")
    assert passed_flat_hash not in (None, ""), (
        "contributor_stale's real colour-loop call site did not pass a real "
        "flat_frame_hash -- this is exactly the round-2 regression, on the SECOND "
        "of the two named call sites (lrgb_orchestrator.py's colour loop)."
    )
    assert frame_hash == real_colour_frame_hash  # sanity: light set really is unchanged
    assert real_contributor_stale(
        load_run_signature(out / "run_signature.json"),
        "colour", "T99_bin2", real_colour_frame_hash, 0.1, passed_flat_hash,
    ) is True


# --- plan-flats-v4.md Step 2: calibration_recipe -- ContributorSignature's
# new field, contributor_stale's new parameter, and the mode-aware
# calibration_recipe_parts() helper. Mirrors this file's own existing
# flat_frame_hash test set exactly (round 4/R4-1's own instruction: this
# file's docstring already warns a keyword-default is NOT enough on its
# own for a first field -- the same applies to a second one). -------------


class _FakeFlat:
    def __init__(self, path_name: str) -> None:
        from pathlib import Path
        self.path = Path(path_name)


def test_calibration_recipe_parts_precalibrated_with_flats_is_empty() -> None:
    """PRECALIBRATED never consults flats at all (already flat-corrected
    upstream) -- ("", "") regardless of a non-empty flat set."""
    flats = [_FakeFlat("a.fit"), _FakeFlat("b.fit")]
    assert calibration_recipe_parts(CalibrationMode.PRECALIBRATED, flats) == ("", "")


def test_calibration_recipe_parts_raw_local_empty_flats_is_empty() -> None:
    assert calibration_recipe_parts(CalibrationMode.RAW_LOCAL, []) == ("", "")


def test_calibration_recipe_parts_raw_local_with_flats_returns_hash_and_bumped_recipe() -> None:
    """Step 3b's own bumped recipe (plan-flats-v4.md's single trigger for
    the real T21_bin1 rebuild): a non-empty RAW_LOCAL flat set now returns
    FLAT_RECIPE_VERSION, not "" -- Step 2 shipped this same helper
    returning "" for this exact case; Step 3b is the actual behaviour
    change that bumps it."""
    from astro_pipeline.calibration import FLAT_RECIPE_VERSION

    flats = [_FakeFlat("a.fit"), _FakeFlat("b.fit")]
    frame_hash, recipe = calibration_recipe_parts(CalibrationMode.RAW_LOCAL, flats)
    assert frame_hash == frame_identity_hash(["a.fit", "b.fit"])
    assert recipe == FLAT_RECIPE_VERSION


def test_contributor_stale_true_on_calibration_recipe_change_direct_call() -> None:
    """Direct check of the mechanism itself: same light frame_hash, same
    pedestal, same flat_frame_hash, only calibration_recipe differs --
    must be caught."""
    old = RunSignature(
        stretch_method="autostretch",
        pedestal=0.1,
        luminance_selected="T21_bin1",
        luminance={
            "T21_bin1": ContributorSignature(
                key="T21_bin1", stackcnt=2, frame_hash="hL", flat_frame_hash="flat-hash-1",
                calibration_recipe="flat:v1",
            )
        },
    )
    assert old.contributor_stale(
        "luminance", "T21_bin1", "hL", pedestal=0.1, flat_frame_hash="flat-hash-1", calibration_recipe="flat:v1",
    ) is False
    assert old.contributor_stale(
        "luminance", "T21_bin1", "hL", pedestal=0.1, flat_frame_hash="flat-hash-1", calibration_recipe="flat:v2:dedup+uniq+mul",
    ) is True


def test_contributor_stale_defaults_calibration_recipe_to_empty_string() -> None:
    """A caller that doesn't pass calibration_recipe at all (e.g. an old
    test, or code that hasn't been updated) must not raise -- the
    parameter has a default -- but see the call-site-exercising tests
    below for why a default alone is NOT sufficient for the real
    Luminance/colour loop call sites."""
    old = RunSignature(
        stretch_method="autostretch",
        pedestal=0.1,
        luminance_selected="T21_bin1",
        luminance={
            "T21_bin1": ContributorSignature(key="T21_bin1", stackcnt=2, frame_hash="hL"),
        },
    )
    assert old.contributor_stale("luminance", "T21_bin1", "hL", pedestal=0.1) is False


def test_old_format_run_signature_json_without_calibration_recipe_still_loads(tmp_path) -> None:
    """The real, currently-persisted _pipeline/run_signature.json on the
    M51 project predates this field entirely, exactly like
    flat_frame_hash's own equivalent test above. Write out exactly that
    shape (no "calibration_recipe" key anywhere) and confirm from_dict()
    both loads it AND that the resulting ContributorSignature reads back
    calibration_recipe="" -- which then correctly mismatches any real,
    freshly-computed non-empty recipe on the next run, forcing exactly
    one rebuild (Step 3b's own one-time T21_bin1 promise)."""
    import json

    old_format = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T21_bin1",
        "luminance": {
            "T21_bin1": {
                "key": "T21_bin1",
                "stackcnt": 2,
                "frame_hash": "c2b76b333110b4a6",
                "spcc_profile": None,
                "flat_frame_hash": "349469062e57762c",
                "calibration_mode": "raw_local",
                # deliberately no "calibration_recipe" key -- matches the
                # real, currently-persisted file on disk before Step 2.
            }
        },
        "colour_reference": "T24_bin2",
        "colour": {},
        "quality_filter_policy": "filter_fwhm_pct=filter_round_pct=90.0 if n>=10 else None",
    }
    path = tmp_path / "run_signature.json"
    path.write_text(json.dumps(old_format), encoding="utf-8")

    loaded = load_run_signature(path)
    assert loaded is not None
    assert loaded.luminance["T21_bin1"].calibration_recipe == ""
    assert loaded.contributor_stale(
        "luminance", "T21_bin1", "c2b76b333110b4a6", pedestal=0.1,
        flat_frame_hash="349469062e57762c", calibration_recipe="flat:v2:dedup+uniq+mul",
    ) is True


def test_calibration_recipe_round_trips_through_to_dict_from_dict(tmp_path) -> None:
    """Round-trip test (round 6, major) -- the missing half of the
    old-JSON test above: a NON-EMPTY calibration_recipe must actually
    survive to_dict()/from_dict() (and a real json.dumps/json.loads round
    trip), not just live in the in-memory object. Without this, Step 3b's
    non-empty recipe would be correctly detected as a change on the run
    right after it ships, but to_dict() would silently drop it, so the
    NEXT run's from_dict() would load it back as "" -- permanently
    mismatching the freshly-computed value forever, forcing a full
    real-Siril T21 rebuild on every run, not the one-time rebuild Step 3b
    explicitly promises."""
    import json

    sig = ContributorSignature(
        key="T21_bin1", stackcnt=2, frame_hash="hL", flat_frame_hash="hF",
        calibration_recipe="flat:v2:dedup+uniq+mul",
    )
    as_dict = sig.to_dict()
    assert as_dict["calibration_recipe"] == "flat:v2:dedup+uniq+mul"
    round_tripped = ContributorSignature.from_dict(json.loads(json.dumps(as_dict)))
    assert round_tripped.calibration_recipe == "flat:v2:dedup+uniq+mul"
    assert round_tripped == sig

    path = tmp_path / "run_signature.json"
    run_sig = RunSignature(
        stretch_method="autostretch", pedestal=0.1, luminance_selected="T21_bin1",
        luminance={"T21_bin1": sig},
    )
    save_run_signature(run_sig, path)
    loaded = load_run_signature(path)
    assert loaded.luminance["T21_bin1"].calibration_recipe == "flat:v2:dedup+uniq+mul"


def test_run_lrgb_luminance_call_site_actually_passes_calibration_recipe_to_contributor_stale(
    tmp_path, monkeypatch
) -> None:
    """Same shape/rationale as
    test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale
    above, aimed at the NEW calibration_recipe parameter specifically:
    confirms the Luminance loop's real call site passes a 6th positional
    argument (calibration_recipe), not just the 5th (flat_frame_hash) --
    a defaulted parameter alone would compile and pass every existing
    test while never actually reaching contributor_stale's comparison."""
    import astro_pipeline.run_signature as run_signature_module

    calls: list[tuple] = []
    real_contributor_stale = run_signature_module.RunSignature.contributor_stale

    def spy(self, section, key, frame_hash, pedestal, *args, **kwargs):
        calls.append((section, key, frame_hash, pedestal, args, kwargs))
        return real_contributor_stale(self, section, key, frame_hash, pedestal, *args, **kwargs)

    monkeypatch.setattr(run_signature_module.RunSignature, "contributor_stale", spy)

    import json
    from astro_pipeline.workspace import pipeline_dir

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    out = pipeline_dir(project_dir)
    out.mkdir(parents=True, exist_ok=True)

    import astro_pipeline.lrgb_orchestrator as lrgb_orchestrator_module

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    lum_light = _FakeLightFrame("SAME-LIGHTS-UNCHANGED.fit")
    lum_light2 = _FakeLightFrame("SAME-LIGHTS-UNCHANGED-2.fit")
    real_light_frame_hash = frame_identity_hash([lum_light.path.name, lum_light2.path.name])
    persisted = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T99_bin1",
        "luminance": {
            "T99_bin1": {
                "key": "T99_bin1",
                "stackcnt": 2,
                "frame_hash": real_light_frame_hash,
                "spcc_profile": None,
            }
        },
        "colour_reference": "",
        "colour": {},
        "quality_filter_policy": "x",
    }
    (out / "run_signature.json").write_text(json.dumps(persisted), encoding="utf-8")

    class _FakeReport:
        def calibration_index(self):
            return {}

        def instrument_groups(self):
            return {("T99", "M51", "Luminance", 1): [lum_light, lum_light2]}

        def calibrated_instrument_groups(self):
            return {}

        def flat_index(self):
            return {}

    monkeypatch.setattr(lrgb_orchestrator_module, "scan_session", lambda project_dir, **kw: _FakeReport())

    def fake_build_master(*args, **kwargs):
        raise RuntimeError("stop-after-build-master-call-site")

    monkeypatch.setattr(lrgb_orchestrator_module, "build_group_master", fake_build_master)

    try:
        lrgb_orchestrator_module.run_lrgb(
            project_dir, telescope="T99", target="M51", ra_hours=1.0, dec_deg=1.0,
        )
    except RuntimeError as exc:
        assert "stop-after-build-master-call-site" in str(exc)

    lum_calls = [c for c in calls if c[0] == "luminance" and c[1] == "T99_bin1"]
    assert lum_calls, "contributor_stale was never called for the Luminance contributor at all"
    section, key, frame_hash, pedestal, extra_args, extra_kwargs = lum_calls[0]
    assert len(extra_args) == 2 or "calibration_recipe" in extra_kwargs, (
        "contributor_stale's real Luminance-loop call site did not pass a "
        "calibration_recipe argument at all -- it compiles and runs (the "
        "parameter has a default) but the freshly-computed recipe never "
        "reaches the comparison, exactly the round-2-class regression "
        "flat_frame_hash's own call-site test already guards against."
    )


def test_run_lrgb_colour_call_site_actually_passes_calibration_recipe_to_contributor_stale(
    tmp_path, monkeypatch
) -> None:
    """Colour-loop counterpart of the Luminance test above."""
    import json

    import numpy as np
    from astropy.io import fits as fits_module

    import astro_pipeline.run_signature as run_signature_module
    import astro_pipeline.lrgb_orchestrator as lrgb_orchestrator_module
    from astro_pipeline.workspace import pipeline_dir

    calls: list[tuple] = []
    real_contributor_stale = run_signature_module.RunSignature.contributor_stale

    def spy(self, section, key, frame_hash, pedestal, *args, **kwargs):
        calls.append((section, key, frame_hash, pedestal, args, kwargs))
        return real_contributor_stale(self, section, key, frame_hash, pedestal, *args, **kwargs)

    monkeypatch.setattr(run_signature_module.RunSignature, "contributor_stale", spy)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    out = pipeline_dir(project_dir)
    out.mkdir(parents=True, exist_ok=True)

    class _FakeLightFrame:
        def __init__(self, path_name: str, user: str = "observer1") -> None:
            self.path = tmp_path / path_name
            self.user = user
            self.exptime = 300.0

    lum_light = _FakeLightFrame("lum_unchanged.fit")
    real_colour_frame_hash = frame_identity_hash([])

    class _FakeReport:
        def calibration_index(self):
            return {}

        def instrument_groups(self):
            return {("T99", "M51", "Luminance", 1): [lum_light]}

        def calibrated_instrument_groups(self):
            return {}

        def flat_index(self):
            return {}

    persisted = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T99_bin1",
        "luminance": {
            "T99_bin1": {
                "key": "T99_bin1", "stackcnt": 1,
                "frame_hash": frame_identity_hash([lum_light.path.name]),
                "spcc_profile": None,
            }
        },
        "colour_reference": "T99_bin2",
        "colour": {
            "T99_bin2": {
                "key": "T99_bin2", "stackcnt": 0,
                "frame_hash": real_colour_frame_hash,
                "spcc_profile": None,
            }
        },
        "quality_filter_policy": "x",
    }
    (out / "run_signature.json").write_text(json.dumps(persisted), encoding="utf-8")

    monkeypatch.setattr(lrgb_orchestrator_module, "scan_session", lambda project_dir, **kw: _FakeReport())

    stub_master = tmp_path / "stub_master.fit"
    fits_module.PrimaryHDU(data=np.zeros((4, 4), dtype=np.float32)).writeto(stub_master)
    monkeypatch.setattr(lrgb_orchestrator_module, "build_group_master", lambda *a, **k: stub_master)

    def fake_build_rgb(self, *args, **kwargs):
        raise RuntimeError("stop-after-colour-call-site")

    from astro_pipeline.colour_contributor import ColourContributorBuilder

    monkeypatch.setattr(ColourContributorBuilder, "build_rgb", fake_build_rgb)

    try:
        lrgb_orchestrator_module.run_lrgb(
            project_dir, telescope="T99", target="M51", ra_hours=1.0, dec_deg=1.0,
        )
    except RuntimeError as exc:
        assert "stop-after-colour-call-site" in str(exc)

    colour_calls = [c for c in calls if c[0] == "colour" and c[1] == "T99_bin2"]
    assert colour_calls, "contributor_stale was never called for the colour contributor at all"
    section, key, frame_hash, pedestal, extra_args, extra_kwargs = colour_calls[0]
    assert len(extra_args) == 2 or "calibration_recipe" in extra_kwargs, (
        "contributor_stale's real colour-loop call site did not pass a "
        "calibration_recipe argument at all."
    )


# --- plan-flats-v4.md Step 5: RunSignature.calibration_header_fallback --
# a top-level, purely diagnostic flag (not on ContributorSignature, where
# dict equality would cascade -- R2-12). ----------------------------------


def test_calibration_header_fallback_round_trips_through_to_dict_from_dict() -> None:
    """Mirrors Step 2's own round-trip test: a True value must actually
    survive to_dict()/from_dict(), not just live in the in-memory object."""
    import json

    sig = RunSignature(
        stretch_method="autostretch", pedestal=0.1, luminance_selected="T21_bin1",
        calibration_header_fallback=True,
    )
    as_dict = sig.to_dict()
    assert as_dict["calibration_header_fallback"] is True
    round_tripped = RunSignature.from_dict(json.loads(json.dumps(as_dict)))
    assert round_tripped.calibration_header_fallback is True
    assert round_tripped == sig


def test_old_format_run_signature_json_without_calibration_header_fallback_still_loads(tmp_path) -> None:
    """A pre-Step-5 run_signature.json (no key at all) must still load and
    read back the documented default, False."""
    import json

    old_format = {
        "stretch_method": "autostretch",
        "pedestal": 0.1,
        "luminance_selected": "T21_bin1",
        "luminance": {},
        "colour_reference": "",
        "colour": {},
        "quality_filter_policy": "x",
        # deliberately no "calibration_header_fallback" key.
    }
    path = tmp_path / "run_signature.json"
    path.write_text(json.dumps(old_format), encoding="utf-8")

    loaded = load_run_signature(path)
    assert loaded is not None
    assert loaded.calibration_header_fallback is False


def test_diff_invalidation_ignores_calibration_header_fallback_toggle_alone() -> None:
    """Toggling ONLY this flag must invalidate nothing -- it's purely
    diagnostic (see the field's own docstring)."""
    old = RunSignature(
        stretch_method="autostretch", pedestal=0.1, luminance_selected="T21_bin1",
        calibration_header_fallback=False,
    )
    new = RunSignature(
        stretch_method="autostretch", pedestal=0.1, luminance_selected="T21_bin1",
        calibration_header_fallback=True,
    )
    assert diff_invalidation(old, new) == set()
