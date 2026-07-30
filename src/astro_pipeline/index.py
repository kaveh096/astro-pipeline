"""Stage 1 (whole-tree): build a persisted index of an entire iTelescope
delivery folder -- not just the tidy single-telescope case ingest.scan_session
handles, but real messy deliveries: multiple telescopes, multiple filename
conventions, zip-wrapped exposures, pre-built master calibration files,
JPEG previews, and whatever else is sitting in the tree.

Nothing is silently dropped. Every file under root ends up in exactly one
of: lights, calibration, archived_entries (peeked inside a zip without
extracting), unrecognized, or other_files. The output is meant to be read
by a human (Kaveh) as much as by later pipeline stages -- it's the
"playlist" file, not just internal state.
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ingest import (
    CalibrationFrame,
    LightFrame,
    UnrecognizedFrame,
    _infer_telescope_from_path,
    classify_filename,
    classify_frame,
)

FITS_EXTENSIONS = (".fit", ".fits", ".fts")
ARCHIVE_EXTENSIONS = (".zip",)


@dataclass(frozen=True)
class ArchivedEntry:
    """A light or calibration frame identified inside a zip by name alone,
    without extracting it."""

    zip_path: Path
    inner_name: str
    kind: str  # "light" | "calibration" | "unrecognized"
    fields: dict


@dataclass(frozen=True)
class OtherFile:
    path: Path
    size: int
    note: str = ""


@dataclass
class TreeIndex:
    root: Path
    lights: list[LightFrame] = field(default_factory=list)
    calibration: list[CalibrationFrame] = field(default_factory=list)
    archived_entries: list[ArchivedEntry] = field(default_factory=list)
    unrecognized: list[UnrecognizedFrame] = field(default_factory=list)
    other_files: list[OtherFile] = field(default_factory=list)

    def summary(self) -> dict:
        """Compact counts for a human to sanity-check at a glance."""
        light_by_scope = Counter((f.provenance, f.telescope) for f in self.lights)
        cal_by_scope = Counter((f.telescope, f.frame_type) for f in self.calibration)
        archived_by_kind = Counter(e.kind for e in self.archived_entries)
        return {
            "lights_by_provenance_telescope": {f"{k[0]}/{k[1]}": v for k, v in light_by_scope.items()},
            "calibration_by_telescope_type": {f"{k[0]}/{k[1]}": v for k, v in cal_by_scope.items()},
            "archived_entries_by_kind": dict(archived_by_kind),
            "unrecognized_count": len(self.unrecognized),
            "other_files_count": len(self.other_files),
        }

    def to_dict(self) -> dict:
        def _serialize(obj) -> dict:
            d = asdict(obj)
            for key, value in d.items():
                if isinstance(value, Path):
                    d[key] = str(value)
            return d

        return {
            "root": str(self.root),
            "lights": [_serialize(f) for f in self.lights],
            "calibration": [_serialize(f) for f in self.calibration],
            "archived_entries": [_serialize(e) for e in self.archived_entries],
            "unrecognized": [_serialize(f) for f in self.unrecognized],
            "other_files": [_serialize(f) for f in self.other_files],
            "summary": self.summary(),
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _scan_zip(zip_path: Path, index: TreeIndex) -> None:
    telescope_hint = _infer_telescope_from_path(zip_path)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        index.other_files.append(
            OtherFile(path=zip_path, size=zip_path.stat().st_size, note=f"Could not open zip: {exc}")
        )
        return

    for inner_name in names:
        basename = Path(inner_name).name
        classified = classify_filename(basename, telescope_hint=telescope_hint)
        if classified is not None:
            kind, fields = classified
        else:
            kind, fields = "unrecognized", {}
        index.archived_entries.append(
            ArchivedEntry(zip_path=zip_path, inner_name=inner_name, kind=kind, fields=fields)
        )


def build_index(root: str | Path) -> TreeIndex:
    """Walk the entire tree under root and classify every file."""
    root = Path(root)
    index = TreeIndex(root=root)

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()

        if suffix in FITS_EXTENSIONS:
            classified = classify_frame(path)
            if isinstance(classified, LightFrame):
                index.lights.append(classified)
            elif isinstance(classified, CalibrationFrame):
                index.calibration.append(classified)
            else:
                index.unrecognized.append(classified)

        elif suffix in ARCHIVE_EXTENSIONS:
            _scan_zip(path, index)

        else:
            telescope_hint = _infer_telescope_from_path(path)
            classified_name = classify_filename(path.name, telescope_hint=telescope_hint)
            if classified_name is not None:
                kind, fields = classified_name
                if kind == "light":
                    index.lights.append(LightFrame(path=path, **fields))
                else:
                    index.calibration.append(CalibrationFrame(path=path, **fields))
            else:
                index.other_files.append(OtherFile(path=path, size=path.stat().st_size))

    return index
