"""Run-manifest: persisted stage state and interview answers for one target/session run.

Resumability requires more than "trust the JSON flag" — large FITS/TIFF
intermediates are exactly the kind of file a disk-space cleanup pass would
delete between sessions, so a stage marked complete must be re-verified
against the filesystem before it's trusted on resume (see
research/2026-07-27-tooling-research.md and the design plan's E1/E2 notes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class VerifyResult(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    SIZE_MISMATCH = "size_mismatch"
    NOT_COMPLETED = "not_completed"


class ManifestIntegrityError(RuntimeError):
    """Raised when a stage recorded as completed no longer matches disk state.

    Must surface to the user, not be silently swallowed or downgraded — a
    stale "completed" flag pointing at a missing/changed file is exactly the
    failure mode that must fail loud on resume.
    """


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OutputFile:
    path: str
    size: int

    @classmethod
    def from_path(cls, path: str | Path) -> OutputFile:
        p = Path(path)
        return cls(path=str(p), size=p.stat().st_size)

    def verify(self) -> VerifyResult:
        p = Path(self.path)
        if not p.exists():
            return VerifyResult.MISSING
        if p.stat().st_size != self.size:
            return VerifyResult.SIZE_MISMATCH
        return VerifyResult.VALID

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputFile:
        return cls(path=data["path"], size=data["size"])


@dataclass
class StageRecord:
    name: str
    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None
    completed_at: str | None = None
    output_files: list[OutputFile] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "output_files": [f.to_dict() for f in self.output_files],
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageRecord:
        return cls(
            name=data["name"],
            status=StageStatus(data["status"]),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            output_files=[OutputFile.from_dict(f) for f in data.get("output_files", [])],
            error=data.get("error"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class RunManifest:
    run_id: str
    target: str
    interview_answers: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, StageRecord] = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    # --- stage lifecycle -------------------------------------------------

    def _get_or_create(self, stage_name: str) -> StageRecord:
        if stage_name not in self.stages:
            self.stages[stage_name] = StageRecord(name=stage_name)
        return self.stages[stage_name]

    def start_stage(self, stage_name: str) -> None:
        stage = self._get_or_create(stage_name)
        stage.status = StageStatus.RUNNING
        stage.started_at = _utcnow()
        stage.completed_at = None
        stage.error = None
        self.updated_at = _utcnow()

    def complete_stage(self, stage_name: str, output_paths: list[str | Path]) -> None:
        stage = self._get_or_create(stage_name)
        stage.status = StageStatus.COMPLETED
        stage.completed_at = _utcnow()
        stage.output_files = [OutputFile.from_path(p) for p in output_paths]
        stage.error = None
        self.updated_at = _utcnow()

    def fail_stage(self, stage_name: str, error: str) -> None:
        stage = self._get_or_create(stage_name)
        stage.status = StageStatus.FAILED
        stage.completed_at = _utcnow()
        stage.error = error
        self.updated_at = _utcnow()

    # --- resumability ------------------------------------------------------

    def verify_stage(self, stage_name: str) -> VerifyResult:
        """Re-check a stage's recorded output files against disk.

        Only meaningful for a COMPLETED stage; anything else returns
        NOT_COMPLETED so callers don't mistake "never ran" for "verified ok".
        """
        stage = self.stages.get(stage_name)
        if stage is None or stage.status != StageStatus.COMPLETED:
            return VerifyResult.NOT_COMPLETED
        for output_file in stage.output_files:
            result = output_file.verify()
            if result != VerifyResult.VALID:
                return result
        return VerifyResult.VALID

    def is_stage_resumable(self, stage_name: str) -> bool:
        """True only if the stage is COMPLETED and its outputs still check out.

        Raises ManifestIntegrityError (rather than silently returning False)
        when the manifest claims completion but disk state disagrees, so a
        resumed run fails loudly at the point of divergence instead of
        continuing on stale data or crashing confusingly in a later stage.
        """
        stage = self.stages.get(stage_name)
        if stage is None or stage.status != StageStatus.COMPLETED:
            return False
        result = self.verify_stage(stage_name)
        if result == VerifyResult.VALID:
            return True
        raise ManifestIntegrityError(
            f"Stage '{stage_name}' is recorded as completed but its output "
            f"failed verification ({result.value}). Re-run this stage; do "
            f"not trust the recorded state."
        )

    # --- persistence ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "target": self.target,
            "interview_answers": self.interview_answers,
            "stages": {name: rec.to_dict() for name, rec in self.stages.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunManifest:
        return cls(
            run_id=data["run_id"],
            target=data["target"],
            interview_answers=data.get("interview_answers", {}),
            stages={
                name: StageRecord.from_dict(rec) for name, rec in data.get("stages", {}).items()
            },
            created_at=data.get("created_at", _utcnow()),
            updated_at=data.get("updated_at", _utcnow()),
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> RunManifest:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
