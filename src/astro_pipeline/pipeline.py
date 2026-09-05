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

from astropy.io import fits

from .background_color import run_graxpert_background_extraction, run_pcc
from .calibration import run_calibration
from .export_image import ExportResult, export
from .ingest import scan_session
from .reconciliation import reproject_to_reference
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
    notes: list[str] = field(default_factory=list)


def _log(message: str, notes: list[str]) -> None:
    print(message, flush=True)
    notes.append(message)


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

    if master_path.exists() and fits.getheader(master_path).get("PLTSOLVD"):
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
    if not rgb_native.exists():
        for filter_name in RGB_FILTERS:
            shutil.copy2(result.masters[filter_name], final / f"{filter_name.lower()}.fit")
        _log("[run ] rgbcomp at native resolution", notes)
        run_script(["rgbcomp red green blue -out=rgb_native"], workdir=final)
    else:
        _log("[skip] rgb_native.fit already present", notes)

    rgb_bg = final / "rgb_native_bg.fits"
    if not rgb_bg.exists():
        _log("[run ] GraXpert background extraction on RGB", notes)
        rgb_bg = run_graxpert_background_extraction(rgb_native, output_stem="rgb_native_bg")
    else:
        _log("[skip] RGB background extraction already done", notes)

    pcc_marker = final / "rgb_pcc.fit"
    if not pcc_marker.exists():
        shutil.copy2(rgb_bg, pcc_marker)
        _log("[run ] PCC colour calibration", notes)
        pcc = run_pcc(pcc_marker, final)
        _log(f"       PCC used {pcc.stars_used} stars, white balance {pcc.white_balance}", notes)
    else:
        _log("[skip] PCC already done", notes)

    # --- luminance: background extraction --------------------------------
    lum_bg = final / "lum_bg.fits"
    if not lum_bg.exists():
        shutil.copy2(result.masters[LUMINANCE_FILTER], final / "lum.fit")
        _log("[run ] GraXpert background extraction on L", notes)
        lum_bg = run_graxpert_background_extraction(final / "lum.fit", output_stem="lum_bg")
    else:
        _log("[skip] L background extraction already done", notes)

    # --- reproject colour up onto L's grid -------------------------------
    rgb_reconciled = final / "rgb_reconciled.fit"
    if not rgb_reconciled.exists():
        _log("[run ] reprojecting colour onto L's pixel grid", notes)
        recon = reproject_to_reference(pcc_marker, lum_bg, rgb_reconciled)
        _log(f"       footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f}", notes)
    else:
        _log("[skip] reprojection already done", notes)

    # --- stretch + LRGB composition --------------------------------------
    composite = final / "lrgb_final.fit"
    if not composite.exists():
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
    return result
