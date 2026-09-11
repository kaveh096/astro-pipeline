"""End-to-end LRGB orchestration for a single telescope+target, combining
every user who contributed data on that telescope.

Deliberately resumable at every step: each stage checks whether its output
already exists and skips if so. This machine has ~8GB RAM and long runs
have been killed mid-flight more than once, so a re-run must continue
rather than start over. It also means a stage can be deleted from
`_pipeline/` to force just that stage to recompute.

Stage order here reflects what was established empirically (see the
individual modules' docstrings), not the original design sketch:

    per (filter, binning): calibrate -> register+stack -> plate solve
    per colour contributor: align R/G/B -> crop -> rgbcomp -> GraXpert -> SPCC
    luminance:              GraXpert
    combine:                reproject each contributor onto L's grid,
                             average them together -> GHT both ->
                             rgbcomp -lum
    export:                 16-bit TIFF + faithful PNG preview

Colour processing happens at native (binned) resolution per contributor
and is reprojected up to L's grid only at the end -- reprojection
introduces NaN edges and interpolation artifacts that break Siril's star
photometry, so anything photometric (SPCC) must run before it.

Colour calibration is SPCC only. An earlier version supported PCC as well
(Siril's broadband method), selectable via a `colour_calibration`
parameter -- removed once SPCC was confirmed working (needs Siril >=
1.4.4) and preferred in every respect: no runtime dependency on VizieR,
and better colour, modelled from the actual sensor/filter spectral
response rather than inferred from broadband photometry. See
docs/colour-calibration-catalogues.md for the PCC-vs-SPCC comparison run
on real M51 data before PCC was dropped.

MULTI-USER / MULTI-CONTRIBUTOR COMBINING, added once real data showed two
users (collaborators sharing an iTelescope account arrangement) had
imaged the same target on the same telescope:

  Luminance combines at the RAW-SUB level within one (telescope, binning).
  Two users sharing a telescope and binning for the same filter (e.g. both
  shooting Luminance/BIN1) get their raw lights merged into ONE
  calibrate+register+stack call, via IngestReport.instrument_groups().
  This gives Siril's sigma-rejection visibility into every individual
  frame -- better outlier/satellite-trail rejection than averaging two
  independently-stacked masters would be. calibration_index() has no user
  dimension, so the shared bias/dark for that telescope+binning already
  applies correctly to a merged light list. A merge across DIFFERING
  exposure times (T21's real case: 300s/600s Luminance lights, single
  user) calibrates fine too -- calibration.select_dark() picks one dark
  master at the group's own binning and calibrate_lights() passes
  `-opt=exp`, which Siril applies as a PER-IMAGE scaling coefficient
  (verified against the real installed 1.4.4 binary), so a mixed-exptime
  merge needs no per-exptime bucketing. What still never happens is
  merging Luminance ACROSS telescopes -- T21 has a genuinely different
  pixel scale from T24, so its Luminance can never raw-sub-combine with
  T24's regardless of exptime; each telescope's Luminance gets its own
  master-level contributor instead (see the discovery loop in run_lrgb --
  every discovered telescope's worth of Luminance masters get built there).
  Which one actually drives the rendered composite is a MEASURED
  RECOMMENDATION, not an automatic rule (Slice 2): each contributor's
  median stellar FWHM is read off Siril's own per-frame registration data
  (`r_pp_lights_.seq`'s `R0` lines, see contributor_fwhm_arcsec()) and
  converted to arcsec against its own plate-solved master's pixel scale,
  the sharpest one wins by default, and every other Luminance master still
  gets built (so it's on disk and inspectable) but excluded from the
  composite -- see `lum_source` on run_lrgb for the explicit override.

  RGB combines at the MASTER level, across "contributors" -- one
  contributor per (telescope, binning) that has all three R/G/B present.
  Two different binnings (Kaveh's own BIN2 RGB vs a collaborator's BIN1
  RGB, both on T24) cannot be combined at the raw-sub level at all
  (different pixel scale/dimensions -- Siril's registration requires
  matching frames). Each contributor is independently aligned across its
  own R/G/B, background-extracted, and colour-calibrated (mirroring the
  single-contributor pipeline exactly), THEN reprojected onto the
  Luminance grid, THEN gain/offset-matched onto a reference contributor's
  flux scale (see reconciliation.match_gain_offset/fit_gain -- necessary
  because different-binning contributors sit at genuinely different flux
  scales, measured ~5x between BIN1/BIN2 on real M51 data), THEN averaged
  together (reconciliation.combine_same_grid, weighted by STACKCNT -- subs
  that actually survived quality filtering/rejection into each
  contributor's stacked masters, not raw sub count) into one final RGB.
  This is exactly the pattern reconciliation.py's own module docstring
  anticipated: "each instrument/user's data is independently calibrated,
  registered, and stacked first, and only the resulting masters get
  reprojected together."

  `rgb_binning` names the PRIMARY contributor (kept backward compatible
  with existing fixtures/tests: its files stay at the legacy top-level
  paths, e.g. final/rgb_native.fit). Any OTHER binning discovered for the
  same telescope+target+RGB-filters becomes an additional contributor
  under final/contrib_<telescope>_bin<n>/ (Slice 3: telescope-explicit,
  not just contrib_bin<n> -- a second telescope shooting the same
  non-primary binning would otherwise collide), and only actually runs if
  all three R/G/B are present for it -- a partial contributor (missing one
  channel) is logged and skipped (Slice 3: was a RuntimeError that
  aborted the whole run; see _build_colour_contributor), not silently
  built into an incomplete composite.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits

from .background_color import (
    INSTRUMENT_PROFILES,
    UnknownInstrumentError,
    run_graxpert_background_extraction,
    run_spcc,
)
from .calibration import (
    DEFAULT_PEDESTAL,
    CalibrationMode,
    FlatPolicy,
    select_dark,
    run_calibration,
    stage_precalibrated_lights,
)
from .export_image import ExportResult, export
from .ingest import CalibrationFrame, scan_session
from .checkpoints import Checkpoint, checkpoint, save_checkpoints, _pixel_scale_arcsec
from .reconciliation import (
    combine_same_grid,
    crop_to_common_coverage,
    match_gain_offset,
    reproject_to_reference,
)
from .registration_stacking import register_and_stack
from .run_signature import (
    STAGE_ORDER,
    ContributorSignature,
    RunSignature,
    cascade_from,
    diff_invalidation,
    frame_identity_hash,
    load_run_signature,
    save_run_signature,
)
from .siril_driver import run_script
from .solving import solve
from .stretch_compose import stretch_and_compose
from .workspace import group_name_for, pipeline_dir

RGB_FILTERS = ("Red", "Green", "Blue")
LUMINANCE_FILTER = "Luminance"


@dataclass
class PipelineResult:
    masters: dict[str, Path] = field(default_factory=dict)
    composite_path: Path | None = None
    export_result: ExportResult | None = None
    checkpoints: list[Checkpoint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _log(message: str, notes: list[str]) -> None:
    print(message, flush=True)
    notes.append(message)


def usable(path: Path, notes: list[str] | None = None) -> bool:
    """Is an existing stage output actually fit to resume from?

    Resumability that only checks os.path.exists trusts whatever is on disk,
    and that bit hard: a run interrupted mid-flight left a 100%-NaN
    background-extraction output behind, and the next run skipped the stage
    ("already done") and happily fed the garbage forward through three more
    stages. The guard that would have caught it lived inside the function
    that was skipped.

    So a skip must be earned: the file has to exist, parse, and contain
    real data.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        data = fits.getdata(path, memmap=False)
    except Exception as exc:
        if notes is not None:
            _log(f"[stale] {path.name} could not be read ({exc}); will regenerate", notes)
        return False
    nan_fraction = float(np.isnan(data).sum()) / data.size
    if nan_fraction > 0.5:
        if notes is not None:
            _log(f"[stale] {path.name} is {nan_fraction:.0%} NaN; will regenerate", notes)
        return False
    return True


