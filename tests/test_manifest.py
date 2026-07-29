from pathlib import Path

import pytest

from astro_pipeline.manifest import (
    ManifestIntegrityError,
    RunManifest,
    StageStatus,
    VerifyResult,
)


def make_output_file(tmp_path: Path, name: str, content: bytes = b"data") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_stage_lifecycle(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    manifest.start_stage("ingest")
    assert manifest.stages["ingest"].status == StageStatus.RUNNING

    out = make_output_file(tmp_path, "master_L.fits")
    manifest.complete_stage("ingest", [out])
    assert manifest.stages["ingest"].status == StageStatus.COMPLETED
    assert manifest.stages["ingest"].output_files[0].size == out.stat().st_size


def test_fail_stage_records_error() -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    manifest.start_stage("calibration")
    manifest.fail_stage("calibration", "missing flats for session 2026-07-20")
    stage = manifest.stages["calibration"]
    assert stage.status == StageStatus.FAILED
    assert "missing flats" in stage.error


def test_round_trip_save_load(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="r1", target="M31", interview_answers={"narrowband": False})
    out = make_output_file(tmp_path, "master_L.fits")
    manifest.complete_stage("ingest", [out])

    manifest_path = tmp_path / "run.manifest.json"
    manifest.save(manifest_path)
    reloaded = RunManifest.load(manifest_path)

    assert reloaded.run_id == "r1"
    assert reloaded.target == "M31"
    assert reloaded.interview_answers == {"narrowband": False}
    assert reloaded.stages["ingest"].status == StageStatus.COMPLETED
    assert reloaded.stages["ingest"].output_files[0].path == str(out)


def test_is_stage_resumable_true_when_file_intact(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    out = make_output_file(tmp_path, "master_L.fits")
    manifest.complete_stage("ingest", [out])

    assert manifest.is_stage_resumable("ingest") is True


def test_is_stage_resumable_false_when_never_run() -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    assert manifest.is_stage_resumable("ingest") is False


def test_is_stage_resumable_raises_when_file_deleted(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    out = make_output_file(tmp_path, "master_L.fits")
    manifest.complete_stage("ingest", [out])

    out.unlink()

    with pytest.raises(ManifestIntegrityError):
        manifest.is_stage_resumable("ingest")


def test_is_stage_resumable_raises_when_file_truncated(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    out = make_output_file(tmp_path, "master_L.fits", content=b"0123456789")
    manifest.complete_stage("ingest", [out])

    # Simulate a partially-written / corrupted re-write between sessions.
    out.write_bytes(b"012")

    with pytest.raises(ManifestIntegrityError):
        manifest.is_stage_resumable("ingest")


def test_verify_stage_not_completed_is_distinct_from_missing() -> None:
    manifest = RunManifest(run_id="r1", target="M31")
    manifest.start_stage("ingest")  # running, not completed
    assert manifest.verify_stage("ingest") == VerifyResult.NOT_COMPLETED

    assert manifest.verify_stage("never_touched") == VerifyResult.NOT_COMPLETED
