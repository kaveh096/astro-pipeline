"""Stage 1: scan an iTelescope delivery tree and classify raw subs.

Filename conventions below were reverse-engineered from real deliveries
(not assumed) across two different telescopes (T24, T21) and two different
users' data mixed into one tree -- see the commits that introduced/extended
this file. FITS headers are not the source of truth for identity:

- OBJECT and TELESCOP are blank on every real frame seen so far.
- FILTER is absent (not just empty) on bias/dark frames.

Observed conventions -- at least three distinct ones across two telescopes,
so this module tries several regexes rather than assuming one:

  Light (T24 and T21 both):
    <provenance>-<telescope>-<user>-<target>-<YYYYMMDD>-<HHMMSS>-<Filter>-BIN<n>-<E|W>-<exptime>-<seq>.<ext>
    provenance is "raw" (unprocessed), "calibrated" (iTelescope-side
    calibrated -- a real delivery had BOTH raw and calibrated versions of
    the same exposure bundled together), or "jpeg" (quick-look preview).
    The <E|W> token is NOT a constant -- it's a meridian-side flag, proven
    by real data containing both. An earlier version of this module
    hardcoded it as literal "-E-", which was a filename-shape bug, not a
    real convention; it only looked constant because the first sample
    happened to be all-"E".

  Bias/Dark (T24 style, telescope embedded in filename):
    <telescope>-<user>-Bias-000-LD<YYYYMMDD>-LT<HHMMSS>-BIN<n>.fit
    <telescope>-<user>-Dark-<exptime>-LD<YYYYMMDD>-LT<HHMMSS>-BIN<n>.fit

  Bias/Dark (T21 style, camera model instead of telescope -- telescope
  must be inferred from the containing directory, e.g. ".../T21/Bias/..."):
    <camera>-<index>bias Bin<n>.fit
    <camera>-<index>dark<exptime>secBin<n>.fit

  Flat (T21 style, also telescope-less, also path-inferred):
    scope_<Filter>_<n>x<n>_skyflat<index>.fit

Calibration frames are not tied to the capture night of the lights they
calibrate -- real deliveries have calibration frames dated weeks to months
away from the lights, grouped by iTelescope/Kaveh into date-ish folders
that cover many light sessions. Calibration validity is scoped to whatever
collection is delivered/organized together (matched by
telescope+binning(+exptime for darks)), not to a single capture night.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from astropy.io import fits

FIT_GLOB_PATTERNS = ("*.fit", "*.fits", "*.fts")

_TELESCOPE_DIR_RE = re.compile(r"^T\d+$", re.IGNORECASE)


# Extension is deliberately broad (not just .fit/.fits): the same per-exposure
# naming convention is also used for iTelescope-side-calibrated TIFFs and JPEG
# previews of the same exposure, confirmed in real deliveries.
_LIGHT_RE = re.compile(
    r"^(?P<provenance>raw|calibrated|jpeg)-(?P<telescope>[A-Za-z0-9]+)-(?P<user>[A-Za-z0-9]+)-"
    r"(?P<target>[A-Za-z0-9]+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<filter>[A-Za-z0-9]+)-"
    r"BIN(?P<bin>\d+)-(?P<side>[EW])-(?P<exptime>\d+)-(?P<seq>\d+)\.(?:fits?|fts|tiff?|jpe?g)$",
    re.IGNORECASE,
)

_CAL_RE_T24 = re.compile(
    r"^(?P<telescope>[A-Za-z0-9]+)-(?P<user>[A-Za-z0-9]+)-"
    r"(?P<frametype>Bias|Dark|Flat)-(?P<exptime>\d+)-"
    r"LD(?P<date>\d{8})-LT(?P<time>\d{6})-BIN(?P<bin>\d+)\.fits?$",
    re.IGNORECASE,
)

# e.g. "FLI6303 -0001biasBin1.fit" / "FLI6303 -0001dark900secBin1.fit"
# Telescope isn't in the filename -- caller supplies it from the path.
_CAL_RE_CAMERA = re.compile(
    r"^(?P<camera>[A-Za-z0-9]+)\s*-(?P<index>\d+)"
    r"(?P<frametype>bias|dark)(?:(?P<exptime>\d+)sec)?Bin(?P<bin>\d+)\.fits?$",
    re.IGNORECASE,
)

# e.g. "scope_Luminance_1x1_skyflat0.fit" -- also telescope-less.
_FLAT_RE_SKYFLAT = re.compile(
    r"^scope_(?P<filter>[A-Za-z0-9]+)_(?P<binx>\d+)x(?P<biny>\d+)_skyflat(?P<index>\d+)\.fits?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LightFrame:
    path: Path
    provenance: str  # "raw" | "calibrated" | "jpeg"
    telescope: str
    user: str  # iTelescope account that captured this -- collaborators show
    # up as different users on the same telescope/target and must not be
    # silently merged (confirmed real: same target, same telescope, two
    # users, different binning choices for RGB).
    target: str
    date: str
    time: str
    filter_name: str
    binning: int
    side: str  # "E" | "W" -- meridian side, not a constant
    exptime: float
    sequence: int


@dataclass(frozen=True)
class CalibrationFrame:
    path: Path
    telescope: str
    frame_type: str  # "Bias" | "Dark" | "Flat"
    binning: int
    exptime: float  # 0.0 for flats (not a matching criterion for them)
    local_date: str | None = None
    local_time: str | None = None


@dataclass(frozen=True)
class UnrecognizedFrame:
    path: Path
    reason: str


@dataclass
class IngestReport:
    lights: list[LightFrame] = field(default_factory=list)
    calibration: list[CalibrationFrame] = field(default_factory=list)
    unrecognized: list[UnrecognizedFrame] = field(default_factory=list)

    def light_groups(self) -> dict[tuple[str, str, str, str, int], list[LightFrame]]:
        """Group RAW lights by (telescope, user, target, filter, binning) --
        the unit Stage 4 stacks into one master. `user` is part of the key
        deliberately: a real delivery has two different iTelescope accounts
        (collaborators) shooting the same target on the same telescope with
        different binning choices for RGB -- silently merging their subs
        into one group would mix incompatible data without anyone deciding
        to. Combining multiple users' *masters* later is a legitimate,
        separate step (same pattern as combining multiple instruments'
        masters), not something this grouping does implicitly.

        Deliberately excludes "calibrated"/"jpeg" provenance frames -- those
        are catalog-only, never fed back into the calibration/stacking
        pipeline as if they were unprocessed subs (a real delivery bundles
        both raw and iTelescope-calibrated versions of the same exposure;
        conflating them would double-process or silently prefer one over
        the other).
        """
        groups: dict[tuple[str, str, str, str, int], list[LightFrame]] = {}
        for frame in self.lights:
            if frame.provenance != "raw":
                continue
            key = (frame.telescope, frame.user, frame.target, frame.filter_name, frame.binning)
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
        checked per (telescope, binning) regardless of exptime (flats don't
        share the exptime-matching requirement bias/dark do). Does not
        decide what to do about gaps -- Stage 2 halts and asks; this just
        reports what's actually there.
        """
        warnings: list[str] = []
        cal_index = self.calibration_index()
        cal_by_type_scope: dict[tuple[str, str, int], list[float]] = {}
        for (telescope, frame_type, binning, exptime), frames in cal_index.items():
            if frames:
                cal_by_type_scope.setdefault((telescope, frame_type, binning), []).append(exptime)

        for (telescope, user, target, filter_name, binning), lights in self.light_groups().items():
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