def build_master(
    project_dir: Path,
    lights: list,
    cal_index: dict,
    group_name: str,
    filter_name: str,
    telescope: str,
    binning: int,
    ra_hours: float,
    dec_deg: float,
    notes: list[str],
    flat_frames: list[CalibrationFrame],
    flat_policy: FlatPolicy,
    pedestal: float = DEFAULT_PEDESTAL,
    calibration_mode: CalibrationMode = CalibrationMode.RAW_LOCAL,
) -> Path:
    """Calibrate -> register+stack -> plate solve one group of raw lights.

    `lights` may span multiple users -- raw-sub-level combining across
    collaborators is decided by the caller (see resolve_lights below), not
    here. Calibration and stacking don't care about user identity at all,
    only about telescope/binning/exptime matching the calibration frames.

    Exposure time is derived from the lights themselves (`{f.exptime for f
    in lights}`), not passed in -- every existing real group is
    single-exptime, so this changes nothing for them, but a mixed-exptime
    group (T21's real 600s/300s Luminance) now calibrates correctly
    instead of the caller having to guess a single exptime up front.

    `flat_frames`/`flat_policy` (Slice 2 of plan-flats-v3.md): unlike
    bias/dark (looked up here from `cal_index`, which has no filter
    dimension), the matched flat set genuinely depends on `filter_name`, so
    it is resolved by the CALLER via `report.flat_index().get((telescope,
    binning, filter_name), [])` -- this function has no `report` in scope
    on its own, only `cal_index` -- mirroring how bias/dark_selection.
    frames are already resolved by the caller before this function is
    invoked, not inside it. Threaded straight through to
    `run_calibration()`, unchanged from its own existing behaviour: an
    empty `flat_frames` under `FlatPolicy.REQUIRE` raises
    `CalibrationFramesMissingError`; under `FlatPolicy.SKIP_IF_MISSING` it
    proceeds without flat correction, same as before flats were wired in
    at all.

    Cost accepted, not silently ignored (Slice 2.2's own documented
    decision): this rebuilds the matched master flat fresh inside every
    group's own `run_calibration()` call below, exactly mirroring the
    existing, already-accepted redundancy where bias/dark masters are
    independently rebuilt per light group even when several groups share
    the same (telescope, binning). No shared-master caching is added for
    flats either here -- out of scope for this pass.

    `pedestal` (Slice 4.1) is threaded here rather than left at
    `run_calibration`'s own default: it is baked into every calibrated
    light BEFORE registration/stacking (see calibration.py), so it
    affects this master's actual pixels -- and `usable()`'s file-
    existence-only gate above has no way to notice a caller passed a
    different value on a resumed run. See run_signature.py's
    `RunSignature.contributor_stale`, which is what actually deletes a
    stale master when `pedestal` (or the matched flat set) changes,
    forcing this function to rebuild it rather than silently keep serving
    the old file.

    `calibration_mode` (precalibrated-path plan, 2026-09): RAW_LOCAL is
    today's only behaviour, unconditionally the default, byte-unchanged.
    PRECALIBRATED skips the bias/dark/flat block below entirely and stages
    already-calibrated (provenance="calibrated") lights directly via
    `stage_precalibrated_lights()` -- for a delivery with no local Dark
    frames at all because iTelescope already calibrated server-side (real
    case: NGC 3628/T73). `cal_index`/`flat_frames`/`flat_policy` are never
    read in that branch; callers pass `{}`/`[]`/`FlatPolicy.SKIP_IF_MISSING`
    as inert placeholders (see plan-precalibrated-path.md ??3.4). Everything
    from `register_and_stack(...)` onward is calibration-mode-agnostic --
    it only cares that a Siril sequence literally named "pp_lights_" exists
    in `work_dir / "lights"`, which both branches produce.
    """
    work_dir = pipeline_dir(project_dir) / group_name
    master_path = work_dir / "lights" / f"master_{filter_name.lower()}.fit"

    if usable(master_path, notes) and fits.getheader(master_path).get("PLTSOLVD"):
        _log(f"[skip] {group_name}: solved master already present", notes)
        return master_path

    light_exptimes = {f.exptime for f in lights}

    if calibration_mode == CalibrationMode.PRECALIBRATED:
        _log(
            f"[run ] {group_name}: staging {len(lights)} precalibrated lights "
            "(skip bias/dark/flat)",
            notes,
        )
        stage_precalibrated_lights(lights, work_dir, basename="lights", pedestal=pedestal, notes=notes)
    else:
        bias = cal_index[(telescope, "Bias", binning, 0.0)]
        dark_selection = select_dark(cal_index, telescope, binning, light_exptimes)

        exptimes_str = ", ".join(f"{e:.0f}s" for e in sorted(light_exptimes))
        scaling_note = (
            f", scaled from {dark_selection.exptime:.0f}s via -opt=exp" if dark_selection.scaled else ""
        )
        flat_note = f", {len(flat_frames)} {filter_name} flat" if flat_frames else ""
        _log(
            f"[run ] {group_name}: calibrating {len(lights)} lights at {exptimes_str} "
            f"({len(bias)} bias, {len(dark_selection.frames)} dark{scaling_note}{flat_note})",
            notes,
        )
        calibration_result = run_calibration(
            lights, bias, dark_selection.frames, work_dir=work_dir,
            flat_frames=flat_frames, flat_policy=flat_policy,
            dark_scaled=dark_selection.scaled, pedestal=pedestal, notes=notes,
        )
        if flat_frames:
            _log(
                f"       {group_name}: flat_corrected={calibration_result.flat_corrected} "
                f"(matched {len(flat_frames)} {filter_name} flat frame(s))",
                notes,
            )

    n = len(lights)
    filter_fwhm_pct = 90.0 if n >= 10 else None
    filter_round_pct = 90.0 if n >= 10 else None
    if n < 10:
        _log(
            f"       {group_name}: only {n} lights, disabling FWHM/roundness "
            "quality filtering (not enough subs for a percentile cut to be meaningful)",
            notes,
        )
    norm = "addscale" if len(light_exptimes) > 1 else None

    _log(f"[run ] {group_name}: register + stack", notes)
    stack_result = register_and_stack(
        "pp_lights_", work_dir / "lights", out_name=f"master_{filter_name.lower()}",
        filter_fwhm_pct=filter_fwhm_pct, filter_round_pct=filter_round_pct, norm=norm,
    )

    _log(f"[run ] {group_name}: plate solve", notes)
    solve(stack_result.master_path, ra_hours=ra_hours, dec_deg=dec_deg, search_radius_deg=5.0)

    stackcnt = fits.getheader(stack_result.master_path).get("STACKCNT", n)
    _log(f"       {group_name}: STACKCNT={stackcnt}", notes)
    return stack_result.master_path


def contributor_fwhm_arcsec(
    master_path: Path, notes: list[str], label: str | None = None
) -> float | None:
    """Median stellar FWHM, in arcsec, for one build_master() output --
    the sharpness figure Slice 2's Luminance-source selection is measured
    against (see run_lrgb).

    A sibling accessor rather than a change to build_master's own return
    type: build_master's plain Path return is left alone (its colour-
    contributor call sites are unrelated to this slice), and this reads
    the same on-disk outputs independently, given nothing but the master
    Path build_master already handed back.

    Mechanics, verified against real T21 and T24 fixture data (not
    assumed from the plan): Siril's `register` step -- which always runs
    immediately before `stack`, see registration_stacking.py -- writes its
    own per-frame FWHM into `r_pp_lights_.seq`, one `R0 <fwhm_x> <fwhm_y>
    <roundness> ...` line per SELECTED frame (confirmed: T21's real file
    has exactly 2 R0 lines for its 2-sub group; T24's real file has
    exactly 20 for its 20-sub group, matching `nb_selected` in each file's
    own header line). That file sits at `master_path.parent /
    "r_pp_lights_.seq"` on both a fresh and a resumed run -- confirmed
    present in every real Luminance work_dir/lights/ on disk. Median (not
    mean) of the first field across every R0 line, so one bad frame's
    outlier FWHM doesn't skew the figure the way an average would.

    The `.seq` file's FWHM is in PIXELS of the pre-solve `pp_`/`r_pp_`
    frames it measured, and those frames carry no WCS of their own (see
    module docstring) -- so the pixel scale has to come from the frame
    that DOES have one: the plate-solved MASTER this contributor became
    (same pixel grid as the frames it was measured on; stacking and plate
    solving don't resample), read via checkpoints._pixel_scale_arcsec(),
    which already does exactly this WCS-header-to-arcsec/px conversion for
    checkpoint statistics.

    Returns None, never raises, if the .seq file is missing/empty, has no
    R0 lines, or the master's header has no usable WCS -- a missing
    sharpness figure should degrade Slice 2's selection (see run_lrgb's
    fallback), not take down the whole run.
    """
    label = label or master_path.parent.parent.name
    seq_path = master_path.parent / "r_pp_lights_.seq"
    if not seq_path.exists():
        _log(f"       {label}: no {seq_path.name} found -- cannot measure FWHM", notes)
        return None

    fwhm_px: list[float] = []
    for line in seq_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("R0 "):
            continue
        fields = line.split()
        try:
            fwhm_px.append(float(fields[1]))
        except (IndexError, ValueError):
            continue
    if not fwhm_px:
        _log(f"       {label}: {seq_path.name} has no R0 (FWHM) lines -- cannot measure FWHM", notes)
        return None

    pixel_scale = _pixel_scale_arcsec(fits.getheader(master_path))
    if pixel_scale is None:
        _log(f"       {label}: master has no usable WCS -- cannot convert FWHM to arcsec", notes)
        return None
    return float(np.median(fwhm_px)) * pixel_scale


LumCandidate = tuple[tuple[str, int], Path, float | None]


