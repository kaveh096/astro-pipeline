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

  Luminance combines at the RAW-SUB level. Two users sharing a telescope
  and binning for the same filter (e.g. both shooting Luminance/BIN1) get
  their raw lights merged into ONE calibrate+register+stack call, via
  IngestReport.instrument_groups(). This gives Siril's sigma-rejection
  visibility into every individual frame -- better outlier/satellite-trail
  rejection than averaging two independently-stacked masters would be.
  This works here because both users, on this data, share the same
  telescope AND exposure time -- calibration_index() has no user
  dimension, so the shared bias/dark for that telescope+binning+exptime
  already applies correctly to a merged light list with no changes needed
  to calibration.py. (A merge across DIFFERING exposure times would need
  calibration.py to route each light to its own matching dark master
  before stacking -- not implemented, because nothing in this delivery
  needs it yet: T21's 2 lights are 300s/600s mixed, but T21 is a separate
  telescope with a genuinely different pixel scale from T24, so its
  Luminance can never raw-sub-combine with T24's regardless; it would need
  its own master-level contributor, which is a real follow-up, not built
  here because two frames doesn't justify the calibration.py work yet.)

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
  under final/contrib_bin<n>/, and only actually runs if all three R/G/B
  are present for it -- a partial contributor (missing one channel) is a
  real data problem and raises rather than silently building an
  incomplete composite.
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
    T24_PROFILE,
    run_graxpert_background_extraction,
    run_spcc,
)
from .calibration import run_calibration
from .export_image import ExportResult, export
from .ingest import scan_session
from .checkpoints import Checkpoint, checkpoint, save_checkpoints
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
    exptime: float,
    ra_hours: float,
    dec_deg: float,
    notes: list[str],
) -> Path:
    """Calibrate -> register+stack -> plate solve one group of raw lights.

    `lights` may span multiple users -- raw-sub-level combining across
    collaborators is decided by the caller (see resolve_lights below), not
    here. Calibration and stacking don't care about user identity at all,
    only about telescope/binning/exptime matching the calibration frames.
    """
    work_dir = pipeline_dir(project_dir) / group_name
    master_path = work_dir / "lights" / f"master_{filter_name.lower()}.fit"

    if usable(master_path, notes) and fits.getheader(master_path).get("PLTSOLVD"):
        _log(f"[skip] {group_name}: solved master already present", notes)
        return master_path

    bias = cal_index[(telescope, "Bias", binning, 0.0)]
    dark = cal_index[(telescope, "Dark", binning, exptime)]

    _log(
        f"[run ] {group_name}: calibrating {len(lights)} lights "
        f"({len(bias)} bias, {len(dark)} dark)",
        notes,
    )
    run_calibration(lights, bias, dark, work_dir=work_dir, flat_frames=None)

    _log(f"[run ] {group_name}: register + stack", notes)
    stack_result = register_and_stack(
        "pp_lights_", work_dir / "lights", out_name=f"master_{filter_name.lower()}"
    )

    _log(f"[run ] {group_name}: plate solve", notes)
    solve(stack_result.master_path, ra_hours=ra_hours, dec_deg=dec_deg, search_radius_deg=5.0)
    return stack_result.master_path


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


def _build_colour_contributor(
    project_dir: Path,
    contrib_dir: Path,
    report,
    telescope: str,
    target: str,
    binning: int,
    exptime: float,
    ra_hours: float,
    dec_deg: float,
    notes: list[str],
) -> tuple[Path, int]:
    """Build one binning's R/G/B masters, align + crop + composite them,
    then background-extract and colour-calibrate -- everything the
    single-contributor pipeline always did, just namespaced under
    `contrib_dir` so multiple binnings can coexist. Returns the
    colour-calibrated composite's path and the number of raw subs that
    went into it (summed across channels), used to weight this
    contributor when combining with others later.
    """
    contrib_dir.mkdir(parents=True, exist_ok=True)
    cal_index = report.calibration_index()

    channel_masters: dict[str, Path] = {}
    sub_count = 0
    for filter_name in RGB_FILTERS:
        lights, group_name = resolve_lights(report, telescope, target, filter_name, binning)
        if not lights:
            raise RuntimeError(
                f"No {filter_name}/BIN{binning} lights found for {telescope}/{target} -- "
                "a colour contributor needs all three of Red/Green/Blue."
            )
        sub_count += len(lights)
        channel_masters[filter_name] = build_master(
            project_dir, lights, cal_index, group_name, filter_name,
            telescope, binning, exptime, ra_hours, dec_deg, notes,
        )

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
        profile = INSTRUMENT_PROFILES.get(telescope, T24_PROFILE)
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

    return colour_calibrated, sub_count


def run_lrgb(
    project_dir: str | Path,
    telescope: str,
    target: str,
    ra_hours: float,
    dec_deg: float,
    lum_binning: int = 1,
    rgb_binning: int = 2,
    exptime: float = 300.0,
    stretch_method: str = "autostretch",
) -> PipelineResult:
    project_dir = Path(project_dir)
    out = pipeline_dir(project_dir)
    final = out / "final"
    final.mkdir(parents=True, exist_ok=True)
    result = PipelineResult()
    notes = result.notes

    _log(f"=== scanning {project_dir.name} ===", notes)
    report = scan_session(project_dir)

    # --- luminance master: merged across every user sharing this telescope
    # and binning (raw-sub-level combining -- see module docstring) -------
    cal_index = report.calibration_index()
    lum_lights, lum_group_name = resolve_lights(report, telescope, target, LUMINANCE_FILTER, lum_binning)
    lum_users = sorted({f.user for f in lum_lights})
    if len(lum_users) > 1:
        _log(f"[run ] combining Luminance across users: {', '.join(lum_users)}", notes)
    result.masters[LUMINANCE_FILTER] = build_master(
        project_dir, lum_lights, cal_index, lum_group_name, LUMINANCE_FILTER,
        telescope, lum_binning, exptime, ra_hours, dec_deg, notes,
    )

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

    contributors: list[tuple[Path, int]] = []
    for binning in rgb_binnings:
        contrib_dir = final if binning == rgb_binning else final / f"contrib_bin{binning}"
        composite, sub_count = _build_colour_contributor(
            project_dir, contrib_dir, report, telescope, target, binning,
            exptime, ra_hours, dec_deg, notes,
        )
        contributors.append((composite, sub_count))
        if len(rgb_binnings) > 1:
            _log(f"       BIN{binning} contributor: {sub_count} raw subs across R/G/B", notes)

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
            recon = reproject_to_reference(contributors[0][0], lum_bg, rgb_reconciled)
            _log(f"       footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f}", notes)
        else:
            reprojected_paths = []
            weights = []
            for i, (composite, sub_count) in enumerate(contributors):
                out_path = final / f"rgb_reconciled_contrib{i}.fit"
                recon = reproject_to_reference(composite, lum_bg, out_path)
                _log(
                    f"[run ] reprojected contributor {i} onto L's grid "
                    f"(footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f})",
                    notes,
                )
                reprojected_paths.append(out_path)
                weights.append(float(sub_count))

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