def _infer_telescope_from_path(path: Path) -> str | None:
    """Some real calibration filenames (camera-model-based, skyflat-based)
    don't embed the telescope at all -- it's only recoverable from the
    directory structure (e.g. ".../Calibrations/T21/Bias/..."). Searches
    ancestor directory names for a "T<digits>" token, nearest first."""
    for parent in path.parents:
        if _TELESCOPE_DIR_RE.match(parent.name):
            return parent.name.upper()
    return None


def classify_filename(
    name: str, telescope_hint: str | None = None
) -> tuple[str, dict] | None:
    """Pure filename classification, no file I/O -- usable both for real
    files on disk and for names peeked out of a zip archive without
    extracting it. Returns (kind, fields) where kind is "light" or
    "calibration", or None if nothing matched. `telescope_hint` is used
    for the telescope-less camera-model/skyflat calibration conventions
    when there's no meaningful path to infer it from (e.g. inside a zip).
    """
    match = _LIGHT_RE.match(name)
    if match:
        return "light", {
            "provenance": match.group("provenance").lower(),
            "telescope": match.group("telescope"),
            "user": match.group("user"),
            "target": match.group("target"),
            "date": match.group("date"),
            "time": match.group("time"),
            "filter_name": match.group("filter"),
            "binning": int(match.group("bin")),
            "side": match.group("side").upper(),
            "exptime": float(match.group("exptime")),
            "sequence": int(match.group("seq")),
        }

    match = _CAL_RE_T24.match(name)
    if match:
        return "calibration", {
            "telescope": match.group("telescope"),
            "frame_type": match.group("frametype").capitalize(),
            "binning": int(match.group("bin")),
            "exptime": float(match.group("exptime")),
            "local_date": match.group("date"),
            "local_time": match.group("time"),
        }

    match = _CAL_RE_CAMERA.match(name)
    if match and telescope_hint:
        return "calibration", {
            "telescope": telescope_hint,
            "frame_type": match.group("frametype").capitalize(),
            "binning": int(match.group("bin")),
            "exptime": float(match.group("exptime")) if match.group("exptime") else 0.0,
        }

    match = _FLAT_RE_SKYFLAT.match(name)
    if match and telescope_hint:
        return "calibration", {
            "telescope": telescope_hint,
            "frame_type": "Flat",
            "binning": int(match.group("binx")),
            "exptime": 0.0,
        }

    return None


def classify_frame(path: Path) -> LightFrame | CalibrationFrame | UnrecognizedFrame:
    """Classify one file by filename first (cheap, verified against real
    data from two telescopes); falls back to reading IMAGETYP from the
    FITS header only when nothing matches, so an unfamiliar naming
    convention doesn't get silently mis-grouped.
    """
    name = path.name
    telescope_hint = _infer_telescope_from_path(path)

    classified = classify_filename(name, telescope_hint=telescope_hint)
    if classified is not None:
        kind, fields = classified
        if kind == "light":
            return LightFrame(path=path, **fields)
        return CalibrationFrame(path=path, **fields)

    # Matched a telescope-less calibration pattern but no telescope could
    # be inferred from the path -- distinguish this from "no pattern
    # matched at all" so the fix (path missing a T<n> folder) is obvious.
    if _CAL_RE_CAMERA.match(name) or _FLAT_RE_SKYFLAT.match(name):
        return UnrecognizedFrame(
            path=path,
            reason=(
                "Matched a telescope-less calibration filename pattern but "
                "no 'T<digits>' telescope directory was found in its path."
            ),
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