def select_luminance_source(
    candidates: list[LumCandidate],
    lum_source: tuple[str, int] | None,
    fallback_key: tuple[str, int],
    notes: list[str],
) -> LumCandidate:
    """Pick which already-built Luminance contributor drives the composite,
    and log why every other one didn't -- Slice 2's measured recommendation,
    factored out of run_lrgb as its own function (side effect: log lines
    only) so the selection rule itself is directly testable without
    invoking the full Siril/GraXpert/SPCC pipeline that builds `candidates`
    in the first place.

    Rule, in order:
    1. `lum_source`, if given, always wins -- an explicit human override,
       logged as such (not a measured choice). Raises ValueError if it
       names a combo that isn't actually among `candidates` (Slice 1's
       discovery would have to have missed it, or the caller made a typo --
       either way, silently ignoring the override would be worse).
    2. Otherwise, the candidate with the lowest measured FWHM wins ("lowest
       FWHM" = sharpest), logged with the number that decided it.
    3. If NOTHING could be measured (every contributor's `.seq` file was
       missing or unreadable), fall back to `fallback_key` -- the caller's
       own (telescope, lum_binning), i.e. the pre-Slice-2 behaviour --
       rather than raising, since a missing sharpness figure is a real but
       survivable degraded mode (see contributor_fwhm_arcsec).
    """
    if lum_source is not None:
        match = next((c for c in candidates if c[0] == lum_source), None)
        if match is None:
            raise ValueError(
                f"lum_source={lum_source!r} was not among the discovered Luminance "
                f"contributors {[c[0] for c in candidates]}"
            )
        selected = match
        _log(
            f"[run ] Luminance source: {selected[0][0]}-bin{selected[0][1]} selected via "
            "EXPLICIT OVERRIDE (lum_source=...) -- not a measured choice",
            notes,
        )
    else:
        measured = [c for c in candidates if c[2] is not None]
        if measured:
            selected = min(measured, key=lambda c: c[2])
            _log(
                f"[run ] Luminance source: {selected[0][0]}-bin{selected[0][1]} selected as "
                f"sharpest (FWHM {selected[2]:.2f}\")",
                notes,
            )
        else:
            fallback = next((c for c in candidates if c[0] == fallback_key), None)
            selected = fallback or candidates[0]
            _log(
                f"[warn] no Luminance contributor FWHM could be measured; falling back to "
                f"{selected[0][0]}-bin{selected[0][1]}",
                notes,
            )

    for key, _path, fwhm in candidates:
        if key == selected[0]:
            continue
        if fwhm is not None and selected[2] is not None:
            reason = f"FWHM {fwhm:.2f}\" vs selected {selected[2]:.2f}\""
        else:
            reason = "not selected"
        _log(
            f"       Luminance-{key[0]}-bin{key[1]}: master built but NOT selected for the "
            f"composite ({reason})",
            notes,
        )

    return selected


def discover_luminance_contributors(
    report, target: str, telescope: str, lum_binning: int
) -> list[tuple[str, int]]:
    """Every (telescope, binning) with Luminance data for `target`, across
    ALL telescopes -- not scoped to `telescope` -- so a telescope that only
    ever contributes colour under Kaveh's colour-only rule (T21 today)
    still gets its own Luminance master built (Slice 2 will select among
    these; this only discovers them). Mirrors exactly how RGB binning
    discovery already handles "caller-supplied but not necessarily
    discovered first" (see run_lrgb's `rgb_binnings` below): sort every
    discovered (telescope, binning), then prepend the caller's own
    (telescope, lum_binning) only if it wasn't already found -- so the
    single-contributor case (T24-only data) returns exactly
    `[(telescope, lum_binning)]`, unchanged from today.
    """
    contributors = sorted(
        {
            (key[0], key[3])
            for key in report.instrument_groups()
            if key[1] == target and key[2] == LUMINANCE_FILTER
        }
    )
    if (telescope, lum_binning) not in contributors:
        contributors = [(telescope, lum_binning), *contributors]
    return contributors


def resolve_lights(
    report,
    telescope: str,
    target: str,
    filter_name: str,
    binning: int,
    calibration_mode: CalibrationMode = CalibrationMode.RAW_LOCAL,
) -> tuple[list, str]:
    """Look up the lights for one (telescope, target, filter, binning)
    unit -- merged across every user who contributed to it (see
    IngestReport.instrument_groups()) -- and derive a stable, descriptive
    group name from whoever those users turn out to be.

    A single-contributor case (only one user shot this filter/binning)
    collapses to exactly the old per-user group name, so existing
    fixtures/tests that assume e.g. "T24-kaveh096-M51-Red-bin2" keep
    working unchanged.

    `calibration_mode` (precalibrated-path plan, 2026-09): RAW_LOCAL (the
    default, byte-unchanged) looks up `report.instrument_groups()` (raw
    provenance only). PRECALIBRATED looks up
    `report.calibrated_instrument_groups()` instead -- the mirror view
    over "calibrated" provenance -- for a telescope whose lights are used
    directly, without local bias/dark/flat calibration.
    """
    groups = (
        report.calibrated_instrument_groups()
        if calibration_mode == CalibrationMode.PRECALIBRATED
        else report.instrument_groups()
    )
    lights = groups.get((telescope, target, filter_name, binning), [])
    users = sorted({frame.user for frame in lights})
    group_name = group_name_for(telescope, "+".join(users), target, filter_name, binning)
    return lights, group_name


@dataclass
class ColourContributor:
    """One (telescope, binning) RGB contributor, once built -- Slice 3's
    naming fix (3.3) means downstream code identifies a contributor by
    this, not by its position in a loop (see run_lrgb's combine-multiple-
    contributors branch and its earlier positional `contrib{i}` bug)."""

    telescope: str
    binning: int
    composite_path: Path
    sub_count: int
    stack_total: int

    @property
    def key(self) -> str:
        """Stable identity string for filenames/logging, e.g. 'T24_bin1'."""
        return f"{self.telescope}_bin{self.binning}"


def resolve_instrument_profile(telescope: str):
    """SPCC's InstrumentProfile for `telescope`, or raise -- never guess.

    Factored out of _build_colour_contributor as its own function (Slice
    3.1) so this safety check is directly unit-testable without invoking
    the full calibrate/stack/solve/rgbcomp/GraXpert chain that runs before
    it in the real pipeline. Previously
    `INSTRUMENT_PROFILES.get(telescope, T24_PROFILE)` silently mis-
    profiled ANY unrecognized telescope as T24's KAF16803/Astrodon
    sensor+filters -- SPCC models the actual spectral response of the
    optical train that produced the data, so a wrong profile doesn't fail
    loudly, it just produces a plausible-looking, physically wrong colour
    solution. Raises UnknownInstrumentError instead.
    """
    profile = INSTRUMENT_PROFILES.get(telescope)
    if profile is None:
        raise UnknownInstrumentError(
            f"No SPCC InstrumentProfile registered for telescope {telescope!r} "
            f"(known: {sorted(INSTRUMENT_PROFILES)}) -- refusing to guess a "
            "sensor/filter profile for colour calibration. Register an "
            f"InstrumentProfile for {telescope!r} in INSTRUMENT_PROFILES first."
        )
    return profile


def infer_flat_policy(report, telescope: str) -> FlatPolicy:
    """Slice 2.2's default `FlatPolicy` for `telescope`, inferred from
    whether it ships ANY flat at all -- not hardcoded by telescope name.

    Deliberately mirrors this project's existing preference
    (resolve_instrument_profile, above) for refusing to special-case a
    telescope by literal name where the data itself already says what's
    needed -- though this is a distinct mechanism (inferred from presence
    in `report.flat_index()`, not a lookup into a curated registry like
    `INSTRUMENT_PROFILES`; stated plainly so the two aren't conflated).

    `FlatPolicy.REQUIRE` if `flat_index()` has any entry at all for this
    telescope (at any binning/filter): a telescope that ships flats for
    SOME filters but is then missing one a light group actually needs is a
    real, surfaceable gap (raises `CalibrationFramesMissingError` via
    `run_calibration`), not something to silently skip.
    `FlatPolicy.SKIP_IF_MISSING` if this telescope has zero flats of any
    kind -- T24's permanent, real situation for this delivery (see
    plan-flats-v3.md's "Context" section: T24 structurally never ships
    flats here).

    Overridable uniformly across every telescope in a run via
    `run_lrgb`'s own `flat_policy` parameter -- see there; this function
    only computes the per-telescope DEFAULT, before any override is
    applied.
    """
    has_any_flat = any(key[0] == telescope for key in report.flat_index())
    return FlatPolicy.REQUIRE if has_any_flat else FlatPolicy.SKIP_IF_MISSING


