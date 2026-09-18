"""`run_narrowband`: pure narrowband (SHO/HOO) false-colour composite
orchestration for a single telescope+target -- a parallel, deliberately
SEPARATE entry point from `run_lrgb` (`lrgb_orchestrator.py`), not a mode
of it. Structurally simpler than `run_lrgb` on purpose: exactly one colour
contributor, no Luminance, no reconciliation/combine step -- kept as a
plain function, not a class, per `docs/task5-oop-refactor-plan.md`
Section 1's explicit non-uniformity call: forcing a shared base class with
`LRGBOrchestrator` here for symmetry would be over-engineering with no
behavior or coupling benefit.
"""

from __future__ import annotations

from pathlib import Path

from .calibration import (
    DEFAULT_PEDESTAL,
    CalibrationMode,
    FlatPolicy,
)
from .calibration_policy import infer_calibration_mode, infer_flat_policy
from .colour_contributor import ColourContributorBuilder
from .contributor_staleness import _delete_if_exists
from .export_image import export
from .ingest import scan_session
from .checkpoints import checkpoint, save_checkpoints
from .logging_utils import log as _log
from .narrowband_filters import (
    NARROWBAND_PALETTES,
    _NarrowbandNormalizingReport,
    equalize_narrowband_channels,
)
from .pipeline_result import PipelineResult
from .resume_guard import usable
from .stretch_compose import stretch_rgb
from .workspace import pipeline_dir


