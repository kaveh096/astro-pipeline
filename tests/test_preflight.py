"""Tests for preflight.py's check_prerequisites().

All mocked -- these exercise the check logic itself, not any real
installed tool or real project data.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from astro_pipeline.color_calibration import UnknownInstrumentError
from astro_pipeline.preflight import MIN_SIRIL_VERSION, check_prerequisites


class _FakeReport:
    def __init__(self, groups: dict) -> None:
        self._groups = groups

    def instrument_groups(self):
        return self._groups

    def calibrated_instrument_groups(self):
        return {}


def test_no_checks_requested_returns_no_problems():
    assert check_prerequisites() == []


def test_missing_siril_is_reported():
    with patch("astro_pipeline.preflight.find_siril_cli", side_effect=FileNotFoundError("siril-cli.exe not found")):
        problems = check_prerequisites(needs_siril=True)
    assert len(problems) == 1
    assert "siril-cli.exe" in problems[0]


def test_old_siril_version_is_reported():
    with (
        patch("astro_pipeline.preflight.find_siril_cli", return_value=Path("fake-siril.exe")),
        patch("astro_pipeline.preflight.get_version", return_value=(1, 4, 3)),
    ):
        problems = check_prerequisites(needs_siril=True)
    assert len(problems) == 1
    assert "1.4.3" in problems[0]
    assert "1.4.4" in problems[0]


def test_new_enough_siril_version_passes():
    with (
        patch("astro_pipeline.preflight.find_siril_cli", return_value=Path("fake-siril.exe")),
        patch("astro_pipeline.preflight.get_version", return_value=MIN_SIRIL_VERSION),
    ):
        assert check_prerequisites(needs_siril=True) == []


def test_siril_version_check_failure_is_reported_not_raised():
    with (
        patch("astro_pipeline.preflight.find_siril_cli", return_value=Path("fake-siril.exe")),
        patch("astro_pipeline.preflight.get_version", side_effect=TimeoutError("hung")),
    ):
        problems = check_prerequisites(needs_siril=True)
    assert len(problems) == 1
    assert "version" in problems[0].lower()


def test_missing_astap_is_reported():
    with patch("astro_pipeline.preflight.find_astap_cli", side_effect=FileNotFoundError("astap_cli.exe not found")):
        problems = check_prerequisites(needs_astap=True)
    assert len(problems) == 1
    assert "astap_cli.exe" in problems[0]


def test_missing_graxpert_uses_finder_when_no_explicit_path():
    with patch("astro_pipeline.preflight.find_graxpert", side_effect=FileNotFoundError("GraXpert.exe not found")):
        problems = check_prerequisites(needs_graxpert=True)
    assert len(problems) == 1


def test_explicit_graxpert_exe_that_exists_skips_the_finder(tmp_path):
    exe = tmp_path / "GraXpert.exe"
    exe.write_text("fake")
    with patch("astro_pipeline.preflight.find_graxpert", side_effect=AssertionError("should not be called")):
        problems = check_prerequisites(needs_graxpert=True, graxpert_exe=exe)
    assert problems == []


def test_explicit_graxpert_exe_that_does_not_exist_is_reported(tmp_path):
    missing = tmp_path / "does-not-exist.exe"
    problems = check_prerequisites(needs_graxpert=True, graxpert_exe=missing)
    assert len(problems) == 1
    assert str(missing) in problems[0]


def test_missing_starnet_is_reported():
    with patch("astro_pipeline.preflight.find_starnet", side_effect=FileNotFoundError("starnet2.exe not found")):
        problems = check_prerequisites(needs_starnet=True)
    assert len(problems) == 1


def test_spcc_profile_missing_for_mono_rgb_telescope_is_reported():
    groups = {
        ("T99", "Target", "Red", 2): [],
        ("T99", "Target", "Green", 2): [],
        ("T99", "Target", "Blue", 2): [],
    }
    with patch("astro_pipeline.preflight.classify_tree", return_value=_FakeReport(groups)):
        problems = check_prerequisites(
            project_dir="fake", telescope="T99", target="Target", needs_spcc=True,
        )
    assert len(problems) == 1
    assert "T99" in problems[0]


def test_spcc_profile_registered_for_mono_rgb_telescope_passes():
    groups = {
        ("T24", "Target", "Red", 2): [],
        ("T24", "Target", "Green", 2): [],
        ("T24", "Target", "Blue", 2): [],
    }
    with patch("astro_pipeline.preflight.classify_tree", return_value=_FakeReport(groups)):
        problems = check_prerequisites(
            project_dir="fake", telescope="T24", target="Target", needs_spcc=True,
        )
    assert problems == []


def test_spcc_profile_check_is_primary_telescope_only_not_every_telescope_seen():
    """The real M51 shape: T21 (Luminance-only, no SPCC profile at all)
    must never block a run whose primary telescope is T24 -- colour
    discovery/SPCC only ever runs for the primary telescope."""
    groups = {
        ("T21", "M51", "Luminance", 1): [],
        ("T24", "M51", "Red", 2): [],
        ("T24", "M51", "Green", 2): [],
        ("T24", "M51", "Blue", 2): [],
    }
    with patch("astro_pipeline.preflight.classify_tree", return_value=_FakeReport(groups)):
        problems = check_prerequisites(
            project_dir="fake", telescope="T24", target="M51", needs_spcc=True,
        )
    assert problems == []


def test_spcc_profile_check_skips_telescope_with_no_recognized_data_yet():
    with patch("astro_pipeline.preflight.classify_tree", return_value=_FakeReport({})):
        problems = check_prerequisites(
            project_dir="fake", telescope="T24", target="M51", needs_spcc=True,
        )
    assert problems == []


def test_spcc_profile_missing_for_osc_telescope_is_reported():
    groups = {("T68", "Target", "Color", 1): []}
    with patch("astro_pipeline.preflight.classify_tree", return_value=_FakeReport(groups)):
        problems = check_prerequisites(
            project_dir="fake", telescope="T68", target="Target", needs_spcc=True,
        )
    assert len(problems) == 1
    assert "OSC" in problems[0] or "Color" in problems[0]


def test_spcc_profile_registered_for_osc_telescope_passes():
    groups = {("T02", "Target", "Color", 1): []}
    with patch("astro_pipeline.preflight.classify_tree", return_value=_FakeReport(groups)):
        problems = check_prerequisites(
            project_dir="fake", telescope="T02", target="Target", needs_spcc=True,
        )
    assert problems == []


def test_multiple_problems_are_all_collected_not_just_the_first():
    with (
        patch("astro_pipeline.preflight.find_siril_cli", side_effect=FileNotFoundError("siril-cli.exe not found")),
        patch("astro_pipeline.preflight.find_astap_cli", side_effect=FileNotFoundError("astap_cli.exe not found")),
    ):
        problems = check_prerequisites(needs_siril=True, needs_astap=True)
    assert len(problems) == 2
