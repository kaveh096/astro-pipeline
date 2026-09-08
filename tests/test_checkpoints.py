from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.checkpoints import (
    FrameStats,
    autostretch_for_display,
    checkpoint,
    frame_stats,
    save_checkpoints,
)
from conftest import LRGB_FINAL, LUM_BG, requires


def write_fits(path: Path, data: np.ndarray) -> Path:
    header = fits.Header()
    header["ROWORDER"] = "TOP-DOWN"
    fits.writeto(path, data.astype(np.float32), header=header, overwrite=True)
    return path


def star_field(shape=(256, 256), background=0.1, noise=0.001, n_stars=25) -> np.ndarray:
    rng = np.random.default_rng(0)
    data = rng.normal(background, noise, size=shape).astype(np.float32)
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]]
    for _ in range(n_stars):
        cy, cx = rng.integers(20, shape[0] - 20), rng.integers(20, shape[1] - 20)
        data += 0.5 * np.exp(-(((ys - cy) ** 2 + (xs - cx) ** 2) / (2 * 2.0**2)))
    return data


# --- display stretch -------------------------------------------------------


def test_autostretch_lifts_background_toward_target() -> None:
    """Linear data sits near black; the display stretch must lift the
    background to roughly the target, or previews of pre-stretch stages
    are unreadable."""
    data = star_field()
    out = autostretch_for_display(data, target_background=0.25)
    assert 0.15 < float(np.median(out)) < 0.40


def test_autostretch_preserves_ordering() -> None:
    """A display stretch may be non-linear but must stay monotonic --
    otherwise it would invent structure that is not in the data."""
    data = np.linspace(0.0, 1.0, 500, dtype=np.float32)
    out = autostretch_for_display(data)
    assert np.all(np.diff(out) >= -1e-6)


def test_autostretch_handles_all_nan_without_raising() -> None:
    out = autostretch_for_display(np.full((16, 16), np.nan, dtype=np.float32))
    assert np.all(out == 0)


# --- statistics ------------------------------------------------------------


def test_frame_stats_measures_background_and_noise(tmp_path: Path) -> None:
    path = write_fits(tmp_path / "field.fit", star_field(background=0.1, noise=0.002))
    stats = frame_stats(path)
    assert stats.background == pytest.approx(0.1, abs=0.02)
    assert stats.noise == pytest.approx(0.002, rel=0.5)
    assert stats.channels == 1


def test_frame_stats_detects_stars(tmp_path: Path) -> None:
    path = write_fits(tmp_path / "stars.fit", star_field(n_stars=25))
    stats = frame_stats(path)
    assert stats.star_count is not None and stats.star_count > 5


def test_frame_stats_reports_nan_and_zero_fractions(tmp_path: Path) -> None:
    data = star_field()
    data[:64] = np.nan
    data[64:128] = 0.0
    path = write_fits(tmp_path / "mixed.fit", data)
    stats = frame_stats(path)
    assert stats.nan_fraction == pytest.approx(0.25, abs=0.01)
    assert stats.zero_fraction == pytest.approx(0.25, abs=0.01)


def test_frame_stats_handles_colour_cube(tmp_path: Path) -> None:
    data = np.stack([star_field() for _ in range(3)])
    path = write_fits(tmp_path / "rgb.fit", data)
    stats = frame_stats(path)
    assert stats.channels == 3
    assert len(stats.shape) == 3


# --- warnings: the failures this project actually hit ----------------------


def test_checkpoint_warns_on_all_nan(tmp_path: Path) -> None:
    """The exact shape of the silent GraXpert corruption."""
    path = write_fits(tmp_path / "nan.fit", np.full((64, 64), np.nan, dtype=np.float32))
    result = checkpoint(path, "nan", output_dir=tmp_path, detect_stars=False)
    assert any("nan" in w.lower() for w in result.warnings)


def test_checkpoint_warns_on_mostly_zero(tmp_path: Path) -> None:
    """The exact shape of the stack-clipped background that caused it."""
    data = np.zeros((64, 64), dtype=np.float32)
    data[0, 0] = 1.0
    path = write_fits(tmp_path / "zeros.fit", data)
    result = checkpoint(path, "zeros", output_dir=tmp_path, detect_stars=False)
    assert any("zero" in w.lower() for w in result.warnings)


def test_checkpoint_warns_on_over_stretch(tmp_path: Path) -> None:
    """The exact shape of the blown-out GHT default."""
    path = write_fits(tmp_path / "hot.fit", np.full((64, 64), 1.0, dtype=np.float32))
    result = checkpoint(path, "hot", output_dir=tmp_path, detect_stars=False)
    assert any("stretch" in w.lower() or "full scale" in w.lower() for w in result.warnings)


# --- checkpoint mechanics ---------------------------------------------------