def run_narrowband(
    project_dir: str | Path,
    telescope: str,
    target: str,
    ra_hours: float,
    dec_deg: float,
    palette: str = "sho",
    binning: int = 2,
    stretch_method: str = "autostretch",
    pedestal: float = DEFAULT_PEDESTAL,
    force: bool = False,
    flat_policy: FlatPolicy | None = None,
    calibration_mode: CalibrationMode | None = None,
) -> PipelineResult:
    """Build a pure narrowband false-colour composite (capability A,
    2026-09) -- SHO ("Hubble palette": SII->R, Ha->G, OIII->B) or HOO
    (bicolor: Ha->R, OIII->G and B). A parallel, deliberately SEPARATE
    entry point from `run_lrgb`, not a mode of it: this run has no
    Luminance, no reconciliation-against-L, and a genuinely different
    colour-calibration story (SPCC skipped -- see
    `_build_colour_contributor`'s `run_colour_calibration` docstring --
    replaced by `equalize_narrowband_channels`'s per-channel balancing,
    not a mode `run_lrgb`'s LRGB-shaped control flow already handles).

    Real, tested combination for M42 (T20): `palette="sho"`,
    `binning=2` (Ha/OIII/SII all ship at BIN2 on T20's real delivery,
    same as the Red/Green/Blue filters -- see plan v3's build order).

    Structurally simpler than `run_lrgb` on purpose: exactly ONE colour
    contributor is ever built here (this target's `filters` triplet at
    the given `telescope`+`binning`; no multi-binning/multi-telescope
    discovery the way Luminance gets in `run_lrgb`, matching the same
    real scope limit `_build_colour_contributor`'s RGB path already has
    for non-primary binnings) -- so there is no reconciliation/combine
    step to write, and `force` is a single on/off switch (delete every
    downstream artifact and rebuild), not the `run_lrgb`'s per-stage
    `{"masters","reconciled","final"}` vocabulary. This means narrowband
    runs do NOT yet have `run_lrgb`'s RunSignature-based automatic
    staleness detection (a parameter change like `palette` or
    `stretch_method` will NOT auto-invalidate a stale composite on a
    resumed run) -- pass `force=True` explicitly after changing a
    parameter. A real, honest limitation, not silently pretended away;
    real signature-tracking integration is legitimate future work once
    this entry point has seen more real use.

    Real FITS `FILTER` header spelling for Ha/OIII/SII varies across the
    wider amateur/network community (this project's own T20 data already
    uses the canonical "Ha"/"OIII"/"SII" spelling, verified directly
    against real headers -- but NINA/SGP/ASIAIR deliveries commonly don't)
    -- lights are looked up through `_NarrowbandNormalizingReport`, which
    normalizes known real-world spelling variants before matching (see
    its own docstring). If no lights match after normalization, the real
    distinct FILTER strings actually present at this telescope/binning
    are logged, so the failure is diagnosable rather than a bare "no
    lights found".

    Output: `<target>_sho.tif`/`<target>_hoo.tif` (never `_lrgb`/`_rgb` --
    a narrowband composite exported under those names would misrepresent
    what produced it).
    """
    if palette not in NARROWBAND_PALETTES:
        raise ValueError(f"palette={palette!r} is not one of {sorted(NARROWBAND_PALETTES)}")
    filters = NARROWBAND_PALETTES[palette]

    project_dir = Path(project_dir)
    out = pipeline_dir(project_dir)
    final = out / "final"
    final.mkdir(parents=True, exist_ok=True)
    result = PipelineResult()
    notes = result.notes

    checkpoint_dir = out / "checkpoints"
    checkpoints_path = checkpoint_dir / f"checkpoints_narrowband_{palette}.json"

    _log(f"=== scanning {project_dir.name} (narrowband, palette={palette}) ===", notes)
    raw_report = scan_session(project_dir)
    report = _NarrowbandNormalizingReport(raw_report)

    resolved_calibration_mode = calibration_mode or infer_calibration_mode(raw_report, telescope)
    resolved_flat_policy = flat_policy if flat_policy is not None else infer_flat_policy(raw_report, telescope)

    contrib_dir = final  # single contributor -- always the primary path, mirrors contributor_dir()'s own rule

    if force:
        for name in (
            "rgb_native.fit", "rgb_native_bg.fits", "rgb_colour_calibrated.fit",
            "rgb_equalized.fit", f"{palette}_final.fit",
        ):
            _delete_if_exists(contrib_dir / name, "force=True", notes)

    builder = ColourContributorBuilder(
        project_dir, contrib_dir, report, telescope, target, binning, ra_hours, dec_deg, notes,
    )
    contributor = builder.build_rgb(
        flat_policy=resolved_flat_policy, calibration_mode=resolved_calibration_mode,
        filters=filters, run_colour_calibration=False,
    )
    if contributor is None:
        raw_filters_seen = sorted({
            filt for (t, tgt, filt, b) in raw_report.instrument_groups()
            if t == telescope and tgt == target and b == binning
        })
        raise RuntimeError(
            f"No usable narrowband contributor for {telescope}/{target}/bin{binning} -- "
            f"palette {palette!r} needs {filters}. Real FILTER header strings actually "
            f"present at this telescope/binning: {raw_filters_seen or '(none)'}"
        )
    result.masters[f"narrowband_{palette}"] = contributor.composite_path

    cp = checkpoint(
        contributor.composite_path, "02_narrowband_colour_calibrated", output_dir=checkpoint_dir,
    )
    result.checkpoints.append(cp)
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    equalized_path = contrib_dir / "rgb_equalized.fit"
    if not usable(equalized_path, notes):
        _log("[run ] per-channel background/scale equalization (narrowband colour substitute for SPCC)", notes)
        stats = equalize_narrowband_channels(contributor.composite_path, equalized_path)
        _log(f"       {', '.join(f'{k}={v:.4f}' for k, v in stats.items())}", notes)
    else:
        _log("[skip] narrowband channel equalization already done", notes)

    cp = checkpoint(
        equalized_path, "03_narrowband_equalized", output_dir=checkpoint_dir,
        previous=cp.stats, previous_linear=True,
    )
    result.checkpoints.append(cp)
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    composite_name = f"{palette}_final"
    composite = final / f"{composite_name}.fit"
    if not usable(composite, notes):
        _log(f"[run ] stretch ({stretch_method}), narrowband (no Luminance to compose)", notes)
        compose = stretch_rgb(equalized_path, final, output_stem=composite_name, method=stretch_method)
        composite = compose.composite_path
    else:
        _log(f"[skip] {palette.upper()} composite already present", notes)
    result.composite_path = composite

    cp = checkpoint(
        composite, f"04_{palette}_final", output_dir=checkpoint_dir,
        linear=False, previous=cp.stats, previous_linear=False,
    )
    result.checkpoints.append(cp)
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    _log("[run ] export TIFF + preview", notes)
    result.export_result = export(composite, output_dir=final, stem=f"{target}_{palette}")
    _log(
        f"       row order {result.export_result.row_order}, "
        f"clipped low {result.export_result.clipped_low_fraction:.4f} / "
        f"high {result.export_result.clipped_high_fraction:.4f}",
        notes,
    )

    return result
