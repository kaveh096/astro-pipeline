"""End-to-end LRGB orchestration for a single telescope+user+target.

Deliberately resumable at every step: each stage checks whether its output
already exists and skips if so. This machine has ~8GB RAM and long runs
have been killed mid-flight more than once, so a re-run must continue
rather than start over. It also means a stage can be deleted from
`_pipeline/` to force just that stage to recompute.

Stage order here reflects what was established empirically (see the
individual modules' docstrings), not the original design sketch:

    per filter:  calibrate -> register+stack -> plate solve
    colour:      rgbcomp at NATIVE resolution -> GraXpert -> PCC
    luminance:   GraXpert
    combine:     reproject colour onto L's grid -> GHT both -> rgbcomp -lum
    export:      16-bit TIFF + faithful PNG preview

Colour processing happens at native (binned) resolution and is reprojected
up to L's grid only at the end -- reprojection introduces NaN edges and
interpolation artifacts that break Siril's star photometry, so anything
photometric (PCC) must run before it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits

from .background_color import run_graxpert_background_extraction, run_pcc
from .calibration import run_calibration
from .export_image import ExportResult, export
from .ingest import scan_session
from .checkpoints import Checkpoint, checkpoint, save_checkpoints
from .reconciliation import crop_to_common_coverage, reproject_to_reference
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
    report,
    telescope: str,
    user: str,
    target: str,
    filter_name: str,
    binning: int,
    exptime: float,
    ra_hours: float,
    dec_deg: float,
    notes: list[str],
) -> Path:
    """Calibrate -> register+stack -> plate solve one filter group."""
    name = group_name_for(telescope, user, target, filter_name, binning)
    work_dir = pipeline_dir(project_dir) / name
    master_path = work_dir / "lights" / f"master_{filter_name.lower()}.fit"

    if usable(master_path, notes) and fits.getheader(master_path).get("PLTSOLVD"):
        _log(f"[skip] {name}: solved master already present", notes)
        return master_path

    groups = report.light_groups()
    lights = groups[(telescope, user, target, filter_name, binning)]
    cal_index = report.calibration_index()
    bias = cal_index[(telescope, "Bias", binning, 0.0)]
    dark = cal_index[(telescope, "Dark", binning, exptime)]

    _log(f"[run ] {name}: calibrating {len(lights)} lights ({len(bias)} bias, {len(dark)} dark)", notes)
    run_calibration(lights, bias, dark, work_dir=work_dir, flat_frames=None)

    _log(f"[run ] {name}: register + stack", notes)
    stack_result = register_and_stack(
        "pp_lights_", work_dir / "lights", out_name=f"master_{filter_name.lower()}"
    )

    _log(f"[run ] {name}: plate solve", notes)
    solve(stack_result.master_path, ra_hours=ra_hours, dec_deg=dec_deg, search_radius_deg=5.0)
    return stack_result.master_path


def run_lrgb(
    project_dir: str | Path,
    telescope: str,
    user: str,
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

    # --- per-filter masters ---------------------------------------------
    for filter_name, binning in (
        (LUMINANCE_FILTER, lum_binning),
        *((f, rgb_binning) for f in RGB_FILTERS),
    ):
        result.masters[filter_name] = build_master(
            project_dir, report, telescope, user, target,
            filter_name, binning, exptime, ra_hours, dec_deg, notes,
        )

    # --- colour: composite at native resolution, then background + PCC ---
    rgb_native = final / "rgb_native.fit"
    if not usable(rgb_native, notes):
        # Each filter was registered against its OWN reference frame, so the
        # three masters do not share a pointing -- and `rgbcomp` stacks them
        # straight into R/G/B channels without aligning anything. Measured on
        # this data, Green sat 8.8px from Red and Blue 4.4px, comparable to
        # the stars' own ~5-9px FWHM, which showed up as red/green fringing
        # on every star in the checkpoint preview. Reproject the other two
        # onto Red's grid first so the channels actually correspond.
        reference = result.masters["Red"]
        shutil.copy2(reference, final / "red.fit")
        for filter_name in ("Green", "Blue"):
            out_path = final / f"{filter_name.lower()}.fit"
            recon = reproject_to_reference(result.masters[filter_name], reference, out_path)
            _log(
                f"[run ] aligned {filter_name} onto Red's grid "
                f"(footprint {recon.footprint_mean:.3f})",
                notes,
            )
        # Crop away the slivers alignment left uncovered rather than filling
        # them -- a constant fill is visible to GraXpert's background model
        # and produced a green band across the finished image. See
        # crop_to_common_coverage.
        channel_paths = [final / f"{f.lower()}.fit" for f in RGB_FILTERS]
        crop_to_common_coverage(channel_paths, final)
        cropped_shape = fits.getdata(channel_paths[0], memmap=False).shape
        _log(f"[run ] cropped channels to common coverage {cropped_shape}", notes)

        _log("[run ] rgbcomp at native resolution", notes)
        run_script(["rgbcomp red green blue -out=rgb_native"], workdir=final)
    else:
        _log("[skip] rgb_native.fit already present", notes)

    rgb_bg = final / "rgb_native_bg.fits"
    if not usable(rgb_bg, notes):
        _log("[run ] GraXpert background extraction on RGB", notes)
        rgb_bg = run_graxpert_background_extraction(rgb_native, output_stem="rgb_native_bg")
    else:
        _log("[skip] RGB background extraction already done", notes)

    pcc_marker = final / "rgb_pcc.fit"
    if not usable(pcc_marker, notes):
        shutil.copy2(rgb_bg, pcc_marker)
        _log("[run ] PCC colour calibration", notes)
        pcc = run_pcc(pcc_marker, final)
        _log(f"       PCC used {pcc.stars_used} stars, white balance {pcc.white_balance}", notes)

    else:
        _log("[skip] PCC already done", notes)

    # --- luminance: background extraction --------------------------------
    lum_bg = final / "lum_bg.fits"
    if not usable(lum_bg, notes):
        shutil.copy2(result.masters[LUMINANCE_FILTER], final / "lum.fit")
        _log("[run ] GraXpert background extraction on L", notes)
        lum_bg = run_graxpert_background_extraction(final / "lum.fit", output_stem="lum_bg")
    else:
        _log("[skip] L background extraction already done", notes)

    # --- reproject colour up onto L's grid -------------------------------
    rgb_reconciled = final / "rgb_reconciled.fit"
    if not usable(rgb_reconciled, notes):
        _log("[run ] reprojecting colour onto L's pixel grid", notes)
        recon = reproject_to_reference(pcc_marker, lum_bg, rgb_reconciled)
        _log(f"       footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f}", notes)
    else:
        _log("[skip] reprojection already done", notes)

    # --- stretch + LRGB composition --------------------------------------
    composite = final / "lrgb_final.fit"
    if not usable(composite, notes):
        lum_in = final / "lum_for_compose.fit"
        rgb_in = final / "rgb_for_compose.fit"
        shutil.copy2(lum_bg, lum_in)
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
        ("02_rgb_native", rgb_native, True),
        ("03_rgb_background_extracted", Path(rgb_bg), True),
        ("04_rgb_colour_calibrated", pcc_marker, True),
        ("05_lum_background_extracted", Path(lum_bg), True),
        ("06_rgb_reconciled", rgb_reconciled, True),
        ("07_lrgb_final", Path(composite), False),  # post-stretch: render faithfully
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
