"""Slice 4.1: run-signature mismatch invalidates exactly the expected
downstream stages.

Verified with SYNTHETIC fixtures (two contributors, then three; then one
contributor's sub silently replaced) rather than the real T21 transition,
per plan-rev4.md's own instruction: under Kaveh's colour-only rule, T21
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
# --- and pipeline.run_lrgb's `force` parameter go through -----------------


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
    original = frame_identity_hash(["raw-T24-kaveh096-M51-Red-20250101-010101-Red-BIN2-E-300-1.fit"])
    replaced = frame_identity_hash(["raw-T24-kaveh096-M51-Red-20250101-020202-Red-BIN2-E-300-1.fit"])
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
