"""Single-group master building, extracted from pipeline.py (Task 5
refactor, 2026-09-16). One real chain: resolve which lights feed a group,
calibrate -> register+stack -> plate solve them, and (optionally) measure
the result's sharpness. `resolve_lights` is used by the Luminance loop,
the RGB loop, and the OSC loop alike -- it belongs at this shared, low
level, not inside a "colour contributor" module.

`build_group_master` was named `build_master` before this refactor --
renamed on extraction because `calibration.py` already defines its own,
unrelated `build_master(frame_paths, basename, work_dir, ...)` (a
low-level stage/convert/stack-with-rejection primitive behind
`build_master_bias/dark/flat`). Both were already imported bare as
`build_master` into different test files; once this module sits flat next
to `calibration.py` both exporting an identically-named symbol would be a
real, ongoing human/grep mistargeting risk.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits

from .calibration import (
    DEFAULT_PEDESTAL,
    CalibrationMode,
    FlatPolicy,
    run_calibration,
    select_dark,
    stage_precalibrated_lights,
)
from .calibration_policy import infer_calibration_mode, infer_flat_policy
from .checkpoints import _pixel_scale_arcsec
from .ingest import CalibrationFrame
from .logging_utils import log as _log
from .registration_stacking import MIN_SEQUENCE_FRAMES, register_and_stack
from .resume_guard import usable
from .solving import solve
from .workspace import group_name_for, pipeline_dir


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
    fixtures/tests that assume e.g. "T24-observer1-M51-Red-bin2" keep
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


def build_group_master(
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
    debayer: bool = False,
    bayer_pattern: int = 0,
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
            f"(skip bias/dark/flat{', debayer' if debayer else ''})",
            notes,
        )
        stage_precalibrated_lights(
            lights, work_dir, basename="lights", pedestal=pedestal, notes=notes,
            debayer=debayer, bayer_pattern=bayer_pattern,
        )
    else:
        bias = cal_index[(telescope, "Bias", binning, 0.0)]

        # OSC + local raw calibration plan (2026-09): a real, not
        # hypothetical, case -- T68 (IC 1396) has real local bias but NO
        # real local dark frames at all. select_dark() correctly raises
        # CalibrationFramesMissingError when no dark exists at this
        # binning (its own documented case 3) -- appropriate for a mono
        # RAW_LOCAL telescope (dark is always required there), but real,
        # expected state for a debayer=True OSC delivery like T68's. Only
        # skip select_dark() when debayer=True AND no dark genuinely
        # exists for this (telescope, binning) at all -- if one DOES
        # exist, still use it normally (a future OSC delivery might have
        # real darks; this must not silently ignore data that exists).
        has_any_dark = any(
            t == telescope and ftype == "Dark" and b == binning
            for (t, ftype, b, _e) in cal_index
        )
        if debayer and not has_any_dark:
            dark_frames: list = []
            dark_scaled = False
            dark_note = "no dark (bias-only + debayer)"
        else:
            dark_selection = select_dark(cal_index, telescope, binning, light_exptimes)
            dark_frames = dark_selection.frames
            dark_scaled = dark_selection.scaled
            dark_note = f"{len(dark_frames)} dark" + (
                f", scaled from {dark_selection.exptime:.0f}s via -opt=exp" if dark_scaled else ""
            )

        exptimes_str = ", ".join(f"{e:.0f}s" for e in sorted(light_exptimes))
        flat_note = f", {len(flat_frames)} {filter_name} flat" if flat_frames else ""
        _log(
            f"[run ] {group_name}: calibrating {len(lights)} lights at {exptimes_str} "
            f"({len(bias)} bias, {dark_note}{flat_note})",
            notes,
        )
        calibration_result = run_calibration(
            lights, bias, dark_frames, work_dir=work_dir,
            flat_frames=flat_frames, flat_policy=flat_policy,
            dark_scaled=dark_scaled, pedestal=pedestal, notes=notes,
            require_dark=not (debayer and not has_any_dark),
            debayer=debayer, bayer_pattern=bayer_pattern,
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
    """Median stellar FWHM, in arcsec, for one build_group_master() output
    -- the sharpness figure Slice 2's Luminance-source selection is
    measured against (see run_lrgb).

    A sibling accessor rather than a change to build_group_master's own
    return type: its plain Path return is left alone (its colour-
    contributor call sites are unrelated to this slice), and this reads
    the same on-disk outputs independently, given nothing but the master
    Path build_group_master already handed back.

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


def build_single_filter_master(
    project_dir: Path,
    report,
    telescope: str,
    target: str,
    filter_name: str,
    binning: int,
    ra_hours: float,
    dec_deg: float,
    notes: list[str],
    flat_policy: FlatPolicy | None = None,
    calibration_mode: CalibrationMode | None = None,
) -> Path | None:
    """Build one (telescope, filter, binning) master, handling the same
    PRECALIBRATED-vs-RAW_LOCAL branching `_build_colour_contributor`'s
    own per-filter loop does -- factored out as its own function
    (narrowband-boost plan, 2026-09) so a caller needing exactly ONE
    filter's master (e.g. a narrowband boost channel, not a full R/G/B
    triplet) doesn't have to reimplement that branching. Returns None --
    logging why, rather than raising -- if there isn't enough data to
    build it (no lights, or too few to form a Siril sequence), matching
    every other real "skip this contributor" pattern in this module.
    """
    resolved_calibration_mode = calibration_mode or infer_calibration_mode(report, telescope)

    lights, group_name = resolve_lights(
        report, telescope, target, filter_name, binning, calibration_mode=resolved_calibration_mode
    )
    if not lights:
        _log(f"[skip] BIN{binning}: no {filter_name} lights found for {telescope}/{target}", notes)
        return None
    if len(lights) < MIN_SEQUENCE_FRAMES:
        _log(
            f"[skip] BIN{binning}: only {len(lights)} {filter_name} light(s) for "
            f"{telescope}/{target}, need at least {MIN_SEQUENCE_FRAMES} to form a Siril sequence",
            notes,
        )
        return None

    if resolved_calibration_mode == CalibrationMode.PRECALIBRATED:
        return build_group_master(
            project_dir, lights, {}, group_name, filter_name, telescope, binning, ra_hours, dec_deg, notes,
            flat_frames=[], flat_policy=FlatPolicy.SKIP_IF_MISSING, calibration_mode=resolved_calibration_mode,
        )
    # infer_flat_policy() calls report.flat_index() -- only safe/meaningful
    # on the RAW_LOCAL path (a PRECALIBRATED telescope's flats are never
    # consulted at all, see this function's own docstring), so this stays
    # inside the else branch rather than being computed unconditionally
    # up front.
    resolved_flat_policy = flat_policy if flat_policy is not None else infer_flat_policy(report, telescope)
    cal_index = report.calibration_index()
    flat_frames = report.flat_index().get((telescope, binning, filter_name), [])
    return build_group_master(
        project_dir, lights, cal_index, group_name, filter_name, telescope, binning, ra_hours, dec_deg, notes,
        flat_frames=flat_frames, flat_policy=resolved_flat_policy,
    )