def infer_calibration_mode(report, telescope: str) -> CalibrationMode:
    """Precalibrated-path plan's default `CalibrationMode` for `telescope`,
    mirroring `infer_flat_policy`'s own pattern: inferred from what the
    data actually has, not hardcoded by telescope name, with an explicit
    per-telescope override escape hatch in `run_lrgb`.

    `CalibrationMode.PRECALIBRATED` if `telescope` has zero Bias-or-Dark
    frames of any kind (raw+local calibration structurally cannot work --
    `run_calibration` would raise `CalibrationFramesMissingError` before
    ever reaching registration) AND has at least one "calibrated"-
    provenance light group (there is something to fall back to). Real
    case: NGC 3628/T73 (Feb 2025) -- zero recognized Bias/Dark, both
    raw- and calibrated- provenance copies of every light delivered.

    `CalibrationMode.RAW_LOCAL` otherwise -- unconditionally the default
    for every telescope with real Bias+Dark (T24, T21), unchanged from
    before this function existed. A delivery with neither local
    bias/dark NOR any calibrated-provenance lights also resolves
    RAW_LOCAL, deliberately: that combination has nothing this function
    can safely infer its way out of, so it stays on the path that fails
    loudly (`CalibrationFramesMissingError`) instead of silently
    resolving to a mode with no real data behind it either.
    """
    cal_index = report.calibration_index()
    has_bias = any(key[0] == telescope and key[1] == "Bias" for key in cal_index)
    has_dark = any(key[0] == telescope and key[1] == "Dark" for key in cal_index)
    has_calibrated_lights = any(
        key[0] == telescope for key in report.calibrated_instrument_groups()
    )
    if (not has_bias or not has_dark) and has_calibrated_lights:
        return CalibrationMode.PRECALIBRATED
    return CalibrationMode.RAW_LOCAL


def contributor_dir(final: Path, telescope: str, binning: int, rgb_binning: int) -> Path:
    """Where one RGB contributor's per-binning files live (Slice 3.3).

    The PRIMARY contributor (`binning == rgb_binning`) keeps the legacy
    top-level `final` directory itself -- e.g. final/rgb_native.fit --
    kept backward compatible with existing fixtures/tests and so this
    naming fix doesn't trigger the hours-of-recompute a full rename of the
    primary's paths would (see usable()'s resume gating). Any OTHER
    binning gets its own telescope-explicit subdirectory:
    `contrib_<telescope>_bin<n>`, not just `contrib_bin<n>` -- the old,
    telescope-blind name that would collide the moment a second telescope
    contributes the same non-primary binning. A pure function of
    (telescope, binning, rgb_binning), not of loop position, so it is
    directly testable and stable across however run_lrgb's discovery
    order changes (see test_pipeline.py).
    """
    if binning == rgb_binning:
        return final
    return final / f"contrib_{telescope}_bin{binning}"


def _build_colour_contributor(
    project_dir: Path,
    contrib_dir: Path,
    report,
    telescope: str,
    target: str,
    binning: int,
    ra_hours: float,
    dec_deg: float,
    notes: list[str],
    flat_policy: FlatPolicy = FlatPolicy.SKIP_IF_MISSING,
    calibration_mode: CalibrationMode = CalibrationMode.RAW_LOCAL,
) -> ColourContributor | None:
    """Build one binning's R/G/B masters, align + crop + composite them,
    then background-extract and colour-calibrate -- everything the
    single-contributor pipeline always did, just namespaced under
    `contrib_dir` so multiple binnings can coexist.

    Returns None -- logging why, rather than raising -- if any of R/G/B is
    missing for this (telescope, binning): a colour contributor needs all
    three, but a partial set is a real, survivable state once RGB
    discovery broadens beyond one telescope (this function's own
    behaviour is scoped narrowly here; broadening the discovery loop
    itself is a separate, later concern -- see run_lrgb). Before Slice 3
    this raised RuntimeError and aborted the entire run over one missing
    filter on one binning; that's disproportionate once a partial
    contributor is an expected, not exceptional, outcome.

    `flat_policy` (Slice 2.2 of plan-flats-v3.md): threaded in from
    run_lrgb's own per-telescope inference/override (see
    infer_flat_policy) -- this function itself has no telescope-discovery
    context of its own, it just applies whatever policy the caller
    decided. Defaults to SKIP_IF_MISSING (the safe, pre-Slice-2 behaviour)
    so a caller that doesn't pass one explicitly still proceeds without a
    hard flat requirement, matching every real colour-contributor
    telescope's situation before this slice.

    `calibration_mode` (precalibrated-path plan, 2026-09): threaded in
    from run_lrgb's own per-telescope inference/override (see
    infer_calibration_mode), same pattern as `flat_policy`. Under
    PRECALIBRATED, `resolve_lights()` looks up calibrated-provenance
    groups instead of raw ones, flat lookup is skipped entirely (never
    consulted for a precalibrated group -- the frames are already
    flat-corrected upstream, see stage_precalibrated_lights()'s
    docstring), and `build_master()` is called with placeholder
    `cal_index={}`/`flat_frames=[]`/`flat_policy=FlatPolicy.SKIP_IF_MISSING`
    (never read inside its PRECALIBRATED branch).
    """
    contrib_dir.mkdir(parents=True, exist_ok=True)
    cal_index = report.calibration_index()

    channel_masters: dict[str, Path] = {}
    sub_count = 0
    stack_total = 0
    for filter_name in RGB_FILTERS:
        lights, group_name = resolve_lights(
            report, telescope, target, filter_name, binning, calibration_mode=calibration_mode
        )
        if not lights:
            _log(
                f"[skip] BIN{binning}: no {filter_name} lights found for {telescope}/{target} "
                "-- a colour contributor needs all three of Red/Green/Blue; skipping this "
                "contributor rather than aborting the whole run",
                notes,
            )
            return None
        sub_count += len(lights)
        if calibration_mode == CalibrationMode.PRECALIBRATED:
            master_path = build_master(
                project_dir, lights, {}, group_name, filter_name,
                telescope, binning, ra_hours, dec_deg, notes,
                flat_frames=[], flat_policy=FlatPolicy.SKIP_IF_MISSING,
                calibration_mode=calibration_mode,
            )
        else:
            flat_frames = report.flat_index().get((telescope, binning, filter_name), [])
            master_path = build_master(
                project_dir, lights, cal_index, group_name, filter_name,
                telescope, binning, ra_hours, dec_deg, notes,
                flat_frames=flat_frames, flat_policy=flat_policy,
            )
        channel_masters[filter_name] = master_path
        stack_total += int(fits.getheader(master_path).get("STACKCNT", len(lights)))

    rgb_native = contrib_dir / "rgb_native.fit"
    if not usable(rgb_native, notes):
        # Each filter was registered against its OWN reference frame, so the
        # three masters do not share a pointing -- and `rgbcomp` stacks them
        # straight into R/G/B channels without aligning anything. Measured on
        # real data, Green sat 8.8px from Red and Blue 4.4px, comparable to
        # the stars' own ~5-9px FWHM, which showed up as red/green fringing
        # on every star in the checkpoint preview. Reproject the other two
        # onto Red's grid first so the channels actually correspond.
        reference = channel_masters["Red"]
        shutil.copy2(reference, contrib_dir / "red.fit")
        for filter_name in ("Green", "Blue"):
            out_path = contrib_dir / f"{filter_name.lower()}.fit"
            recon = reproject_to_reference(channel_masters[filter_name], reference, out_path)
            _log(
                f"[run ] BIN{binning}: aligned {filter_name} onto Red's grid "
                f"(footprint {recon.footprint_mean:.3f})",
                notes,
            )
        # Crop away the slivers alignment left uncovered rather than filling
        # them -- a constant fill is visible to GraXpert's background model
        # and produced a green band across the finished image. See
        # crop_to_common_coverage.
        channel_paths = [contrib_dir / f"{f.lower()}.fit" for f in RGB_FILTERS]
        crop_to_common_coverage(channel_paths, contrib_dir)
        cropped_shape = fits.getdata(channel_paths[0], memmap=False).shape
        _log(f"[run ] BIN{binning}: cropped channels to common coverage {cropped_shape}", notes)

        _log(f"[run ] BIN{binning}: rgbcomp at native resolution", notes)
        run_script(["rgbcomp red green blue -out=rgb_native"], workdir=contrib_dir)
    else:
        _log(f"[skip] BIN{binning}: rgb_native.fit already present", notes)

    rgb_bg = contrib_dir / "rgb_native_bg.fits"
    if not usable(rgb_bg, notes):
        _log(f"[run ] BIN{binning}: GraXpert background extraction on RGB", notes)
        rgb_bg = run_graxpert_background_extraction(rgb_native, output_stem="rgb_native_bg")
    else:
        _log(f"[skip] BIN{binning}: RGB background extraction already done", notes)

    colour_calibrated = contrib_dir / "rgb_colour_calibrated.fit"
    if not usable(colour_calibrated, notes):
        # Do the work on a temporary name and only move it into place once
        # it succeeds -- see module docstring / commit history: copying the
        # input to the final name up-front and processing in place leaves a
        # valid-looking file behind when the stage fails, and `usable()`
        # cannot tell the difference, since the data IS intact, it simply
        # has not been transformed. Existence must mean completion.
        staging = contrib_dir / "rgb_colour_calibrated__inprogress.fit"
        shutil.copy2(rgb_bg, staging)
        profile = resolve_instrument_profile(telescope)
        _log(f"[run ] BIN{binning}: SPCC colour calibration ({profile.mono_sensor}, local Gaia)", notes)
        solution = run_spcc(staging, contrib_dir, profile=profile)
        os.replace(staging, colour_calibrated)
        _log(
            f"       BIN{binning} SPCC used {solution.stars_used} stars, "
            f"white balance {solution.white_balance}",
            notes,
        )
    else:
        _log(f"[skip] BIN{binning}: SPCC already done", notes)

    return ColourContributor(
        telescope=telescope, binning=binning, composite_path=colour_calibrated,
        sub_count=sub_count, stack_total=stack_total,
    )


