import json
import zipfile
from pathlib import Path

import pytest

from astro_pipeline.index import build_index

REAL_TREE_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025")
requires_real_tree = pytest.mark.skipif(
    not REAL_TREE_DIR.exists(), reason="Real multi-telescope sample tree not present on this machine"
)


def make_zip(tmp_path: Path, zip_name: str, inner_name: str) -> Path:
    zip_path = tmp_path / zip_name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(inner_name, b"fake fits bytes")
    return zip_path


def test_build_index_peeks_inside_zip_without_extracting(tmp_path: Path) -> None:
    make_zip(
        tmp_path,
        "raw-T21-kaveh096-M51-20250113-045216-Luminance-BIN1-E-600-001.fit.zip",
        "raw-T21-kaveh096-M51-20250113-045216-Luminance-BIN1-E-600-001.fit",
    )

    idx = build_index(tmp_path)

    assert len(idx.lights) == 0  # never extracted, so not a real on-disk light
    assert len(idx.archived_entries) == 1
    entry = idx.archived_entries[0]
    assert entry.kind == "light"
    assert entry.fields["telescope"] == "T21"
    assert entry.fields["provenance"] == "raw"
    # Original zip is untouched on disk.
    assert zipfile.is_zipfile(entry.zip_path)


def test_build_index_catches_unopenable_zip_as_other_file(tmp_path: Path) -> None:
    bad_zip = tmp_path / "corrupt.zip"
    bad_zip.write_bytes(b"not actually a zip")

    idx = build_index(tmp_path)

    assert len(idx.other_files) == 1
    assert "could not open zip" in idx.other_files[0].note.lower()


def test_build_index_catalogs_unmatched_files_as_other(tmp_path: Path) -> None:
    (tmp_path / "MasterOffset_ISO0.tif").write_bytes(b"x" * 100)
    (tmp_path / "notes.txt").write_text("some observing notes")

    idx = build_index(tmp_path)

    other_names = {f.path.name for f in idx.other_files}
    assert "MasterOffset_ISO0.tif" in other_names
    assert "notes.txt" in other_names


def test_build_index_classifies_jpeg_preview_via_light_pattern(tmp_path: Path) -> None:
    (tmp_path / "jpeg-T21-kaveh096-M51-20250113-045216-Luminance-BIN1-E-600-001.jpg").write_bytes(b"")

    idx = build_index(tmp_path)

    assert len(idx.lights) == 1
    assert idx.lights[0].provenance == "jpeg"


def test_tree_index_to_dict_is_json_serializable(tmp_path: Path) -> None:
    (tmp_path / "T24-kaveh096-Bias-000-LD20250203-LT171434-BIN1.fit").write_bytes(b"")
    idx = build_index(tmp_path)

    # Must not raise -- Path objects need to be stringified.
    text = json.dumps(idx.to_dict())
    assert "Bias" in text


def test_tree_index_save_writes_readable_json(tmp_path: Path) -> None:
    (tmp_path / "T24-kaveh096-Bias-000-LD20250203-LT171434-BIN1.fit").write_bytes(b"")
    idx = build_index(tmp_path)

    out_path = tmp_path / "index.json"
    idx.save(out_path)

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(data["calibration"]) == 1
    assert "summary" in data


# --- integration test against the real multi-telescope tree ----------------


@requires_real_tree
def test_build_index_on_real_tree() -> None:
    idx = build_index(REAL_TREE_DIR)

    summary = idx.summary()

    # T24 raw lights are loose files, directly visible.
    assert summary["lights_by_provenance_telescope"]["raw/T24"] == 95
    # T21 lights are all zip-wrapped in this delivery -- correctly absent
    # from the direct lights list, present as archived_entries instead.
    assert "raw/T21" not in summary["lights_by_provenance_telescope"]
    assert summary["archived_entries_by_kind"]["light"] > 0

    # T21 has real bias/dark/flat (camera-model and skyflat conventions).
    assert summary["calibration_by_telescope_type"]["T21/Bias"] > 0
    assert summary["calibration_by_telescope_type"]["T21/Dark"] > 0
    assert summary["calibration_by_telescope_type"]["T21/Flat"] > 0

    # The unidentified "Master_Flat <Filter> ..." files (4th, unrecognized
    # instrument/source -- doesn't match T24 or T21 conventions) must be
    # flagged, not silently dropped or guessed into either telescope's pool.
    assert len(idx.unrecognized) == 11
    assert all("master_flat" in f.path.name.lower() for f in idx.unrecognized)

    # Nothing under root is silently skipped: every file is accounted for.
    # FITS files go to lights/calibration/unrecognized; zip files are
    # accounted for via archived_entries (their contents, not the zip path
    # itself, since a zip normally holds exactly one thing worth cataloging);
    # everything else goes to lights (previews) or other_files.
    all_fs_paths = {p for p in REAL_TREE_DIR.rglob("*") if p.is_file()}
    accounted_paths = (
        {f.path for f in idx.lights}
        | {f.path for f in idx.calibration}
        | {f.path for f in idx.unrecognized}
        | {f.path for f in idx.other_files}
        | {e.zip_path for e in idx.archived_entries}
    )
    assert accounted_paths <= all_fs_paths
    assert len(all_fs_paths - accounted_paths) == 0
