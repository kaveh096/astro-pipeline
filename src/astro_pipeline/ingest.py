"""Stage 1: scan an iTelescope session folder and group subs by target/filter/binning/instrument.

Filename conventions below were reverse-engineered from a real T24 delivery
(not assumed) -- see tests/fixtures/ and the commit that introduced this
file. Two things the FITS headers themselves do NOT reliably provide, which
is why this module is filename-first rather than header-first:

- OBJECT and TELESCOP are blank on every real frame seen so far.
- FILTER is absent (not just empty) on bias/dark frames, which is fine
  since they don't use one, but means "does this frame have a filter" is
  not a safe existence check for classification.

Observed filename conventions (telescope = e.g. "T24"):

  Light:  raw-<telescope>-<user>-<target>-<YYYYMMDD>-<HHMMSS>-<Filter>-BIN<n>-E-<exptime>-<seq>.fit
  Bias:   <telescope>-<user>-Bias-000-LD<YYYYMMDD>-LT<HHMMSS>-BIN<n>.fit
  Dark:   <telescope>-<user>-Dark-<exptime>-LD<YYYYMMDD>-LT<HHMMSS>-BIN<n>.fit

Flat frame naming is UNVERIFIED -- no sample has been seen yet. Flats are
attempted with the same shape as Bias/Dark; a real sample should be used to
confirm/fix this the first time flats are actually available.

Calibration frames in real deliveries are not necessarily captured the same
night as the lights they calibrate (the sample session had lights from
2025-01-15/23/27 and calibration frames from 2025-02-03, grouped by Kaveh
into "Bias (Jan 2025)" / "Dark - 300s (Jan 2025)" folders) -- so calibration
validity is scoped to whatever collection of frames is delivered/organized
together, matched by telescope+binning(+exptime for darks), not to a single
capture night.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from astropy.io import fits

FIT_GLOB_PATTERNS = ("*.fit", "*.fits", "*.fts")

_LIGHT_RE = re.compile(
    r"^raw-(?P<telescope>[A-Za-z0-9]+)-(?P<user>[A-Za-z0-9]+)-(?P<target>[A-Za-z0-9]+)-"
    r"(?P<date>\d{8})-(?P<time>\d{6})-(?P<filter>[A-Za-z0-9]+)-BIN(?P<bin>\d+)-E-"
    r"(?P<exptime>\d+)-(?P<seq>\d+)\.fits?$",
    re.IGNORECASE,
)

_CAL_RE = re.compile(
    r"^(?P<telescope>[A-Za-z0-9]+)-(?P<user>[A-Za-z0-9]+)-"
    r"(?P<frametype>Bias|Dark|Flat)-(?P<exptime>\d+)-"
    r"LD(?P<date>\d{8})-LT(?P<time>\d{6})-BIN(?P<bin>\d+)\.fits?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LightFrame:
    path: Path
    telescope: str
    target: str
    date: str
    time: str
    filter_name: str
    binning: int
    exptime: float
    sequence: int


@dataclass(frozen=True)
class CalibrationFrame:
    path: Path
    telescope: str
    frame_type: str  # "Bias" | "Dark" | "Flat"
    binning: int
    exptime: float
    local_date: str
    local_time: str


@dataclass(frozen=True)
class UnrecognizedFrame:
    path: Path
    reason: str


@dataclass
class IngestReport:
    lights: list[LightFrame] = field(default_factory=list)
    calibration: list[CalibrationFrame] = field(default_factory=list)
    unrecognized: list[UnrecognizedFrame] = field(default_factory=list)

    def light_groups(self) -> dict[tuple[str, str, str, int], list[LightFrame]]:
        """Group lights by (telescope, target, filter, binning) -- the unit
        Stage 4 stacks into one master."""
        groups: dict[tuple[str, str, str, int], list[LightFrame]] = {}
        for frame in self.lights:
            key = (frame.telescope, frame.target, frame.filter_name, frame.binning)
            groups.setdefault(key, []).append(frame)
        return groups

    def calibration_index(self) -> dict[tuple[str, str, int, float], list[CalibrationFrame]]:
        """Index calibration frames by (telescope, frame_type, binning, exptime)."""
        index: dict[tuple[str, str, int, float], list[CalibrationFrame]] = {}
        for frame in self.calibration:
            key = (frame.telescope, frame.frame_type, frame.binning, frame.exptime)
            index.setdefault(key, []).append(frame)
        return index

    def missing_calibration_warnings(self) -> list[str]:
        """Human-readable gaps: for every (telescope, binning) a light group
        needs, is there a Bias and a matching-exptime Dark? Flats are
        checked per (telescope, filter, binning) since they're optics/filter
        specific. Does not decide what to do about gaps -- Stage 2 halts and
        asks; this just reports what's actually there.
        """
        warnings: list[str] = []
        cal_index = self.calibration_index()
        cal_by_type_scope: dict[tuple[str, str, int], list[float]] = {}
        for (telescope, frame_type, binning, exptime), frames in cal_index.items():
            if frames:
                cal_by_type_scope.setdefault((telescope, frame_type, binning), []).append(exptime)

        for (telescope, target, filter_name, binning), lights in self.light_groups().items():
            if (telescope, "Bias", binning) not in cal_by_type_scope:
                warnings.append(
                    f"No Bias frames found for {telescope} BIN{binning} "
                    f"(needed for {target}/{filter_name})."
                )
            light_exptimes = {frame.exptime for frame in lights}
            dark_exptimes = set(cal_by_type_scope.get((telescope, "Dark", binning), []))
            for exptime in light_exptimes - dark_exptimes:
                warnings.append(
                    f"No Dark frames at {exptime:.0f}s found for {telescope} BIN{binning} "
                    f"(needed for {target}/{filter_name})."
                )
            if (telescope, "Flat", binning) not in cal_by_type_scope:
                warnings.append(
                    f"No Flat frames found for {telescope} BIN{binning} "
                    f"(needed for {target}/{filter_name})."
                )
        return warnings


def _read_imagetyp(path: Path) -> str | None:
    try:
        header = fits.getheader(path)
    except Exception:
        return None
    value = header.get("IMAGETYP")
    return str(value) if value is not None else None


def classify_frame(path: Path) -> LightFrame | CalibrationFrame | UnrecognizedFrame:
    """Classify one file by filename first (cheap, verified against real
    data); falls back to reading IMAGETYP from the FITS header only when
    the filename doesn't match a known pattern, so an unfamiliar naming
    convention doesn't get silently mis-grouped.
    """
    name = path.name

    match = _LIGHT_RE.match(name)
    if match:
        return LightFrame(
            path=path,
            telescope=match.group("telescope"),
            target=match.group("target"),
            date=match.group("date"),
            time=match.group("time"),
            filter_name=match.group("filter"),
            binning=int(match.group("bin")),
            exptime=float(match.group("exptime")),
            sequence=int(match.group("seq")),
        )

    match = _CAL_RE.match(name)
    if match:
        return CalibrationFrame(
            path=path,
            telescope=match.group("telescope"),
            frame_type=match.group("frametype").capitalize(),
            binning=int(match.group("bin")),
            exptime=float(match.group("exptime")),
            local_date=match.group("date"),
            local_time=match.group("time"),
        )

    imagetyp = _read_imagetyp(path)
    if imagetyp:
        return UnrecognizedFrame(
            path=path,
            reason=(
                f"Filename did not match known conventions, but FITS header "
                f"IMAGETYP='{imagetyp}' -- needs a filename-pattern update, "
                f"not silently grouped."
            ),
        )
    return UnrecognizedFrame(path=path, reason="Filename and FITS header both unrecognized.")


def scan_session(root: str | Path) -> IngestReport:
    root = Path(root)
    report = IngestReport()
    seen: set[Path] = set()
    for pattern in FIT_GLOB_PATTERNS:
        for path in root.rglob(pattern):
            if path in seen:
                continue
            seen.add(path)
            classified = classify_frame(path)
            if isinstance(classified, LightFrame):
                report.lights.append(classified)
            elif isinstance(classified, CalibrationFrame):
                report.calibration.append(classified)
            else:
                report.unrecognized.append(classified)
    return report
