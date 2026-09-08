"""Slice 4.4: unit tests for skill/interview.py's deduplication and
dark-scaling-preview logic -- the presentation fix for
IngestReport.missing_calibration_warnings()'s real, accepted per-user
repetition (see that function's own docstring and skill/interview.py's
module docstring for why the repetition itself is not a bug to fix)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill"))

from interview import dark_scaling_notes, dedupe_calibration_warnings, session_summary  # noqa: E402

from conftest import PROJECT_DIR, requires_project  # noqa: E402


# --- dedupe_calibration_warnings ---------------------------------------------


def test_dedupe_collapses_exact_per_user_duplicate() -> None:
    """Two users sharing a (telescope, binning, filter) group produce the
    exact same missing-Flat line today (light_groups() is keyed per user) --
    this must collapse to one."""
    raw = [
        "No Flat frames found for T24 BIN1 (needed for M51/Luminance).",
        "No Flat frames found for T24 BIN1 (needed for M51/Luminance).",
    ]
    deduped = dedupe_calibration_warnings(raw)
    assert deduped == ["T24 BIN1: no Flat frames (needed for Luminance)"]


def test_dedupe_flat_merges_across_filters_same_binning() -> None:
    raw = [
        "No Flat frames found for T24 BIN2 (needed for M51/Red).",
        "No Flat frames found for T24 BIN2 (needed for M51/Green).",
        "No Flat frames found for T24 BIN2 (needed for M51/Blue).",
    ]
    deduped = dedupe_calibration_warnings(raw)
    assert deduped == ["T24 BIN2: no Flat frames (needed for Blue, Green, Red)"]


def test_dedupe_dark_aggregates_multiple_exptimes_onto_one_line() -> None:
    """(telescope, frame_type, binning) is the collapse key -- it does NOT
    include exptime, so two missing exptimes at the same binning must
    collapse to ONE line (with both exptimes named), not two."""
    raw = [
        "No Dark frames at 300s found for T21 BIN1 (needed for M51/Luminance).",
        "No Dark frames at 600s found for T21 BIN1 (needed for M51/Luminance).",
    ]
    deduped = dedupe_calibration_warnings(raw)
    assert deduped == ["T21 BIN1: no Dark frames at 300s, 600s (needed for Luminance)"]


def test_dedupe_bias_collapses_per_user() -> None:
    raw = [
        "No Bias frames found for T21 BIN1 (needed for M51/Luminance).",
        "No Bias frames found for T21 BIN1 (needed for M51/Luminance).",
    ]
    assert dedupe_calibration_warnings(raw) == [
        "T21 BIN1: no Bias frames (needed for Luminance)"
    ]


def test_dedupe_keeps_different_telescopes_and_binnings_separate() -> None:
    raw = [
        "No Flat frames found for T24 BIN1 (needed for M51/Red).",
        "No Flat frames found for T24 BIN2 (needed for M51/Red).",
        "No Flat frames found for T21 BIN1 (needed for M51/Red).",
    ]
    deduped = dedupe_calibration_warnings(raw)
    assert len(deduped) == 3


def test_dedupe_passes_through_unrecognized_lines() -> None:
    """A future wording change to missing_calibration_warnings() must not
    crash or silently drop the line -- degrade to verbatim passthrough."""
    raw = ["Some new warning format nobody wrote a regex for yet."]
    assert dedupe_calibration_warnings(raw) == raw


# --- dark_scaling_notes -------------------------------------------------------


class _FakeFrame:
    def __init__(self, exptime: float) -> None:
        self.exptime = exptime


class _FakeReport:
    def __init__(self, groups: dict, cal_index: dict) -> None:
        self._groups = groups
        self._cal_index = cal_index

    def instrument_groups(self) -> dict:
        return self._groups

    def calibration_index(self) -> dict:
        return self._cal_index


def test_dark_scaling_notes_flags_scale_when_no_exact_exptime_dark() -> None:
    """Mirrors T21's real case: lights at 300s/600s, only a 900s dark."""
    report = _FakeReport(
        groups={("T21", "M51", "Luminance", 1): [_FakeFrame(300.0), _FakeFrame(600.0)]},
        cal_index={
            ("T21", "Bias", 1, 0.0): ["bias"],
            ("T21", "Dark", 1, 900.0): ["dark"],
        },
    )
    notes = dark_scaling_notes(report)
    assert len(notes) == 1
    assert "will scale from 900s dark" in notes[0]
    assert "300s, 600s" in notes[0]


def test_dark_scaling_notes_flags_blocked_when_no_dark_at_all() -> None:
    report = _FakeReport(
        groups={("T99", "M51", "Luminance", 1): [_FakeFrame(300.0)]},
        cal_index={("T99", "Bias", 1, 0.0): ["bias"]},
    )
    notes = dark_scaling_notes(report)
    assert len(notes) == 1
    assert notes[0].startswith("[BLOCKED]")


def test_dark_scaling_notes_silent_when_exact_dark_present() -> None:
    """Every real T24 group hits the exact-match path -- no note should be
    generated for it (nothing to warn a human about)."""
    report = _FakeReport(
        groups={("T24", "M51", "Red", 2): [_FakeFrame(300.0)]},
        cal_index={
            ("T24", "Bias", 2, 0.0): ["bias"],
            ("T24", "Dark", 2, 300.0): ["dark"],
        },
    )
    assert dark_scaling_notes(report) == []


# --- session_summary -----------------------------------------------------


def test_session_summary_reports_real_counts_not_a_template() -> None:
    class _Frame:
        def __init__(self, user: str) -> None:
            self.user = user

    report = _FakeReport(
        groups={
            ("T24", "M51", "Red", 2): [_Frame("kaveh096")],
            ("T21", "M51", "Luminance", 1): [_Frame("kaveh096")],
        },
        cal_index={},
    )
    report.lights = [_Frame("kaveh096"), _Frame("jmwill")]
    summary = session_summary(report)
    assert summary["telescopes"] == ["T21", "T24"]
    assert summary["targets"] == ["M51"]
    assert summary["binnings"] == [1, 2]
    assert summary["users"] == ["jmwill", "kaveh096"]


# --- live check against the real M51 fixture ---------------------------------


@requires_project
def test_dedupe_actually_reduces_warning_count_on_real_data() -> None:
    """The real regression this slice fixes: missing_calibration_warnings()
    really does repeat on the real M51 project (verified: 10 raw lines from
    per-user/per-filter repetition), and the deduplicated view must be
    strictly smaller."""
    from astro_pipeline.ingest import scan_session

    report = scan_session(PROJECT_DIR)
    raw = report.missing_calibration_warnings()
    deduped = dedupe_calibration_warnings(raw)
    assert len(raw) >= 10
    assert len(deduped) < len(raw)
    # Every deduped line must still be well-formed text, not an artifact of
    # a failed regex match silently passing through unparsed.
    for line in deduped:
        assert ":" in line