def test_checkpoint_writes_preview_and_labels_display_stretch(tmp_path: Path) -> None:
    path = write_fits(tmp_path / "linear.fit", star_field())
    result = checkpoint(path, "linear", output_dir=tmp_path)

    assert result.preview_mode == "autostretch"
    assert result.preview_path is not None and Path(result.preview_path).exists()
    # The summary must disclose that the preview was stretched for display,
    # or a viewer will judge linear data through a flattering lie.
    assert "display stretch" in result.summary()


def test_checkpoint_faithful_mode_is_not_labelled_as_stretched(tmp_path: Path) -> None:
    path = write_fits(tmp_path / "stretched.fit", star_field(background=0.3))
    result = checkpoint(path, "stretched", output_dir=tmp_path, linear=False)
    assert result.preview_mode == "faithful"
    assert "display stretch" not in result.summary()


def test_checkpoint_does_not_modify_the_source_file(tmp_path: Path) -> None:
    """Previews are display-only; the pipeline's data must be untouched."""
    path = write_fits(tmp_path / "src.fit", star_field())
    before = fits.getdata(path, memmap=False).copy()
    checkpoint(path, "src", output_dir=tmp_path)
    after = fits.getdata(path, memmap=False)
    assert np.array_equal(before, after)


def test_checkpoint_comparison_reports_movement(tmp_path: Path) -> None:
    first = frame_stats(write_fits(tmp_path / "a.fit", star_field(background=0.10)))
    path_b = write_fits(tmp_path / "b.fit", star_field(background=0.20))
    result = checkpoint(path_b, "b", output_dir=tmp_path, previous=first, detect_stars=False)
    assert "background" in result.comparison


