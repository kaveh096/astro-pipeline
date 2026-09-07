from pathlib import Path

import pytest

from astro_pipeline.ingest import scan_session
from astro_pipeline.pipeline import discover_luminance_contributors, resolve_lights

from conftest import PROJECT_DIR as REAL_SESSION_DIR

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
