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
  Luminance grid, THEN averaged together (reconciliation.combine_same_grid,
  weighted by how many raw subs each contributor stacked) into one final
  RGB. This is exactly the pattern reconciliation.py's own module
  docstring anticipated: "each instrument/user's data is independently
  calibrated, registered, and stacked first, and only the resulting
  masters get reprojected together."

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
from .calibration import select_dark, run_calibration
from .export_image import ExportResult, export
from .ingest import scan_session
from .checkpoints import Checkpoint, checkpoint, save_checkpoints, _pixel_scale_arcsec
from .reconciliation import combine_same_grid, crop_to_common_coverage, reproject_to_reference
from .registration_stacking import register_and_stack
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
    """
    work_dir = pipeline_dir(project_dir) / group_name
    master_path = work_dir / "lights" / f"master_{filter_name.lower()}.fit"

    if usable(master_path, notes) and fits.getheader(master_path).get("PLTSOLVD"):
        _log(f"[skip] {group_name}: solved master already present", notes)
        return master_path

    light_exptimes = {f.exptime for f in lights}
    bias = cal_index[(telescope, "Bias", binning, 0.0)]
    dark_selection = select_dark(cal_index, telescope, binning, light_exptimes)

    exptimes_str = ", ".join(f"{e:.0f}s" for e in sorted(light_exptimes))
    scaling_note = (
        f", scaled from {dark_selection.exptime:.0f}s via -opt=exp" if dark_selection.scaled else ""
    )
    _log(
        f"[run ] {group_name}: calibrating {len(lights)} lights at {exptimes_str} "
        f"({len(bias)} bias, {len(dark_selection.frames)} dark{scaling_note})",
        notes,
    )
    run_calibration(
        lights, bias, dark_selection.frames, work_dir=work_dir, flat_frames=None,
        dark_scaled=dark_selection.scaled, notes=notes,
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
    report, telescope: str, target: str, filter_name: str, binning: int
) -> tuple[list, str]:
    """Look up the raw lights for one (telescope, target, filter, binning)
    unit -- merged across every user who contributed to it (see
    IngestReport.instrument_groups()) -- and derive a stable, descriptive
    group name from whoever those users turn out to be.

    A single-contributor case (only one user shot this filter/binning)
    collapses to exactly the old per-user group name, so existing
    fixtures/tests that assume e.g. "T24-kaveh096-M51-Red-bin2" keep
    working unchanged.
    """
    lights = report.instrument_groups().get((telescope, target, filter_name, binning), [])
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
    """
    contrib_dir.mkdir(parents=True, exist_ok=True)
    cal_index = report.calibration_index()

    channel_masters: dict[str, Path] = {}
    sub_count = 0
    stack_total = 0
    for filter_name in RGB_FILTERS:
        lights, group_name = resolve_lights(report, telescope, target, filter_name, binning)
        if not lights:
            _log(
                f"[skip] BIN{binning}: no {filter_name} lights found for {telescope}/{target} "
                "-- a colour contributor needs all three of Red/Green/Blue; skipping this "
                "contributor rather than aborting the whole run",
                notes,
            )
            return None
        sub_count += len(lights)
        master_path = build_master(
            project_dir, lights, cal_index, group_name, filter_name,
            telescope, binning, ra_hours, dec_deg, notes,
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
    named combo wasn't actually discovered for this target."""
    project_dir = Path(project_dir)
    out = pipeline_dir(project_dir)
    final = out / "final"
    final.mkdir(parents=True, exist_ok=True)
    result = PipelineResult()
    notes = result.notes

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

    lum_candidates: list[LumCandidate] = []
    for lum_telescope, contrib_lum_binning in lum_contributors:
        lum_key = (lum_telescope, contrib_lum_binning)
        lum_label = f"Luminance-{lum_telescope}-bin{contrib_lum_binning}"
        lum_lights, lum_group_name = resolve_lights(
            report, lum_telescope, target, LUMINANCE_FILTER, contrib_lum_binning
        )
        lum_users = sorted({f.user for f in lum_lights})
        if len(lum_users) > 1:
            _log(f"[run ] combining Luminance across users: {', '.join(lum_users)}", notes)
        lum_master_path = build_master(
            project_dir, lum_lights, cal_index, lum_group_name, LUMINANCE_FILTER,
            lum_telescope, contrib_lum_binning, ra_hours, dec_deg, notes,
        )
        result.masters[lum_label] = lum_master_path
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

    contributors: list[ColourContributor] = []
    for binning in rgb_binnings:
        contrib_dir = contributor_dir(final, telescope, binning, rgb_binning)
        contributor = _build_colour_contributor(
            project_dir, contrib_dir, report, telescope, target, binning,
            ra_hours, dec_deg, notes,
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

    # --- luminance: background extraction --------------------------------
    lum_bg = final / "lum_bg.fits"
    if not usable(lum_bg, notes):
        shutil.copy2(result.masters[LUMINANCE_FILTER], final / "lum.fit")
        _log("[run ] GraXpert background extraction on L", notes)
        lum_bg = run_graxpert_background_extraction(final / "lum.fit", output_stem="lum_bg")
    else:
        _log("[skip] L background extraction already done", notes)

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
            # Weighting stays on sub_count (raw sub count) -- unchanged from
            # today; Slice 3.4-3.6 replaces this with a gain/offset-matched,
            # STACKCNT-weighted combine.
            weights = [float(c.sub_count) for c in contributors]
            combine_same_grid(cropped[1:], rgb_reconciled, weights=weights)
            _log(
                f"[run ] combined {len(contributors)} colour contributors "
                f"(weights {weights})",
                notes,
            )
    else:
        _log("[skip] reprojection already done", notes)

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

    # --- export -----------------------------------------------------------
    _log("[run ] export TIFF + preview", notes)
    result.export_result = export(composite, output_dir=final, stem="M51_lrgb")
    _log(
        f"       row order {result.export_result.row_order}, "
        f"clipped low {result.export_result.clipped_low_fraction:.4f} / "
        f"high {result.export_result.clipped_high_fraction:.4f}",
        notes,
    )

    # --- checkpoints -------------------------------------------------------
    # Always produced, even on a fully-resumed run: they describe the state
    # of the outputs, not the work done to get there, so a run that skipped
    # everything should still be inspectable.
    _log("[run ] checkpoints", notes)
    checkpoint_dir = out / "checkpoints"
    stages: list[tuple[str, Path, bool]] = [
        (f"01_master_{LUMINANCE_FILTER.lower()}", result.masters[LUMINANCE_FILTER], True),
        (
            "02_primary_rgb_colour_calibrated",
            final / "rgb_colour_calibrated.fit",
            True,
        ),
        ("03_lum_background_extracted", Path(lum_bg), True),
        ("04_rgb_reconciled", rgb_reconciled, True),
        ("05_lrgb_final", Path(composite), False),  # post-stretch: render faithfully
    ]
    previous = None
    previous_linear: bool | None = None
    for label, path, linear in stages:
        if not Path(path).exists():
            continue
        cp = checkpoint(
            path, label, output_dir=checkpoint_dir, linear=linear,
            previous=previous, previous_linear=previous_linear,
        )
        result.checkpoints.append(cp)
        previous, previous_linear = cp.stats, linear
        _log(cp.summary(), notes)

    save_checkpoints(result.checkpoints, checkpoint_dir / "checkpoints.json")
    return result
