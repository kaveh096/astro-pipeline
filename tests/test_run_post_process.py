"""Tests for skill/run_post_process.py's input-composite resolution
(publish-readiness Step 8) -- auto-detecting which final composite to
post-process now that a target can have more than one real shape
(plain LRGB/RGB-only, narrowband, narrowband-boost), and the
--black-point range validation.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill"))

import run_post_process  # noqa: E402
from run_post_process import _black_point, _tag_for, main, resolve_input_composite  # noqa: E402


def _write_fit(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=np.zeros((3, 4, 4), dtype=np.float32)).writeto(path, overwrite=True)


# --- _tag_for --------------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected_tag",
    [
        ("lrgb_final", None),
        ("rgb_final", None),
        ("lrgb_haboost_final", "haboost"),
        ("rgb_haboost_final", "haboost"),
        ("sho_final", "sho"),
        ("hoo_final", "hoo"),
    ],
)
def test_tag_for_every_candidate_stem(stem: str, expected_tag: str | None) -> None:
    assert _tag_for(stem) == expected_tag


# --- _black_point ------------------------------------------------------------


@pytest.mark.parametrize("value", ["0.0", "0.02", "0.5", "0.999"])
def test_black_point_accepts_valid_range(value: str) -> None:
    assert _black_point(value) == float(value)


@pytest.mark.parametrize("value", ["1.0", "1.5", "-0.01", "-1"])
def test_black_point_rejects_out_of_range(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=r"\[0, 1\)"):
        _black_point(value)


# --- resolve_input_composite -------------------------------------------------


def test_explicit_input_bypasses_autodetection(tmp_path: Path) -> None:
    final_dir = tmp_path / "final"
    explicit = tmp_path / "somewhere_else" / "custom.fit"
    _write_fit(explicit)
    # Also create real candidates that would otherwise auto-detect --
    # --input must win over them entirely, not just take priority.
    _write_fit(final_dir / "lrgb_final.fit")

    path, tag = resolve_input_composite(final_dir, str(explicit))
    assert path == explicit
    # "custom" isn't one of the 6 known stems -- _tag_for's fallback uses
    # the stem itself as the tag, rather than silently using no tag (which
    # could collide with an untagged plain-LRGB output for this target).
    assert tag == "custom"


def test_explicit_input_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_input_composite(Path("/fake/final"), "/fake/does-not-exist.fit")


def test_autodetect_no_candidates_raises_clear_error(tmp_path: Path) -> None:
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="No final composite found"):
        resolve_input_composite(final_dir, None)


def test_autodetect_single_candidate_lrgb_final_has_no_tag(tmp_path: Path) -> None:
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "lrgb_final.fit")

    path, tag = resolve_input_composite(final_dir, None)
    assert path == final_dir / "lrgb_final.fit"
    assert tag is None


def test_autodetect_single_candidate_sho_final_gets_sho_tag(tmp_path: Path) -> None:
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "sho_final.fit")

    path, tag = resolve_input_composite(final_dir, None)
    assert path == final_dir / "sho_final.fit"
    assert tag == "sho"


def test_autodetect_prefers_final_dir_over_intermediate_for_same_stem(tmp_path: Path) -> None:
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "_intermediate" / "lrgb_final.fit")
    _write_fit(final_dir / "lrgb_final.fit")

    path, tag = resolve_input_composite(final_dir, None)
    assert path == final_dir / "lrgb_final.fit"  # final/ wins, not _intermediate/


def test_autodetect_falls_back_to_intermediate_when_final_dir_missing(tmp_path: Path) -> None:
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "_intermediate" / "rgb_final.fit")

    path, tag = resolve_input_composite(final_dir, None)
    assert path == final_dir / "_intermediate" / "rgb_final.fit"


def test_autodetect_multiple_distinct_stems_raises_ambiguous_error(tmp_path: Path) -> None:
    """The real case this fixes: a target that has both a plain LRGB
    composite and a later narrowband-boost composite -- auto-detection
    must refuse to silently guess which one to post-process."""
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "lrgb_final.fit")
    _write_fit(final_dir / "lrgb_haboost_final.fit")

    with pytest.raises(RuntimeError, match="ambiguous") as exc_info:
        resolve_input_composite(final_dir, None)
    assert "--input" in str(exc_info.value)
    assert "lrgb_final" in str(exc_info.value)
    assert "lrgb_haboost_final" in str(exc_info.value)


# --- main(): output naming and starless resume cache are tag-aware ---------


def test_main_uses_untagged_output_names_for_plain_lrgb_final(tmp_path: Path, monkeypatch) -> None:
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "lrgb_final.fit")

    monkeypatch.setattr(run_post_process.preflight, "check_prerequisites", lambda **kwargs: [])

    captured = {}

    def fake_denoise(denoise_input, output_stem, **kwargs):
        captured["output_stem"] = output_stem
        out = tmp_path / f"{output_stem}.fit"
        _write_fit(out)
        return out

    def fake_export_with_black_point(fits_path, black_point, output_dir, stem):
        class _R:
            tiff_path = Path(output_dir) / f"{stem}.tif"
            clipped_low_fraction = 0.0
            clipped_high_fraction = 0.0

        return _R()

    monkeypatch.setattr(run_post_process, "run_graxpert_denoise", fake_denoise)
    monkeypatch.setattr(run_post_process, "export_with_black_point", fake_export_with_black_point)

    main([str(final_dir), "--target-name", "M51", "--black-point", "0.02"])

    assert captured["output_stem"] == "M51_denoised"  # no tag inserted


def test_main_inserts_tag_for_haboost_input(tmp_path: Path, monkeypatch) -> None:
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "lrgb_haboost_final.fit")

    monkeypatch.setattr(run_post_process.preflight, "check_prerequisites", lambda **kwargs: [])

    captured = {}

    def fake_denoise(denoise_input, output_stem, **kwargs):
        captured["output_stem"] = output_stem
        out = tmp_path / f"{output_stem}.fit"
        _write_fit(out)
        return out

    def fake_export_with_black_point(fits_path, black_point, output_dir, stem):
        class _R:
            tiff_path = Path(output_dir) / f"{stem}.tif"
            clipped_low_fraction = 0.0
            clipped_high_fraction = 0.0

        return _R()

    monkeypatch.setattr(run_post_process, "run_graxpert_denoise", fake_denoise)
    monkeypatch.setattr(run_post_process, "export_with_black_point", fake_export_with_black_point)

    main([str(final_dir), "--target-name", "M51", "--black-point", "0.02"])

    assert captured["output_stem"] == "M51_haboost_denoised"


def test_main_starless_resume_cache_is_keyed_by_tag(tmp_path: Path, monkeypatch) -> None:
    """Real bug this fixes: the starless resume cache was keyed only by
    target_name, e.g. "M51_starless.fit" -- switching --input from a
    plain LRGB composite to a narrowband-boost one for the same target
    would silently reuse the WRONG starless image (built from different
    source data). Now keyed by target_name + tag, so they can't collide."""
    final_dir = tmp_path / "final"
    _write_fit(final_dir / "lrgb_haboost_final.fit")

    monkeypatch.setattr(run_post_process.preflight, "check_prerequisites", lambda **kwargs: [])

    def fake_run_star_removal(composite, output_dir, output_stem, **kwargs):
        starless = Path(output_dir) / f"{output_stem}_starless.fit"
        stars = Path(output_dir) / f"{output_stem}_stars.fit"
        _write_fit(starless)
        _write_fit(stars)

        class _R:
            starless_path = starless
            stars_path = stars

        return _R()

    def fake_export(fits_path, output_dir, stem):
        class _R:
            tiff_path = Path(output_dir) / f"{stem}.tif"

        return _R()

    def fake_denoise(denoise_input, output_stem, **kwargs):
        out = tmp_path / f"{output_stem}.fit"
        _write_fit(out)
        return out

    def fake_export_with_black_point(fits_path, black_point, output_dir, stem):
        class _R:
            tiff_path = Path(output_dir) / f"{stem}.tif"
            clipped_low_fraction = 0.0
            clipped_high_fraction = 0.0

        return _R()

    monkeypatch.setattr(run_post_process, "run_star_removal", fake_run_star_removal)
    monkeypatch.setattr(run_post_process, "export", fake_export)
    monkeypatch.setattr(run_post_process, "run_graxpert_denoise", fake_denoise)
    monkeypatch.setattr(run_post_process, "export_with_black_point", fake_export_with_black_point)

    main([str(final_dir), "--target-name", "M51", "--nebula", "--black-point", "0.02"])

    # Keyed by "M51_haboost", not just "M51" -- a plain LRGB run's own
    # "M51_starless.fit" (if it existed) would be a DIFFERENT file.
    assert (final_dir / "_intermediate" / "M51_haboost_starless.fit").exists()
    assert not (final_dir / "_intermediate" / "M51_starless.fit").exists()
