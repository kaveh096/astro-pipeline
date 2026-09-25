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
    <provenance>-<telescope>-<user>-<target>-<YYYYMMDD>-<HHMMSS>-<Filter>-BIN<n>-<E|W|_>-<exptime>-<seq>.<ext>
    provenance is "raw" (unprocessed), "calibrated" (iTelescope-side
    calibrated -- a real delivery had BOTH raw and calibrated versions of
    the same exposure bundled together), or "jpeg" (quick-look preview).
    The <E|W> token is NOT a constant -- it's a meridian-side flag, proven
    by real data containing both. An earlier version of this module
    hardcoded it as literal "-E-", which was a filename-shape bug, not a
    real convention; it only looked constant because the first sample
    happened to be all-"E". A real T72 exposure (NGC 3628, Feb 2025) uses
    "_" here instead -- meridian side not tracked/applicable for that
    frame. <target> may itself contain spaces (real: "NGC 3628") -- an
    earlier alphanumeric-only pattern silently excluded every light frame
    from that whole session as UnrecognizedFrame.

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
away from the lights, grouped by iTelescope/the account holder into date-ish folders
that cover many light sessions. Calibration validity is scoped to whatever
collection is delivered/organized together (matched by
telescope+binning(+exptime for darks)), not to a single capture night.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from astropy.io import fits

logger = logging.getLogger(__name__)

FIT_GLOB_PATTERNS = ("*.fit", "*.fits", "*.fts")
ARCHIVE_GLOB_PATTERNS = ("*.zip",)

# Directory (inside a project folder) holding pipeline-generated data. Must
# never be scanned as input -- see is_generated(). Kept as a literal here
# rather than imported from workspace.py to avoid a circular import;
# workspace.PIPELINE_DIRNAME is the same value and the two are asserted
# equal in the tests.
GENERATED_DIRNAME = "_pipeline"

_TELESCOPE_DIR_RE = re.compile(r"^T\d+$", re.IGNORECASE)