# --- Slice 4.1: run-signature helpers ---------------------------------------
# Factored out of run_lrgb as their own functions for the same reason
# select_luminance_source/resolve_instrument_profile were: directly
# testable without invoking the full calibrate/stack/solve/SPCC chain.


def _delete_if_exists(path: Path, reason: str, notes: list[str]) -> None:
    """Delete one stage-output file so usable()'s existing skip-if-present
    gate naturally regenerates it -- the actual mechanism run-signature
    invalidation uses (see run_signature.py's module docstring: no second,
    parallel gating system)."""
    path = Path(path)
    if path.exists():
        _log(f"[run ] {path.name}: {reason} -- deleting to force regeneration", notes)
        path.unlink()


def _colour_contributor_frame_hash(report, telescope: str, target: str, binning: int) -> str:
    """One hash for a colour contributor's entire R+G+B light set -- a
    change to ANY of its three filters' subs (not just one) must be
    caught, since all three feed the one rgbcomp'd composite."""
    names: list[str] = []
    for filter_name in RGB_FILTERS:
        lights, _ = resolve_lights(report, telescope, target, filter_name, binning)
        names.extend(f.path.name for f in lights)
    return frame_identity_hash(names)


def _flat_frame_hash(flat_frames: list) -> str:
    """Slice 2.3's flat-set hashing rule: "" for an EMPTY matched flat set,
    not `frame_identity_hash([])`'s own real (non-empty) hash-of-an-empty-
    string constant.

    This matters concretely, not just cosmetically: T24 has zero flats of
    any kind in this delivery, structurally, permanently (see plan-flats-
    v3.md's Context section) -- a persisted `run_signature.json` predating
    `flat_frame_hash` reads it back as "" (the field's own default, see
    ContributorSignature). If a freshly-computed "no flats matched" value
    were instead `frame_identity_hash([])` (a real, fixed hash string, NOT
    ""), it would mismatch that persisted "" and mark T24's entire colour
    contributor stale -- forcing a full rebuild of its raw R/G/B masters,
    GraXpert, and SPCC -- on literally every run's FIRST comparison after
    this field was added, even though T24's actual flat situation never
    changed at all. That is a materially bigger, unintended one-time cost
    than the plan's own Slice 2.3 section describes (which names only the
    real T21_bin1 Luminance entry as the expected one-time rebuild).
    Keeping "no flats" as a stable "" on both sides avoids it, while a
    genuinely non-empty matched flat set still goes through the real
    `frame_identity_hash` mechanism -- an actual flat-set CHANGE (a
    re-shot flat, added/removed frames) is still caught exactly as before.
    """
    if not flat_frames:
        return ""
    return frame_identity_hash([f.path.name for f in flat_frames])


def _colour_contributor_flat_frame_hash(report, telescope: str, binning: int) -> str:
    """Slice 2.3's flat-aware sibling of _colour_contributor_frame_hash
    above: one hash for a colour contributor's entire matched flat set
    across R+G+B -- a change to ANY of the three filters' matched flats
    (a flat re-shot, a new filter's flats added) must be caught, since all
    three feed the one rgbcomp'd composite exactly like the light set
    does. See _flat_frame_hash for why an entirely empty matched flat set
    (T24's real, permanent situation) hashes to "" rather than
    frame_identity_hash([])'s own non-empty constant."""
    names: list[str] = []
    for filter_name in RGB_FILTERS:
        flats = report.flat_index().get((telescope, binning, filter_name), [])
        names.extend(f.path.name for f in flats)
    if not names:
        return ""
    return frame_identity_hash(names)


def _clear_colour_contributor_products(
    contrib_dir: Path,
    project_dir: Path,
    report,
    telescope: str,
    target: str,
    binning: int,
    calibration_mode: CalibrationMode = CalibrationMode.RAW_LOCAL,
) -> list[Path]:
    """Delete one colour contributor's raw R/G/B masters AND its own
    build products (rgb_native.fit onward), so _build_colour_contributor
    rebuilds it cleanly from scratch rather than mixing freshly-rebuilt
    masters with stale downstream products from the old ones. Returns the
    paths actually deleted, for logging by the caller (which knows the
    human-readable BIN{n} label this function doesn't).

    `calibration_mode` mirrors _build_colour_contributor's own parameter
    (precalibrated-path plan) so the group_name/master_path computed here
    matches whichever provenance _build_colour_contributor will actually
    rebuild from."""
    deleted: list[Path] = []
    for name in (
        "rgb_native.fit", "red.fit", "green.fit", "blue.fit",
        "rgb_native_bg.fits", "rgb_colour_calibrated.fit",
    ):
        candidate = contrib_dir / name
        if candidate.exists():
            candidate.unlink()
            deleted.append(candidate)
    for filter_name in RGB_FILTERS:
        _, group_name = resolve_lights(
            report, telescope, target, filter_name, binning, calibration_mode=calibration_mode
        )
        master_path = pipeline_dir(project_dir) / group_name / "lights" / f"master_{filter_name.lower()}.fit"
        if master_path.exists():
            master_path.unlink()
            deleted.append(master_path)
    return deleted