def test_save_checkpoints_round_trips(tmp_path: Path) -> None:
    import json

    path = write_fits(tmp_path / "c.fit", star_field())
    result = checkpoint(path, "c", output_dir=tmp_path, detect_stars=False)
    out = tmp_path / "checkpoints.json"
    save_checkpoints([result], out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded[0]["label"] == "c"
    assert "stats" in loaded[0]


# --- Slice 4.2: merge-by-label, not truncate-or-append ----------------------


def test_save_checkpoints_merges_new_stage_without_losing_earlier_ones(tmp_path: Path) -> None:
    """Checkpoint emission moved inline (Slice 4.2) means save_checkpoints
    gets called once per stage, potentially across separate run_lrgb
    calls. A second call for a DIFFERENT label must not truncate the
    first label's entry -- the actual defect a plain
    `path.write_text(json.dumps(...))` truncating write has."""
    import json

    path_a = write_fits(tmp_path / "a.fit", star_field())
    path_b = write_fits(tmp_path / "b.fit", star_field(background=0.2))
    out = tmp_path / "checkpoints.json"

    save_checkpoints([checkpoint(path_a, "01_a", output_dir=tmp_path, detect_stars=False)], out)
    save_checkpoints([checkpoint(path_b, "02_b", output_dir=tmp_path, detect_stars=False)], out)

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert {d["label"] for d in loaded} == {"01_a", "02_b"}


def test_save_checkpoints_replaces_same_label_not_appends_duplicate(tmp_path: Path) -> None:
    """A stage re-checkpointed after a force-recompute (Slice 4.3) must
    REPLACE its own prior entry, not accumulate a second one under the
    same label."""
    import json

    path = write_fits(tmp_path / "a.fit", star_field())
    out = tmp_path / "checkpoints.json"

    save_checkpoints([checkpoint(path, "01_a", output_dir=tmp_path, detect_stars=False)], out)
    save_checkpoints([checkpoint(path, "01_a", output_dir=tmp_path, detect_stars=False)], out)

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert len(loaded) == 1
    assert loaded[0]["label"] == "01_a"


def test_save_checkpoints_orders_by_label_not_append_order(tmp_path: Path) -> None:
    """Ordering must read coherently even after an out-of-order re-run
    (e.g. only stage 01 got force-recomputed after stage 02 already
    existed) -- so the merged file is sorted by label, not by call order."""
    import json

    path = write_fits(tmp_path / "a.fit", star_field())
    out = tmp_path / "checkpoints.json"

    save_checkpoints([checkpoint(path, "02_b", output_dir=tmp_path, detect_stars=False)], out)
    save_checkpoints([checkpoint(path, "01_a", output_dir=tmp_path, detect_stars=False)], out)

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert [d["label"] for d in loaded] == ["01_a", "02_b"]


def test_save_checkpoints_prunes_orphaned_preview_png(tmp_path: Path) -> None:
    """The actual, already-real defect on disk: a preview PNG whose label
    no longer appears in checkpoints.json (from an earlier, now-changed
    label scheme) must be deleted the next time save_checkpoints runs --
    not left behind forever."""
    path = write_fits(tmp_path / "a.fit", star_field())
    out = tmp_path / "checkpoints.json"

    cp = checkpoint(path, "01_a", output_dir=tmp_path, detect_stars=False)
    save_checkpoints([cp], out)
    current_preview = Path(cp.preview_path)
    assert current_preview.exists()

    # Simulate an orphan: a preview PNG from a label scheme that no longer
    # exists in the current checkpoint list (mirrors the 7 real orphans
    # found under the M51 project's _pipeline/checkpoints/).
    orphan = tmp_path / "checkpoint_00_old_scheme_preview.png"
    orphan.write_bytes(current_preview.read_bytes())
    assert orphan.exists()

    # Re-saving the SAME (still-current) checkpoint list must prune the
    # orphan but keep the still-referenced preview.
    save_checkpoints([cp], out)
    assert not orphan.exists()
    assert current_preview.exists()


def test_save_checkpoints_prunes_preview_of_a_label_that_no_longer_appears(tmp_path: Path) -> None:
    """When a label is dropped from the merged set entirely (not just
    replaced), its own preview must be pruned too."""
    path = write_fits(tmp_path / "a.fit", star_field())
    out = tmp_path / "checkpoints.json"

    cp_old = checkpoint(path, "05_old_final", output_dir=tmp_path, detect_stars=False)
    save_checkpoints([cp_old], out)
    old_preview = Path(cp_old.preview_path)
    assert old_preview.exists()

    # A fresh save that no longer includes "05_old_final" at all -- but
    # save_checkpoints merges against what's on disk, so simulate a
    # rewritten checkpoints.json (as if a prior label scheme changed) by
    # writing a merged set directly, then let save_checkpoints prune.
    import json

    out.write_text(
        json.dumps([{**cp_old.to_dict(), "label": "05_new_final"}], indent=2),
        encoding="utf-8",
    )
    cp_new = checkpoint(path, "05_new_final", output_dir=tmp_path, detect_stars=False)
    save_checkpoints([cp_new], out)

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert {d["label"] for d in loaded} == {"05_new_final"}
    assert not old_preview.exists()


def test_save_checkpoints_handles_corrupt_existing_file(tmp_path: Path) -> None:
    """A corrupt checkpoints.json degrades to 'nothing persisted yet'
    rather than raising -- this project's existing usable()/checkpoint()
    convention of degrading rather than failing on a bad-but-optional
    input."""
    path = write_fits(tmp_path / "a.fit", star_field())
    out = tmp_path / "checkpoints.json"
    out.write_text("{not valid json", encoding="utf-8")

    cp = checkpoint(path, "01_a", output_dir=tmp_path, detect_stars=False)
    save_checkpoints([cp], out)  # must not raise

    import json

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded[0]["label"] == "01_a"


# --- against the real pipeline output --------------------------------------


@requires(LUM_BG)
def test_checkpoint_on_real_linear_master(tmp_path: Path) -> None:
    # output_dir=tmp_path, NOT the real project's _pipeline/final -- an
    # earlier version wrote test previews directly into Kaveh's live project
    # folder (output_dir=Path(LUM_BG).parent), which meant every test run
    # left checkpoint_real_*.png files mixed in with actual pipeline output.
    result = checkpoint(LUM_BG, "real_lum", output_dir=tmp_path, linear=True)
    assert result.stats.nan_fraction < 0.05
    assert result.stats.noise > 0
    assert result.stats.pixel_scale_arcsec is not None  # plate-solved
    assert not any("nan" in w.lower() for w in result.warnings)


@requires(LRGB_FINAL)
def test_checkpoint_on_real_final_composite(tmp_path: Path) -> None:
    result = checkpoint(LRGB_FINAL, "real_final", output_dir=tmp_path, linear=False)
    assert result.stats.channels == 3
    # The delivered result must not trip the over-stretch warning that the
    # original GHT default would have.
    assert not any("stretch" in w.lower() for w in result.warnings)


def test_comparison_suppressed_across_stretch_boundary(tmp_path: Path) -> None:
    """Comparing a stretched frame to a linear one produced confident
    nonsense in an earlier version ("noise +167759%"). It must now say the
    two are not comparable instead."""
    linear_stats = frame_stats(write_fits(tmp_path / "lin.fit", star_field(background=0.08)))
    stretched = write_fits(tmp_path / "str.fit", star_field(background=0.30, noise=0.05))

    result = checkpoint(
        stretched, "str", output_dir=tmp_path, linear=False,
        previous=linear_stats, previous_linear=True, detect_stars=False,
    )
    assert "background" not in result.comparison
    assert "not comparable" in " ".join(result.comparison.values())


def test_comparison_suppressed_across_shape_change(tmp_path: Path) -> None:
    """Star counts across a resolution change are not like-for-like."""
    small = frame_stats(write_fits(tmp_path / "small.fit", star_field(shape=(128, 128))))
    big = write_fits(tmp_path / "big.fit", star_field(shape=(256, 256)))

    result = checkpoint(
        big, "big", output_dir=tmp_path, linear=True,
        previous=small, previous_linear=True, detect_stars=False,
    )
    assert set(result.comparison) == {"shape"}