# Extension is deliberately broad (not just .fit/.fits): the same per-exposure
# naming convention is also used for iTelescope-side-calibrated TIFFs and JPEG
# previews of the same exposure, confirmed in real deliveries.
#
# Target allows internal spaces (` `), not just alphanumerics: a real NGC
# 3628 delivery (Feb 2025) uses literal "NGC 3628" as the target token in
# the filename (e.g. "calibrated-T73-observer1-NGC 3628-20250224-...-Red-
# BIN2-E-240-001.fit") -- an earlier alphanumeric-only pattern silently
# classified every one of that session's 150 real light frames as
# UnrecognizedFrame (caught via classify_frame's own diagnostic: "Filename
# did not match known conventions, but FITS header IMAGETYP='Light Frame'").
# Safe to widen without ambiguity: target can't contain a literal "-", so
# the following "-<8-digit date>-<6-digit time>-" boundary still resolves
# unambiguously via backtracking.
#
# Side also allows "_": a real T72 exposure of NGC 3628 (single Luminance
# test sub, Feb 2025) uses "-_-" where every other real sample uses "-E-"
# or "-W-" -- meridian side not tracked/applicable for that exposure,
# rather than a third real side value. Stored as "_" (LightFrame.side),
# not normalized away, since nothing downstream groups or filters on side.
_LIGHT_RE = re.compile(
    r"^(?P<provenance>raw|calibrated|jpeg)-(?P<telescope>[A-Za-z0-9]+)-(?P<user>[A-Za-z0-9]+)-"
    r"(?P<target>[A-Za-z0-9 ]+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<filter>[A-Za-z0-9]+)-"
    r"BIN(?P<bin>\d+)-(?P<side>[EW_])-(?P<exptime>\d+)-(?P<seq>\d+)\.(?:fits?|fts|tiff?|jpe?g)$",
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

# The three fixed templates IngestReport.missing_calibration_warnings()
# emits (below) -- module-level (not skill/interview.py-private, where an
# earlier version of these lived) as the shared source of truth, since
# skill/ has no __init__.py and is not an importable package: pipeline.py
# importing these OUT of skill/ would be architecturally backwards.
# skill/interview.py's dedupe_calibration_warnings() imports these rather
# than redefining them; pipeline.py's precalibrated-path filtering
# (run_lrgb, 2026-09) uses warning_telescope() below for the same reason
# -- an anchored regex extracting a real, whitespace-delimited telescope
# token is not equivalent to a naive substring test, which could (in
# principle, not observed with today's real telescope names) collide with
# a target/filter name inside the message.
CALIBRATION_WARNING_RES = {
    "bias": re.compile(
        r"^No Bias frames found for (?P<telescope>\S+) BIN(?P<binning>\d+) "
        r"\(needed for (?P<target>[^/]+)/(?P<filter>[^)]+)\)\.$"
    ),
    "dark": re.compile(
        r"^No Dark frames at (?P<exptime>[\d.]+)s found for (?P<telescope>\S+) BIN(?P<binning>\d+) "
        r"\(needed for (?P<target>[^/]+)/(?P<filter>[^)]+)\)\.$"
    ),
    "flat": re.compile(
        r"^No Flat frames found for (?P<telescope>\S+) BIN(?P<binning>\d+) "
        r"\(needed for (?P<target>[^/]+)/(?P<filter>[^)]+)\)\.$"
    ),
}


def warning_telescope(line: str) -> str | None:
    """The telescope named in one missing_calibration_warnings() line, via
    an anchored regex match against CALIBRATION_WARNING_RES -- None if the
    line doesn't match any of the three known templates (e.g. the wording
    changed, or it's not one of these warnings at all)."""
    for pattern in CALIBRATION_WARNING_RES.values():
        m = pattern.match(line)
        if m:
            return m["telescope"]
    return None


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
    side: str  # "E" | "W" -- meridian side, not a constant; "_" seen on at
    # least one real exposure where meridian side wasn't tracked/applicable
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
    # Only ever populated for frame_type == "Flat" (bias/dark frames don't
    # vary by filter and never set this). Keyword-default so every existing
    # construction site (always keyword-based) stays valid unchanged.
    # _CAL_RE_T24's Flat branch has no filter token in its filename at all
    # (see classify_filename), so a Flat classified via that convention
    # leaves this None -- flat_index() drops those defensively rather than
    # guessing a filter (see flat_index()'s docstring).
    filter_name: str | None = None
    # Step 4b (plan-flats-v4.md, G4/G5): "filename" (the default, and every
    # frame classified above this point) or "header" for a frame recovered
    # ONLY via classify_tree's opt-in calibration_header_fallback -- never
    # populated any other way. Read via `getattr(f, "source", "filename")`
    # everywhere it's consulted (calibration_policy.infer_calibration_mode),
    # not `f.source` directly, so the ~20 pre-existing test fakes that
    # return plain strings (not CalibrationFrame objects) from
    # calibration_index()/flat_index() keep working unchanged.
    source: str = "filename"


@dataclass(frozen=True)
class UnrecognizedFrame:
    path: Path
    reason: str


@dataclass
class IngestReport:
    lights: list[LightFrame] = field(default_factory=list)
    calibration: list[CalibrationFrame] = field(default_factory=list)
    unrecognized: list[UnrecognizedFrame] = field(default_factory=list)
    # The real (zip_path, inner_name, fields) tuples behind every zip-
    # peeked placeholder in `lights` (classify_tree's own zip-aware pass,
    # `_peek_zipped_lights`) -- `scan_session` extracts directly from
    # these instead of re-listing each zip and re-running
    # classify_filename() a second time (see scan_session's own
    # docstring for the double-work this used to do, unconditionally, on
    # every scan_session call).
    zip_peeks: list[tuple[Path, str, dict]] = field(default_factory=list)

    def _light_groups_by_provenance(
        self, provenance: str
    ) -> dict[tuple[str, str, str, str, int], list[LightFrame]]:
        """Shared implementation behind light_groups()/calibrated_light_groups()
        so the two provenance views cannot silently drift apart."""
        groups: dict[tuple[str, str, str, str, int], list[LightFrame]] = {}
        for frame in self.lights:
            if frame.provenance != provenance:
                continue
            key = (frame.telescope, frame.user, frame.target, frame.filter_name, frame.binning)
            groups.setdefault(key, []).append(frame)
        return groups

    def light_groups(self) -> dict[tuple[str, str, str, str, int], list[LightFrame]]:
        """Group RAW lights by (telescope, user, target, filter, binning).
        `user` is part of the key deliberately: a real delivery has two
        different iTelescope accounts (collaborators) shooting the same
        target on the same telescope with different binning choices for
        RGB -- silently merging their subs into one group here would mix
        incompatible data without anyone deciding to. See
        `instrument_groups()` for the merged view actually used to build a
        master, which combines across users only where doing so is
        physically valid (same telescope + binning).

        Deliberately excludes "calibrated"/"jpeg" provenance frames -- those
        are catalog-only, never fed back into the calibration/stacking
        pipeline as if they were unprocessed subs (a real delivery bundles
        both raw and iTelescope-calibrated versions of the same exposure;
        conflating them would double-process or silently prefer one over
        the other). See calibrated_light_groups() for the mirror view used
        by CalibrationMode.PRECALIBRATED (calibration.py) -- a group is
        either fed through local bias/dark/flat calibration (raw_local) or
        used directly (precalibrated), never both, never silently chosen
        between.
        """
        return self._light_groups_by_provenance("raw")

    def calibrated_light_groups(self) -> dict[tuple[str, str, str, str, int], list[LightFrame]]:
        """Group CALIBRATED-provenance lights by (telescope, user, target,
        filter, binning) -- the mirror of light_groups() for iTelescope-side
        precalibrated deliveries (CalibrationMode.PRECALIBRATED,
        calibration.py). Never merged into light_groups()'s own raw-only
        view; see that method's docstring for why the split is deliberate.
        """
        return self._light_groups_by_provenance("calibrated")

    def _instrument_groups_from(
        self, groups: dict[tuple[str, str, str, str, int], list[LightFrame]]
    ) -> dict[tuple[str, str, str, int], list[LightFrame]]:
        merged: dict[tuple[str, str, str, int], list[LightFrame]] = {}
        for (telescope, _user, target, filter_name, binning), frames in groups.items():
            key = (telescope, target, filter_name, binning)
            merged.setdefault(key, []).extend(frames)
        return merged

    def instrument_groups(self) -> dict[tuple[str, str, str, int], list[LightFrame]]:
        """Group RAW lights by (telescope, target, filter, binning), merging
        across users.

        This is the unit that actually gets calibrated + registered +
        stacked into ONE master: when two users share a telescope and
        binning for a filter (e.g. two collaborators' Luminance/BIN1 subs),
        combining their raw frames into a single stack gives Siril's
        rejection algorithm visibility into every individual frame, which
        rejects outliers (satellite trails, etc) better than averaging two
        independently-stacked masters would.

        Different-binning contributions for the same filter (e.g. one
        user's BIN1 RGB against another's BIN2 RGB) necessarily stay in
        separate groups here -- they have different pixel scale/dimensions
        and cannot be combined at the raw-sub level at all. That case is
        reconciled at the MASTER level instead, once each binning's own
        master exists (see reconciliation.py).
        """
        return self._instrument_groups_from(self.light_groups())

    def calibrated_instrument_groups(self) -> dict[tuple[str, str, str, int], list[LightFrame]]:
        """Mirror of instrument_groups() over calibrated_light_groups()."""
        return self._instrument_groups_from(self.calibrated_light_groups())

    def calibration_index(self) -> dict[tuple[str, str, int, float], list[CalibrationFrame]]:
        """Index calibration frames by (telescope, frame_type, binning, exptime)."""
        index: dict[tuple[str, str, int, float], list[CalibrationFrame]] = {}
        for frame in self.calibration:
            key = (frame.telescope, frame.frame_type, frame.binning, frame.exptime)
            index.setdefault(key, []).append(frame)
        return index

    def flat_index(self) -> dict[tuple[str, int, str], list[CalibrationFrame]]:
        """Index Flat frames by (telescope, binning, filter_name).

        A DELIBERATELY SEPARATE index from calibration_index(), not a widened
        key on it. Bias/Dark genuinely don't vary by filter, and every real
        bias/dark lookup in the codebase (pipeline.py's
        `cal_index[(telescope, "Bias", binning, 0.0)]`, calibration.py's
        `select_dark()` iterating `cal_index.items()` filtering by
        `t, ftype, b`) keys on the existing 4-tuple. Folding filter_name in
        would force every one of those call sites to pass a spurious
        `filter_name=None` sentinel just to keep the tuple shape uniform --
        buys nothing, and adds a footgun (a bias frame accidentally tagged
        with a real filter string by some future code path would silently
        vanish from every existing bias/dark lookup). A separate, filter-real,
        flat-only index is the narrower, lower-risk change.

        exptime is deliberately excluded from the key -- flats aren't
        exptime-matched against lights (see CalibrationFrame.exptime's own
        comment: "0.0 for flats (not a matching criterion for them)").

        Any CalibrationFrame with frame_type == "Flat" and filter_name is
        None is dropped here (logged, not silently mis-bucketed under a
        guessed filter) -- this is real for `_CAL_RE_T24`'s Flat branch
        (`<telescope>-<user>-Flat-<exptime>-LD...-LT...-BIN<n>`), which has
        no filter token anywhere in its filename. T24 ships zero flats of
        any kind in the real data this codebase has seen so far, so this
        path is currently unreached in practice, but the defensive drop
        exists so a future flat shipped under that convention doesn't get
        silently attributed to a fake/guessed filter instead of refusing.
        """
        index: dict[tuple[str, int, str], list[CalibrationFrame]] = {}
        for frame in self.calibration:
            if frame.frame_type != "Flat":
                continue
            if frame.filter_name is None:
                logger.warning(
                    "Dropping Flat frame with no filter identity: %s "
                    "(telescope=%s, binning=%s) -- matched a calibration-frame "
                    "naming convention with no filter token in the filename; "
                    "refusing to guess rather than mis-bucket it.",
                    frame.path,
                    frame.telescope,
                    frame.binning,
                )
                continue
            key = (frame.telescope, frame.binning, frame.filter_name)
            index.setdefault(key, []).append(frame)
        return index

    def missing_calibration_warnings(self) -> list[str]:
        """Human-readable gaps: for every (telescope, binning) a light group
        needs, is there a Bias and a matching-exptime Dark? Flats are
        checked per (telescope, binning, filter_name) -- exptime is not a
        matching criterion for flats, but filter identity IS, and flat
        presence must be checked per-filter, not merely "does this
        telescope+binning have a flat for ANY filter" (that was a real,
        live bug: a telescope shipping only a Luminance flat would silently
        suppress the "no flat" warning for Red/Green/Blue/etc too, since the
        old check was derived from the filter-blind calibration_index()).
        Does not decide what to do about gaps -- Stage 2 halts and asks;
        this just reports what's actually there.
        """
        warnings: list[str] = []
        cal_index = self.calibration_index()
        cal_by_type_scope: dict[tuple[str, str, int], list[float]] = {}
        for (telescope, frame_type, binning, exptime), frames in cal_index.items():
            if frame_type == "Flat":
                continue  # flat presence is checked per-filter via flat_index() below
            if frames:
                cal_by_type_scope.setdefault((telescope, frame_type, binning), []).append(exptime)
        flat_index = self.flat_index()

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
            if (telescope, binning, filter_name) not in flat_index:
                # Message template deliberately kept byte-identical in shape
                # to the Bias/Dark lines above ("... (needed for
                # {target}/{filter_name})."), NOT reworded to mention
                # "filter" explicitly -- skill/interview.py's
                # dedupe_calibration_warnings() parses this exact template
                # via a regex (_FLAT_RE) to collapse per-user/per-filter
                # repeats into one summary line per (telescope, binning).
                # Only the PRESENCE CHECK above changed (now per-filter via
                # flat_index(), fixing the real bug in fact 3 of
                # plan-flats-v3.md); the wording did not need to and must
                # not change, or the downstream parser silently degrades to
                # verbatim passthrough (see
                # test_dedupe_passes_through_unrecognized_lines).
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
            "filter_name": match.group("filter"),
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


def is_generated(path: Path, root: Path) -> bool:
    """True if `path` lives under a generated-output directory.

    The pipeline writes its intermediates into `<project>/_pipeline/`, which
    sits INSIDE the directory being scanned. Without this exclusion a second
    run re-discovers its own staged copies of the raw frames and treats them
    as additional raw data -- verified real and badly wrong, not theoretical:
    a re-run reported "26 lights (10 bias, 10 dark)" for a group that has
    exactly 13 lights, 5 bias and 5 dark, because every staged copy was
    counted a second time. Calibrated (`pp_`) and registered (`r_`) outputs
    would likewise be fed back in as if they were unprocessed subs.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return GENERATED_DIRNAME in relative.parts


def _materialize_zipped_light(zip_path: Path, inner_name: str, fields: dict, extract_dir: Path) -> LightFrame:
    """Extract one already-classified zip member onto disk and build its
    real `LightFrame` -- `fields` was already computed ONCE by
    `classify_filename()` inside `classify_tree`'s own peek pass
    (`_peek_zipped_lights`, via `IngestReport.zip_peeks`); this performs
    only the extraction side effect, never re-lists the zip's `namelist()`
    or re-runs `classify_filename()` a second time (see `scan_session`'s
    own docstring for the double-work this replaces).

    Idempotent: if the extracted file already exists, it is reused rather
    than re-extracted, so a resumed scan doesn't redo the work.
    """
    basename = Path(inner_name).name
    extract_dir.mkdir(parents=True, exist_ok=True)
    out_path = extract_dir / basename
    if not out_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open(inner_name) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
    return LightFrame(path=out_path, **fields)


def _peek_zipped_lights(zip_path: Path) -> tuple[list[LightFrame], list[tuple[Path, str, dict]]]:
    """Classify a zip's raw-provenance light entries BY NAME ONLY, without
    extracting anything -- the zip-aware half of `classify_tree` (Step 4a,
    plan-flats-v4.md), mirroring `index._scan_zip`'s own peek-by-name
    approach. Filters to "raw" provenance only -- calibrated/jpeg zip
    entries are catalog-only duplicates, and no real delivery seen so far
    ships calibration frames inside a zip.

    Returns TWO things, computed from the SAME single `namelist()`/
    `classify_filename()` pass:
    - the peeked `LightFrame`s, each with a PLACEHOLDER `path`
      (`zip_path / basename`, not a real, readable file). `scan_session`
      recognises this shape (`path.parent.suffix.lower() == ".zip"`,
      which a genuine bare-file light's parent directory can never be)
      and replaces every one of these placeholders with its real
      extracted-file equivalent before returning -- never appends both,
      which would double-count (see ??1.5's real M51 duplication bug this
      exact append-instead-of-replace mistake caused elsewhere:
      `_instrument_groups_from` merging two zips' worth of identically-
      named lights into one inflated group);
    - the real `(zip_path, inner_name, fields)` tuples behind those same
      placeholders, so `scan_session` can extract directly from these
      (`_materialize_zipped_light`) instead of re-listing this same zip
      and re-running `classify_filename()` on every entry a second time.
    """
    telescope_hint = _infer_telescope_from_path(zip_path)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return [], []

    peeked: list[LightFrame] = []
    real: list[tuple[Path, str, dict]] = []
    for inner_name in names:
        basename = Path(inner_name).name
        classified = classify_filename(basename, telescope_hint=telescope_hint)
        if classified is None:
            continue
        kind, fields = classified
        if kind != "light" or fields["provenance"] != "raw":
            continue
        peeked.append(LightFrame(path=zip_path / basename, **fields))
        real.append((zip_path, inner_name, fields))
    return peeked, real


# Step 4b (plan-flats-v4.md): telescope attribution rule (b) -- a token
# T<1-3 digits>, delimited by non-alphanumerics (or string edges), inside
# an ancestor DIRECTORY name (never the file's own name). Real, load-
# bearing case: M31/T05's project root is literally named
# "M31 - Andromeda - T5 - RGB - Aug 2021" -- normalising "T5" to "T05"
# here is what lets it match the T05 token its own real bias/dark/flat
# filenames and folder structure otherwise use. Real ambiguous case: M51's
# root "M51 - Whirlpool galaxy - T24 & T21 - Jan 2025" carries TWO distinct
# tokens -- rule (b) refuses rather than guessing.
_TELESCOPE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])T(\d{1,3})(?![A-Za-z0-9])")


def _attribute_telescope_for_fallback(path: Path, root: Path, raw_telescopes: set[str]) -> str | None:
    """Attribution rules (a)/(b)/(c), in order, for a header-fallback
    candidate calibration frame (Step 4b). None if none of the three
    succeed (rule (b) ambiguous: more than one distinct token in the same
    ancestor name; rule (c): more than one telescope's raw lights exist,
    with nothing to disambiguate)."""
    # (a) a strict "^T<digits>$" ancestor folder -- the existing, filename-
    # side helper already implements exactly this rule.
    hint = _infer_telescope_from_path(path)
    if hint is not None:
        return hint

    # (b) nearest ancestor directory name, up to and including the root,
    # containing exactly one distinct T<digits> token.
    ancestors: list[Path] = []
    current = path.parent
    while True:
        ancestors.append(current)
        if current == root or current.parent == current:
            break
        current = current.parent
    for parent in ancestors:
        tokens = {f"T{int(m):02d}" for m in _TELESCOPE_TOKEN_RE.findall(parent.name)}
        if len(tokens) > 1:
            return None
        if len(tokens) == 1:
            return next(iter(tokens))

    # (c) the root's raw lights (bare or zip-peeked) come from exactly one
    # telescope.
    if len(raw_telescopes) == 1:
        return next(iter(raw_telescopes))
    return None


def _raw_light_reference(report: IngestReport) -> dict[tuple[str, int], Path]:
    """First BARE (never a zip-peek placeholder -- those aren't real,
    readable files) raw light's path per (telescope, binning), for Step
    4b's INSTRUME/NAXIS1/NAXIS2 cross-check. Real ambiguous case this
    guards against: T68 (IC 1396) shipped a genuinely different camera in
    2023 (NGC 3628, 4944x3284) under the identical INSTRUME string used in
    2021 (6248x4176) -- NAXIS is the only real discriminator (see
    ingest.py's own module docstring / plan ??2.1) -- never a production
    trigger today since the two deliveries are never co-scanned in one
    `scan_session` call, but this cross-check is what would catch it if
    they ever were."""
    ref: dict[tuple[str, int], Path] = {}
    for light in report.lights:
        if light.provenance != "raw":
            continue
        if light.path.parent.suffix.lower() == ".zip":
            continue
        key = (light.telescope, light.binning)
        if key not in ref:
            ref[key] = light.path
    return ref


def _filename_only_calibration_fields(name: str) -> tuple[str, int] | None:
    """Re-derives (frame_type, binning) from a TELESCOPE-LESS calibration
    filename pattern (camera-model bias/dark, or skyflat), WITHOUT
    requiring a telescope hint -- `classify_filename` itself refuses to
    return anything for these two patterns without one (`and
    telescope_hint`), which is exactly why a file matching one of them but
    with no hint lands in `report.unrecognized` in the first place (see
    `classify_frame`'s own early-return). Used only for the "filename vs
    header" disagreement check below -- None if neither pattern matches at
    all (the far more common real case: no filename pattern matched
    anything, so there is nothing to disagree with)."""
    match = _CAL_RE_CAMERA.match(name)
    if match:
        return match.group("frametype").capitalize(), int(match.group("bin"))
    match = _FLAT_RE_SKYFLAT.match(name)
    if match:
        return "Flat", int(match.group("binx"))
    return None


def _classify_calibration_via_header(
    frame: UnrecognizedFrame,
    root: Path,
    raw_telescopes: set[str],
    raw_light_ref: dict[tuple[str, int], Path],
    filename_recognized_types: set[tuple[str, str]],
) -> CalibrationFrame | UnrecognizedFrame:
    """Step 4b's actual fallback rule set, applied to ONE already-
    unrecognized frame (no filename pattern matched at all, OR a
    telescope-less calibration pattern matched with no telescope hint --
    both land in `report.unrecognized` from the filename pass above, and
    both need this fresh header read). Returns the original `frame`
    unchanged for every rejection EXCEPT the two the plan calls out by
    name (shadowing; no readable raw light to cross-check), which get a
    specific, actionable reason instead of the filename pass's generic one.
    """
    path = frame.path
    try:
        header = fits.getheader(path)
    except Exception:
        return frame  # unreadable/truncated header -- rejected

    calstat = header.get("CALSTAT")
    if calstat and "M" in str(calstat):
        return frame  # a master (CALSTAT contains 'M') -- rejected

    imagetyp = str(header.get("IMAGETYP") or "").lower()
    if "bias" in imagetyp:
        frame_type = "Bias"
    elif "dark" in imagetyp:
        frame_type = "Dark"
    elif "flat" in imagetyp:
        frame_type = "Flat"
    else:
        # Includes "Light Frame" (lights are NEVER classified this way --
        # a real, present case: IC 1396's own unparsed calibrated- lights)
        # and anything else unrecognized.
        return frame

    xbinning, ybinning = header.get("XBINNING"), header.get("YBINNING")
    if xbinning is None or ybinning is None:
        return frame
    try:
        xbinning, ybinning = int(xbinning), int(ybinning)
    except (TypeError, ValueError):
        return frame
    if xbinning != ybinning:
        return frame
    binning = xbinning

    # Filename vs header: the filename wins. A file that matched a
    # telescope-less pattern (camera-model bias/dark, skyflat) already
    # told us its OWN frame_type/binning from the name -- if the header
    # disagrees, refuse rather than trust the header over an already-
    # parsed filename.
    filename_fields = _filename_only_calibration_fields(path.name)
    if filename_fields is not None and filename_fields != (frame_type, binning):
        return frame

    telescope = _attribute_telescope_for_fallback(path, root, raw_telescopes)
    if telescope is None:
        return frame

    if (telescope, frame_type) in filename_recognized_types:
        return UnrecognizedFrame(
            path=path,
            reason=(
                f"Header suggests a {telescope} {frame_type} frame, but "
                f"{telescope} already has filename-recognised {frame_type} "
                "frames -- shadowed by filename-recognised frames of this "
                "type, never merged into the same index entry."
            ),
        )

    ref_path = raw_light_ref.get((telescope, binning))
    if ref_path is None:
        return UnrecognizedFrame(
            path=path,
            reason=(
                f"Header suggests a {telescope} BIN{binning} {frame_type} frame, "
                "but no readable raw light to cross-check."
            ),
        )
    try:
        ref_header = fits.getheader(ref_path)
    except Exception:
        return frame
    if header.get("INSTRUME") != ref_header.get("INSTRUME"):
        return frame
    if (header.get("NAXIS1"), header.get("NAXIS2")) != (ref_header.get("NAXIS1"), ref_header.get("NAXIS2")):
        return frame

    if frame_type == "Bias":
        exptime = 0.0
    elif frame_type == "Dark":
        raw_exptime = header.get("EXPTIME")
        if raw_exptime is None:
            return frame
        try:
            exptime = round(float(raw_exptime), 1)
        except (TypeError, ValueError):
            # Malformed/non-numeric EXPTIME (empty string, "N/A", an
            # astropy Undefined) -- treat like every other malformed-
            # header case above: skip just this one frame, never crash
            # the whole classify_tree/scan_session call. Mirrors
            # flat_sanity_notes()'s own guard around the equivalent
            # conversion in calibration.py.
            return frame
    else:
        exptime = 0.0

    filter_name: str | None = None
    if frame_type == "Flat":
        raw_filter = header.get("FILTER")
        if not raw_filter:
            return frame
        filter_name = str(raw_filter)

    return CalibrationFrame(
        path=path, telescope=telescope, frame_type=frame_type, binning=binning,
        exptime=exptime, filter_name=filter_name, source="header",
    )


def _apply_calibration_header_fallback(report: IngestReport, root: Path) -> None:
    """Mutates `report` in place: every remaining `unrecognized` entry
    gets one shot at header-based reclassification (Step 4b). Computed
    once per `classify_tree` call, not per-frame: which telescopes have
    ANY raw light at all (rule (c)), one reference raw light per
    (telescope, binning) (the cross-check), and which (telescope,
    frame_type) pairs are already filename-recognised (the shadowing
    rule) -- all from the filename pass that already ran above."""
    raw_telescopes = {f.telescope for f in report.lights if f.provenance == "raw"}
    raw_light_ref = _raw_light_reference(report)
    filename_recognized_types = {(f.telescope, f.frame_type) for f in report.calibration}

    still_unrecognized: list[UnrecognizedFrame] = []
    for frame in report.unrecognized:
        result = _classify_calibration_via_header(
            frame, root, raw_telescopes, raw_light_ref, filename_recognized_types,
        )
        if isinstance(result, CalibrationFrame):
            report.calibration.append(result)
        else:
            still_unrecognized.append(result)
    report.unrecognized = still_unrecognized


def classify_tree(root: str | Path, *, calibration_header_fallback: bool = False) -> IngestReport:
    """Pure, zip-aware classification (Step 4a, plan-flats-v4.md):
    behaviour-preserving refactor extracted out of `scan_session` so its
    fallback logic (Step 4b) is reviewable independently of this pure
    classifier, with an exact-equality gate between the two.

    Peeks lights inside zips BY NAME (`_peek_zipped_lights`, mirroring
    `index._scan_zip`) rather than extracting them -- extraction is a real
    filesystem side effect (writing into `_extracted_zips/`) that has no
    place in a "just classify what's here" function. `scan_session` is the
    only impure caller: it takes this report's zip-peeked placeholders and
    REPLACES them with their real, extracted-file equivalents.

    `calibration_header_fallback` (Step 4b, G4/G5): default False, so this
    function's own default behaviour is completely unchanged. When True,
    every file that no filename pattern matched AT ALL, plus every file
    that matched a telescope-less calibration pattern (skyflat/camera-
    model) with no telescope hint (both land in `report.unrecognized` from
    the filename pass above), gets one shot at header-based
    reclassification into a real `CalibrationFrame` with `source="header"`
    -- see `_classify_calibration_via_header`'s own docstring for the full
    rule set (IMAGETYP normalisation, CALSTAT/binning/exptime/filter
    handling, telescope attribution, the INSTRUME/NAXIS cross-check, and
    the shadowing rule). Lights are NEVER classified this way, whatever
    IMAGETYP says.
    """
    root = Path(root)
    report = IngestReport()

    seen: set[Path] = set()
    for pattern in FIT_GLOB_PATTERNS:
        for path in root.rglob(pattern):
            if path in seen or is_generated(path, root):
                continue
            seen.add(path)
            classified = classify_frame(path)
            if isinstance(classified, LightFrame):
                report.lights.append(classified)
            elif isinstance(classified, CalibrationFrame):
                report.calibration.append(classified)
            else:
                report.unrecognized.append(classified)

    for pattern in ARCHIVE_GLOB_PATTERNS:
        for path in root.rglob(pattern):
            if path in seen or is_generated(path, root):
                continue
            seen.add(path)
            peeked, real = _peek_zipped_lights(path)
            report.lights.extend(peeked)
            report.zip_peeks.extend(real)

    if calibration_header_fallback:
        _apply_calibration_header_fallback(report, root)

    return report


def scan_session(root: str | Path, *, calibration_header_fallback: bool = False) -> IngestReport:
    """`calibration_header_fallback` (Step 4b, plan-flats-v4.md): threaded
    straight through to `classify_tree`, unchanged from its own meaning
    there -- default False, so this function's own default behaviour is
    completely unchanged. Step 5 wires this up to `run_lrgb`/
    `run_narrowband`/`run_narrowband_boost`/the interview CLI.

    Zip-wrapped lights are extracted into the pipeline's own generated
    directory. That is deliberate, not incidental: is_generated() already
    excludes everything under GENERATED_DIRNAME from the raw-file scan
    `classify_tree` does, so the extracted copies can never be
    re-discovered as if they were additional raw deliveries on a second
    run (see is_generated's docstring for the real bug that exact mistake
    caused with staged calibration copies).

    Extracts directly from `classify_tree`'s own `report.zip_peeks` (the
    real `(zip_path, inner_name, fields)` tuples it already computed while
    building the zip-peeked placeholders) via `_materialize_zipped_light`,
    rather than re-opening every zip and re-running `classify_filename()`
    a second time here -- `classify_tree` already did both exactly once,
    per zip entry, building `report.lights`'s placeholders.
    """
    root = Path(root)
    extract_dir = root / GENERATED_DIRNAME / "_extracted_zips"

    report = classify_tree(root, calibration_header_fallback=calibration_header_fallback)

    # Replace (never append) each zip-peeked placeholder with its real
    # extracted-file equivalent -- see classify_tree/_peek_zipped_lights'
    # own docstrings for why appending both would double-count.
    report.lights = [
        light for light in report.lights if light.path.parent.suffix.lower() != ".zip"
    ]
    for zip_path, inner_name, fields in report.zip_peeks:
        report.lights.append(_materialize_zipped_light(zip_path, inner_name, fields, extract_dir))

    return report