def run_lrgb(
    project_dir: str | Path,
    telescope: str,
    target: str,
    ra_hours: float,
    dec_deg: float,
    lum_binning: int = 1,
    rgb_binning: int = 2,
    stretch_method: str = "autostretch",
    lum_source: tuple[str, int] | None = None,
    pedestal: float = DEFAULT_PEDESTAL,
    stop_after: str | None = None,
    force: set[str] | None = None,
    flat_policy: FlatPolicy | None = None,
    calibration_mode: dict[str, CalibrationMode] | None = None,
) -> PipelineResult:
    """`lum_source`, if given, names an explicit `(telescope, binning)`
    among the discovered Luminance contributors to drive the composite --
    overriding Slice 2's default sharpest-wins measurement. This is
    deliberately a direct parameter, not an automatic override rule: a
    fully-automatic sharpness rule is genuinely underspecified in real
    edge cases (same telescope shot at two binnings; a near-tie), so the
    project's own checkpointed-not-black-box design principle keeps the
    final call available to a human (Slice 4's skill wraps this as a real
    menu choice; here it's just an argument). Raises ValueError if the
    named combo wasn't actually discovered for this target.

    `pedestal` (Slice 4.1) is the constant added to every calibrated
    light before registration/stacking (see calibration.py's module
    docstring for why this exists at all) -- promoted here from a
    calibration.py-internal default to a real, resume-guarded `run_lrgb`
    argument, per plan-rev4.md's Slice 4.1: it changes every master's
    actual pixels, so a caller passing a different value on a resumed run
    must not silently get back a master built under the OLD value just
    because `usable()` sees an existing file.

    `stop_after` (Slice 4.3) ends the call early, honestly, against this
    function's REAL control flow -- not against the 5 checkpoint labels,
    which are not phase boundaries (`02_primary_rgb_colour_calibrated`
    completes only after every colour contributor is built, since RGB
    binnings are processed in sorted() order and the primary is whichever
    binning the caller named, not necessarily processed first). One of:

        "masters"     -- Luminance selected + every colour contributor
                          built. Returns with `result.masters` populated,
                          no reconciliation/stretch/export attempted.
        "reconciled"  -- the above, plus L background extraction and
                          (when there is more than one colour contributor)
                          reprojection/gain-match/combine. A no-op boundary
                          for a single-contributor run -- no combine ever
                          happens for it, so this stops in the same place
                          "masters" would other than L's background
                          extraction.
        "final"       -- (the default, via `stop_after=None`) everything,
                          i.e. today's full run.

    Returns without raising, with no partial/broken files -- whatever
    stages were actually reached get their checkpoints emitted (Slice
    4.2 moved checkpoint emission inline, per-stage, specifically so a
    `stop_after`-terminated call still leaves an inspectable
    `checkpoints.json` behind).

    `force` (Slice 4.3) names stages -- from the same
    `{"masters", "reconciled", "final"}` vocabulary -- whose outputs
    should be deleted/invalidated before running, even if `usable()`
    would otherwise skip them, matching 4.1's dependency order:
    `force={"masters"}` also invalidates "reconciled" and "final" (they
    are derived from masters), not just "masters" itself -- see
    run_signature.cascade_from, which both `force` and the automatic
    run-signature-mismatch check below go through.

    `flat_policy` (Slice 2.2 of plan-flats-v3.md), when given, overrides
    the per-telescope default computed by `infer_flat_policy` (REQUIRE if
    a telescope ships ANY flat at all, else SKIP_IF_MISSING) UNIFORMLY for
    every telescope discovered in this run -- a narrower override than
    per-telescope, but the common case ("force skip everywhere for a
    quick test run") doesn't need per-telescope granularity, and
    `lum_source`-style per-contributor overrides already exist as the
    escape hatch pattern for anything finer. Left as None (the default),
    each telescope gets its own inferred policy: T21 (ships flats for all
    11 filters at BIN1 in this delivery) defaults to REQUIRE, T24 (ships
    none) defaults to SKIP_IF_MISSING.

    `calibration_mode` (precalibrated-path plan, 2026-09), when given, is a
    PER-TELESCOPE override dict (`{telescope: CalibrationMode}`) -- unlike
    `flat_policy`'s single uniform override, precalibrated-vs-raw is
    fundamentally a per-telescope data-availability fact, not a
    quick-test-override convenience. A telescope not present in the dict
    (or when `calibration_mode` is left `None`) gets its own inferred mode
    from `infer_calibration_mode` -- PRECALIBRATED only if this telescope
    has zero local Bias-or-Dark frames of any kind AND has calibrated-
    provenance lights to fall back to (real case: NGC 3628/T73); every
    telescope with real Bias+Dark (T24, T21) is unaffected, unconditionally
    RAW_LOCAL.
    """
    if stop_after is not None and stop_after not in STAGE_ORDER:
        raise ValueError(f"stop_after={stop_after!r} is not one of {STAGE_ORDER}")
    force = set(force) if force else set()
    unknown_force = force - set(STAGE_ORDER)
    if unknown_force:
        raise ValueError(f"force={sorted(unknown_force)} names unknown stage(s); valid: {STAGE_ORDER}")

    project_dir = Path(project_dir)
    out = pipeline_dir(project_dir)
    final = out / "final"
    final.mkdir(parents=True, exist_ok=True)
    result = PipelineResult()
    notes = result.notes

    checkpoint_dir = out / "checkpoints"
    checkpoints_path = checkpoint_dir / "checkpoints.json"
    run_signature_path = out / "run_signature.json"
    old_signature = load_run_signature(run_signature_path)

    _log(f"=== scanning {project_dir.name} ===", notes)
    report = scan_session(project_dir)

    # --- luminance masters: every (telescope, binning) that has Luminance
    # data for this target gets its own master built here -- NOT scoped to
    # the caller's `telescope`, otherwise a telescope that only ever
    # contributes colour under Kaveh's colour-only rule (T21 today) would
    # never get its own Luminance master built under any slice, including
    # this one. Each user sharing a (telescope, binning) is still merged at
    # the raw-sub level (see module docstring) -- that combining logic is
    # per-contributor, unaffected by there now being more than one
    # contributor.
    #
    # Which contributor actually drives the rendered composite is Slice 2's
    # measured recommendation: every contributor's master gets built (so
    # every one is on disk and inspectable, even the ones not selected --
    # per this project's checkpointed-not-black-box design principle), its
    # median FWHM gets measured and logged, and the sharpest one wins by
    # default. This is deliberately NOT folded into the build loop below --
    # the winner can only be known after every candidate has been measured.
    cal_index = report.calibration_index()
    lum_contributors = discover_luminance_contributors(report, target, telescope, lum_binning)
    force_masters = "masters" in force

    lum_candidates: list[LumCandidate] = []
    lum_frame_hashes: dict[str, str] = {}
    lum_flat_frame_hashes: dict[str, str] = {}
    lum_stackcnt: dict[str, int] = {}
    lum_calibration_modes: dict[str, str] = {}
    for lum_telescope, contrib_lum_binning in lum_contributors:
        lum_key = (lum_telescope, contrib_lum_binning)
        lum_label = f"Luminance-{lum_telescope}-bin{contrib_lum_binning}"
        contributor_key = f"{lum_telescope}_bin{contrib_lum_binning}"
        lum_calibration_mode = (calibration_mode or {}).get(
            lum_telescope, infer_calibration_mode(report, lum_telescope)
        )
        lum_calibration_modes[contributor_key] = lum_calibration_mode.value
        lum_lights, lum_group_name = resolve_lights(
            report, lum_telescope, target, LUMINANCE_FILTER, contrib_lum_binning,
            calibration_mode=lum_calibration_mode,
        )
        frame_hash = frame_identity_hash([f.path.name for f in lum_lights])
        lum_frame_hashes[contributor_key] = frame_hash

        # Slice 2.2: the matched flat set for this Luminance contributor,
        # looked up here (not inside build_master -- see its own docstring)
        # since only the caller has `report` in scope to call flat_index()
        # on. Slice 2.2's per-telescope FlatPolicy: REQUIRE if this
        # telescope ships ANY flat at all (T21's real case), else
        # SKIP_IF_MISSING (T24's), unless `flat_policy` was explicitly
        # passed to override uniformly for the whole run.
        #
        # Precalibrated-path plan: a PRECALIBRATED telescope's flats are
        # never consulted at all (already flat-corrected upstream, see
        # stage_precalibrated_lights()'s docstring) -- calling
        # infer_flat_policy on it would be misleading, since T73 DOES ship
        # (BIN1-only) flats and would report REQUIRE despite this run path
        # never using them.
        if lum_calibration_mode == CalibrationMode.PRECALIBRATED:
            lum_flat_frames: list = []
            lum_flat_policy = FlatPolicy.SKIP_IF_MISSING
        else:
            lum_flat_frames = report.flat_index().get(
                (lum_telescope, contrib_lum_binning, LUMINANCE_FILTER), []
            )
            lum_flat_policy = (
                flat_policy if flat_policy is not None else infer_flat_policy(report, lum_telescope)
            )
        lum_flat_frame_hash = _flat_frame_hash(lum_flat_frames)
        lum_flat_frame_hashes[contributor_key] = lum_flat_frame_hash

        # Slice 4.1: usable() only knows whether master_luminance.fit
        # exists and reads clean -- it has zero notion of which lights
        # produced it, so a sub silently added/removed/replaced (the
        # orphaned pre-merge T24-kaveh096-M51-Luminance-bin1 group is real,
        # on-disk proof this gap is not hypothetical) would otherwise never
        # trigger a rebuild. Delete the stale master here so usable()'s own
        # skip-if-present gate inside build_master naturally regenerates it.
        # Slice 2.3: `flat_frame_hash` is now also passed here -- omitting
        # it (or leaving contributor_stale's own parameter at its default)
        # would silently defeat the whole point of tracking it at all, see
        # ContributorSignature/contributor_stale in run_signature.py.
        stale = force_masters or (
            old_signature is not None
            and old_signature.contributor_stale(
                "luminance", contributor_key, frame_hash, pedestal, lum_flat_frame_hash
            )
        )
        if stale:
            master_path = pipeline_dir(project_dir) / lum_group_name / "lights" / f"master_{LUMINANCE_FILTER.lower()}.fit"
            _delete_if_exists(master_path, f"{lum_label}: run signature changed", notes)

        lum_users = sorted({f.user for f in lum_lights})
        if len(lum_users) > 1:
            _log(f"[run ] combining Luminance across users: {', '.join(lum_users)}", notes)
        lum_master_path = build_master(
            project_dir, lum_lights, cal_index, lum_group_name, LUMINANCE_FILTER,
            lum_telescope, contrib_lum_binning, ra_hours, dec_deg, notes,
            flat_frames=lum_flat_frames, flat_policy=lum_flat_policy, pedestal=pedestal,
            calibration_mode=lum_calibration_mode,
        )
        result.masters[lum_label] = lum_master_path
        lum_stackcnt[contributor_key] = int(fits.getheader(lum_master_path).get("STACKCNT", len(lum_lights)))
        fwhm_arcsec = contributor_fwhm_arcsec(lum_master_path, notes, label=lum_label)
        if fwhm_arcsec is not None:
            _log(f"       {lum_label}: median FWHM {fwhm_arcsec:.2f}\"", notes)
        lum_candidates.append((lum_key, lum_master_path, fwhm_arcsec))

    selected_key, selected_path, selected_fwhm = select_luminance_source(
        lum_candidates, lum_source, (telescope, lum_binning), notes
    )
    result.masters[LUMINANCE_FILTER] = selected_path

    # --- colour contributors: one per binning that has a full R/G/B set,
    # for this telescope+target. rgb_binning is the PRIMARY contributor and
    # keeps the legacy top-level file paths; anything else discovered is an
    # additional contributor reconciled in at the master level. ------------
    rgb_binnings = sorted(
        {
            key[3]
            for key in report.instrument_groups()
            if key[0] == telescope and key[1] == target and key[2] in RGB_FILTERS
        }
    )
    if rgb_binning not in rgb_binnings:
        rgb_binnings = [rgb_binning, *rgb_binnings]

    try:
        profile = resolve_instrument_profile(telescope)
    except UnknownInstrumentError:
        # Let _build_colour_contributor raise this at the right point
        # (inside SPCC, once there is actually a contributor to fail on)
        # rather than aborting discovery over a telescope with no colour
        # data at all; recorded as no profile for signature purposes.
        profile = None
    profile_tuple = (
        (profile.mono_sensor, profile.red_filter, profile.green_filter, profile.blue_filter)
        if profile is not None else None
    )

    # Slice 2.2: colour discovery is hard-scoped to the caller's own
    # `telescope` (see module docstring / plan-flats-v3.md fact 11), so
    # there is exactly one telescope's worth of flat policy (and,
    # precalibrated-path plan, calibration mode) to infer here, computed
    # once rather than per-binning.
    colour_calibration_mode = (calibration_mode or {}).get(
        telescope, infer_calibration_mode(report, telescope)
    )
    colour_flat_policy = (
        FlatPolicy.SKIP_IF_MISSING
        if colour_calibration_mode == CalibrationMode.PRECALIBRATED
        else (flat_policy if flat_policy is not None else infer_flat_policy(report, telescope))
    )

    contributors: list[ColourContributor] = []
    colour_frame_hashes: dict[str, str] = {}
    colour_flat_frame_hashes: dict[str, str] = {}
    for binning in rgb_binnings:
        contributor_key = f"{telescope}_bin{binning}"
        contrib_dir = contributor_dir(final, telescope, binning, rgb_binning)
        frame_hash = _colour_contributor_frame_hash(report, telescope, target, binning)
        colour_frame_hashes[contributor_key] = frame_hash
        colour_flat_frame_hash = _colour_contributor_flat_frame_hash(report, telescope, binning)
        colour_flat_frame_hashes[contributor_key] = colour_flat_frame_hash

        # Slice 4.1: same gap as the Luminance loop above, plus SPCC
        # profile identity -- neither is visible to usable()'s file-only
        # gate. A frame/pedestal change forces a full rebuild (raw R/G/B
        # masters and every downstream product); an SPCC-profile-only
        # change only needs the colour-calibrated output redone, since the
        # profile has no effect on calibration/stacking. Slice 2.3: the
        # matched flat set is now also part of what "full rebuild" means --
        # contributor_stale's own `flat_frame_hash` parameter is passed
        # explicitly here, not left at its default (see run_signature.py's
        # ContributorSignature/contributor_stale docstrings for exactly why
        # a default alone would silently defeat this).
        needs_full_rebuild = force_masters or (
            old_signature is not None
            and old_signature.contributor_stale(
                "colour", contributor_key, frame_hash, pedestal, colour_flat_frame_hash
            )
        )
        if needs_full_rebuild:
            deleted = _clear_colour_contributor_products(
                contrib_dir, project_dir, report, telescope, target, binning,
                calibration_mode=colour_calibration_mode,
            )
            if deleted:
                _log(
                    f"[run ] BIN{binning}: run signature changed -- deleted "
                    f"{len(deleted)} stale contributor file(s) to force rebuild",
                    notes,
                )
        elif old_signature is not None:
            existing = old_signature.colour.get(contributor_key)
            if existing is not None and existing.spcc_profile != profile_tuple:
                _delete_if_exists(
                    contrib_dir / "rgb_colour_calibrated.fit",
                    f"BIN{binning}: SPCC profile changed",
                    notes,
                )

        contributor = _build_colour_contributor(
            project_dir, contrib_dir, report, telescope, target, binning,
            ra_hours, dec_deg, notes, flat_policy=colour_flat_policy,
            calibration_mode=colour_calibration_mode,
        )
        if contributor is None:
            # Logged inside _build_colour_contributor already (Slice 3.2:
            # log-and-skip, not abort-the-run).
            continue
        contributors.append(contributor)
        if len(rgb_binnings) > 1:
            _log(
                f"       {contributor.key} contributor: {contributor.stack_total} stacked "
                "subs across R/G/B (STACKCNT)",
                notes,
            )

    if not contributors:
        raise RuntimeError(
            f"No usable RGB colour contributor for {telescope}/{target} -- every discovered "
            f"binning ({rgb_binnings}) was missing at least one of Red/Green/Blue."
        )

    # Slice 3.4/3.5's designated reference -- the contributor with the
    # most STACKCNT -- computed here (not only inside the >1-contributor
    # branch below) so Slice 4.1's signature can record it even for a
    # single-contributor run; max() over one element just returns it.
    reference_pos = max(range(len(contributors)), key=lambda i: contributors[i].stack_total)
    reference = contributors[reference_pos]
    if len(contributors) > 1:
        _log(
            f"[run ] gain/offset reference: {reference.key} "
            f"(STACKCNT {reference.stack_total}, highest)",
            notes,
        )

    # --- Slice 4.1: build this run's signature, diff against whatever was
    # persisted last time, and delete exactly the downstream files that
    # diff says are now stale -- BEFORE touching lum_bg.fits/
    # rgb_reconciled.fit/lrgb_final.fit below, so their own usable() gates
    # see the deletion and regenerate naturally (see run_signature.py's
    # module docstring: no second, parallel gating mechanism). This is
    # the ONLY place STACKCNT and the reference-contributor identity are
    # known, which is why the reference-flip nuance (STACKCNT changing
    # enough to pick a different reference with no light frame changing
    # at all) can only be caught here, post-build -- not in the pre-build
    # per-contributor checks above.
    new_signature = RunSignature(
        stretch_method=stretch_method,
        pedestal=pedestal,
        luminance_selected=f"{selected_key[0]}_bin{selected_key[1]}",
        luminance={
            key: ContributorSignature(
                key=key, stackcnt=lum_stackcnt[key], frame_hash=lum_frame_hashes[key],
                flat_frame_hash=lum_flat_frame_hashes[key],
                calibration_mode=lum_calibration_modes[key],
            )
            for key in lum_frame_hashes
        },
        colour_reference=reference.key,
        colour={
            c.key: ContributorSignature(
                key=c.key, stackcnt=c.stack_total, frame_hash=colour_frame_hashes[c.key],
                flat_frame_hash=colour_flat_frame_hashes[c.key],
                spcc_profile=profile_tuple,
                calibration_mode=colour_calibration_mode.value,
            )
            for c in contributors
        },
    )
    signature_stages = diff_invalidation(old_signature, new_signature)
    stages_to_invalidate = set(signature_stages)
    for stage in force:
        stages_to_invalidate |= cascade_from(stage)

    def _reason(stage: str) -> str:
        # Distinguish an automatic signature-mismatch invalidation from an
        # explicit force -- both end up in `stages_to_invalidate`, but the
        # log should say which actually applied here, not always blame the
        # signature (a force={"final"} call has nothing to do with
        # stretch_method, for example).
        via_signature = stage in signature_stages
        via_force = any(stage in cascade_from(f) for f in force)
        if via_signature and via_force:
            return "run signature changed, and explicitly forced"
        if via_signature:
            return "run signature changed"
        return "explicitly forced"

    if "masters" in stages_to_invalidate:
        reason = f"{_reason('masters')} (Luminance-affecting)"
        _delete_if_exists(final / "lum_bg.fits", reason, notes)
        _delete_if_exists(final / "rgb_reconciled.fit", reason, notes)
        _delete_if_exists(final / "lrgb_final.fit", reason, notes)
    if "reconciled" in stages_to_invalidate:
        reason = f"{_reason('reconciled')} (colour-affecting)"
        _delete_if_exists(final / "rgb_reconciled.fit", reason, notes)
        _delete_if_exists(final / "lrgb_final.fit", reason, notes)
    if "final" in stages_to_invalidate:
        _delete_if_exists(final / "lrgb_final.fit", _reason("final"), notes)
    save_run_signature(new_signature, run_signature_path)

    # --- checkpoints for the "masters" stop_after boundary ----------------
    previous = None
    previous_linear: bool | None = None
    cp = checkpoint(
        selected_path, f"01_master_{LUMINANCE_FILTER.lower()}", output_dir=checkpoint_dir,
        linear=True, previous=previous, previous_linear=previous_linear,
    )
    result.checkpoints.append(cp)
    previous, previous_linear = cp.stats, True
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    primary_calibrated = final / "rgb_colour_calibrated.fit"
    if primary_calibrated.exists():
        cp = checkpoint(
            primary_calibrated, "02_primary_rgb_colour_calibrated", output_dir=checkpoint_dir,
            linear=True, previous=previous, previous_linear=previous_linear,
        )
        result.checkpoints.append(cp)
        previous, previous_linear = cp.stats, True
        _log(cp.summary(), notes)
        save_checkpoints(result.checkpoints, checkpoints_path)

    if stop_after == "masters":
        _log(
            "[stop] stop_after='masters' -- Luminance selected and every colour contributor "
            "built; stopping before reconciliation",
            notes,
        )
        return result

    # --- luminance: background extraction --------------------------------
    lum_bg = final / "lum_bg.fits"
    if not usable(lum_bg, notes):
        shutil.copy2(result.masters[LUMINANCE_FILTER], final / "lum.fit")
        _log("[run ] GraXpert background extraction on L", notes)
        lum_bg = run_graxpert_background_extraction(final / "lum.fit", output_stem="lum_bg")
    else:
        _log("[skip] L background extraction already done", notes)

    cp = checkpoint(
        lum_bg, "03_lum_background_extracted", output_dir=checkpoint_dir,
        linear=True, previous=previous, previous_linear=previous_linear,
    )
    result.checkpoints.append(cp)
    previous, previous_linear = cp.stats, True
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    # --- reproject every colour contributor onto L's grid, then combine --
    # `lum_for_compose_path` is computed unconditionally (not inside the
    # usable() guard below) so a RESUMED run -- which skips rebuilding
    # rgb_reconciled entirely -- still picks the same L file that was
    # actually paired with it. Getting this wrong pairs a cropped
    # rgb_reconciled with the original, larger lum_bg on resume: a shape
    # mismatch feeding into rgbcomp -lum=.
    lum_for_compose_path = lum_bg
    if len(contributors) > 1:
        lum_for_compose_path = final / "lum_bg_cropped.fits"

    rgb_reconciled = final / "rgb_reconciled.fit"
    if not usable(rgb_reconciled, notes):
        if len(contributors) == 1:
            _log("[run ] reprojecting colour onto L's pixel grid", notes)
            recon = reproject_to_reference(contributors[0].composite_path, lum_bg, rgb_reconciled)
            _log(f"       footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f}", notes)
        else:
            reprojected_paths = []
            for contributor in contributors:
                # Slice 3.3: keyed by (telescope, binning) identity, not
                # loop position -- verified on real data that positional
                # naming (`contrib{i}` over sorted(rgb_binnings)) put
                # BIN1's contributor at index 0 even though BIN2 is the
                # PRIMARY one, a real footgun for anyone reading these
                # files by number expecting index 0 = primary.
                out_path = final / f"rgb_reconciled_contrib_{contributor.key}.fit"
                recon = reproject_to_reference(contributor.composite_path, lum_bg, out_path)
                _log(
                    f"[run ] reprojected {contributor.key} onto L's grid "
                    f"(footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f})",
                    notes,
                )
                reprojected_paths.append(out_path)

            # Crop L and every reprojected contributor to their common
            # coverage BEFORE combining, rather than combining with a
            # NaN-aware per-pixel fallback. Each contributor ran its own
            # independent GraXpert background extraction, so their absolute
            # background pedestals genuinely differ (measured on real data:
            # 0.101 vs 0.085, ~18% apart) even though each is internally
            # well balanced (R~G~B within each). A NaN-aware combine uses
            # whichever contributor has data at a given pixel, which leaves
            # a hard STEP in background level exactly at the coverage
            # boundary -- rendered by the shadow-clipped stretch as a
            # visible colour-shifted band along that edge. This is the same
            # failure shape as the single-contributor channel-alignment
            # banding fixed earlier (see crop_to_common_coverage's own
            # docstring) -- cropping means every remaining pixel is a
            # genuine average of every contributor, so there is no boundary
            # left to show a seam at.
            cropped = crop_to_common_coverage(
                [lum_bg, *reprojected_paths], final / "combined_crop"
            )
            cropped_shape = fits.getdata(cropped[0], memmap=False).shape
            _log(
                f"[run ] cropped L + {len(contributors)} contributors to common coverage {cropped_shape}",
                notes,
            )
            shutil.copy2(cropped[0], lum_for_compose_path)
            cropped_rgb = cropped[1:]  # aligned 1:1 with `contributors`

            # Slice 3.4/3.5: the designated reference is the contributor
            # with the most STACKCNT -- both the gain/offset fit (3.4) and
            # the weighting (3.5) are measured against/by it, and it also
            # supplies the combined output's FITS header (3.6), replacing
            # the old paths[0] positional pick. `reference`/`reference_pos`
            # are computed once, above, right after the colour-contributor
            # loop (Slice 4.1 needs the reference's identity for the run
            # signature even on a single-contributor run) -- reused here
            # rather than recomputed.
            gain_matched: list[Path] = []
            for i, (contributor, path) in enumerate(zip(contributors, cropped_rgb)):
                if i == reference_pos:
                    gain_matched.append(path)
                    continue
                matched_path = final / "combined_crop" / f"gain_matched_{contributor.key}.fit"
                fit_results = match_gain_offset(path, cropped_rgb[reference_pos], matched_path)
                for fit in fit_results:
                    _log(
                        f"       {contributor.key} vs {reference.key} ch{fit.channel}: "
                        f"gain={fit.gain:.4f} on {fit.n_pixels} high-signal px "
                        f"(ref bkg {fit.reference_background:.5f}, "
                        f"contrib bkg {fit.contributor_background:.5f})",
                        notes,
                    )
                gain_matched.append(matched_path)

            # Slice 3.5: weight by STACKCNT (subs that actually survived
            # quality filtering/rejection into the stacked masters), not
            # sub_count (raw light count) -- see combine_same_grid's own
            # docstring for why an inverse-variance scheme was tried and
            # rejected in between.
            weights = [float(c.stack_total) for c in contributors]
            combine_same_grid(
                gain_matched, rgb_reconciled, weights=weights, reference_index=reference_pos,
            )
            _log(
                f"[run ] combined {len(contributors)} colour contributors by STACKCNT "
                f"(weights {weights})",
                notes,
            )
    else:
        _log("[skip] reprojection already done", notes)

    cp = checkpoint(
        rgb_reconciled, "04_rgb_reconciled", output_dir=checkpoint_dir,
        linear=True, previous=previous, previous_linear=previous_linear,
    )
    result.checkpoints.append(cp)
    previous, previous_linear = cp.stats, True
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    if stop_after == "reconciled":
        _log(
            "[stop] stop_after='reconciled' -- L background extracted and colour reconciled; "
            "stopping before stretch/export",
            notes,
        )
        return result

    # --- stretch + LRGB composition --------------------------------------
    composite = final / "lrgb_final.fit"
    if not usable(composite, notes):
        lum_in = final / "lum_for_compose.fit"
        rgb_in = final / "rgb_for_compose.fit"
        shutil.copy2(lum_for_compose_path, lum_in)
        shutil.copy2(rgb_reconciled, rgb_in)
        _log(f"[run ] stretch ({stretch_method}) + rgbcomp -lum", notes)
        compose = stretch_and_compose(
            lum_in, rgb_in, final, output_stem="lrgb_final", method=stretch_method,
        )
        composite = compose.composite_path
    else:
        _log("[skip] LRGB composite already present", notes)
    result.composite_path = composite

    cp = checkpoint(
        composite, "05_lrgb_final", output_dir=checkpoint_dir,
        linear=False, previous=previous, previous_linear=previous_linear,  # post-stretch: render faithfully
    )
    result.checkpoints.append(cp)
    previous, previous_linear = cp.stats, False
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    # --- export -----------------------------------------------------------
    # Always re-run, unconditionally -- export() has no usable() gate of its
    # own (cheap: format conversion, not a Siril/GraXpert/SPCC call), so it
    # simply reflects whatever `composite` currently is, resumed or fresh.
    _log("[run ] export TIFF + preview", notes)
    # Slice 4.4: `target` threaded through as the stem instead of a
    # hardcoded "M51_lrgb" -- verified safe against the real M51 fixture
    # (target="M51" here reproduces the exact same "M51_lrgb" stem, so
    # tests/test_pipeline.py's byte-identical-output assertions against
    # M51_lrgb.tif are unaffected), and required for the skill (4.4) to
    # generalize the handoff path to whatever target Kaveh throws at it
    # next instead of silently mislabeling every future target's TIFF as
    # M51's.
    result.export_result = export(composite, output_dir=final, stem=f"{target}_lrgb")
    _log(
        f"       row order {result.export_result.row_order}, "
        f"clipped low {result.export_result.clipped_low_fraction:.4f} / "
        f"high {result.export_result.clipped_high_fraction:.4f}",
        notes,
    )

    return result
