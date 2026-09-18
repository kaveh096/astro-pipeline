"""Narrowband-boost (HaRGB) post-processing: blend a narrowband channel
into an already-completed LRGB/RGB run's composite (capability A2,
2026-09). See src/astro_pipeline/narrowband_boost.py for the real,
sourced technique and Siril `pm`/`save` mechanics.

Requires the target's LRGB (or RGB-only) run to have already produced,
under `_pipeline/final/` or `_pipeline/final/_intermediate/`:
  - rgb_reconciled.fit  (the linear RGB composite -- already reprojected
                          onto Luminance's grid, if this is an LRGB
                          target)
  - lum_bg.fits         (the background-extracted Luminance -- omit
                          --no-luminance for an RGB-only target instead,
                          which has no Luminance to recompose with)

Builds a new master for --boost-filter (default Ha) at the SAME binning
as the RGB masters (via `build_single_filter_master`), reprojects it
onto the reconciled RGB's own grid, splits the RGB into channels, boosts
the chosen channel via Siril's real `pm` blend, recombines via
`rgbcomp`, and (unless --no-luminance) re-composes with the existing
Luminance via `stretch_and_compose` -- otherwise stretches the boosted
RGB alone via `stretch_rgb`.

Non-destructive: every output is a NEW file under `final/` --
`<target>_lrgb_haboost.tif` (or `_rgb_haboost.tif` for --no-luminance),
plus the raw registered narrowband layer and the raw boosted channel,
exported standalone as real ingredients for further manual tuning in
Photoshop if the automated blend ratio isn't to taste.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.export_image import export  # noqa: E402
from astro_pipeline.ingest import scan_session  # noqa: E402
from astro_pipeline.master_builder import build_single_filter_master  # noqa: E402
from astro_pipeline.narrowband_boost import (  # noqa: E402
    CHANNEL_INDEX,
    DEFAULT_BOOST_FACTOR,
    boost_channel_with_narrowband,
    rescale_narrowband_to_reference,
    split_rgb_channels,
)
from astro_pipeline.narrowband_filters import _NarrowbandNormalizingReport  # noqa: E402
from astro_pipeline.reconciliation import reproject_to_reference  # noqa: E402
from astro_pipeline.siril_driver import run_script  # noqa: E402
from astro_pipeline.stretch_compose import stretch_and_compose, stretch_rgb  # noqa: E402
from astro_pipeline.workspace import pipeline_dir  # noqa: E402


def _find_existing(final_dir: Path, name: str) -> Path:
    for candidate in (final_dir / "_intermediate" / name, final_dir / name):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"{name} not found under {final_dir} or its _intermediate/ -- run the target's "
        "LRGB/RGB pipeline first (skill/run_stage.py)."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project_dir")
    p.add_argument("--telescope", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--ra-hours", type=float, required=True)
    p.add_argument("--dec-deg", type=float, required=True)
    p.add_argument("--boost-filter", default="Ha")
    p.add_argument("--boost-channel", default="red", choices=sorted(CHANNEL_INDEX))
    p.add_argument("--boost-factor", type=float, default=DEFAULT_BOOST_FACTOR)
    p.add_argument("--binning", type=int, default=2)
    p.add_argument("--stretch-method", default="autostretch")
    p.add_argument("--no-luminance", action="store_true", help="RGB-only target: stretch the boosted RGB alone")
    args = p.parse_args(argv)

    project_dir = Path(args.project_dir)
    final_dir = pipeline_dir(project_dir) / "final"
    work_dir = final_dir / "_intermediate" / "narrowband_boost"
    work_dir.mkdir(parents=True, exist_ok=True)

    rgb_reconciled = _find_existing(final_dir, "rgb_reconciled.fit")
    print(f"[run ] source reconciled RGB: {rgb_reconciled}")

    notes: list[str] = []
    raw_report = scan_session(project_dir)
    report = _NarrowbandNormalizingReport(raw_report)

    print(f"[run ] building {args.boost_filter} master (BIN{args.binning})")
    narrowband_master = build_single_filter_master(
        project_dir, report, args.telescope, args.target, args.boost_filter, args.binning,
        args.ra_hours, args.dec_deg, notes,
    )
    for line in notes:
        print("      ", line)
    if narrowband_master is None:
        raw_filters_seen = sorted({
            filt for (t, tgt, filt, b) in raw_report.instrument_groups()
            if t == args.telescope and tgt == args.target and b == args.binning
        })
        print(f"[error] no usable {args.boost_filter} master -- real FILTER strings seen: {raw_filters_seen}")
        return 1

    print("[run ] splitting reconciled RGB into channels")
    red, green, blue = split_rgb_channels(rgb_reconciled, work_dir)
    channels = {"red": red, "green": green, "blue": blue}
    target_channel = channels[args.boost_channel]

    print(f"[run ] reprojecting {args.boost_filter} onto the RGB composite's grid")
    reprojected_narrowband = work_dir / f"{args.boost_filter.lower()}_reprojected.fit"
    reproject_to_reference(narrowband_master, target_channel, reprojected_narrowband)

    # REAL FINDING (M42/T20, this session): raw Siril `stack` output has
    # no background subtraction or cross-filter normalization applied --
    # a real Red master and a real Ha master can land on near-identical
    # absolute pixel scales, in which case `max(Red, Ha*k)` at any
    # realistic k boosts ZERO pixels (confirmed: 0% at k=0.4 on real M42
    # data). The narrowband layer is re-expressed in the TARGET channel's
    # own real units before blending -- the target channel itself is
    # never rescaled, so its real relationship to the other two RGB
    # channels (needed for a correct rgbcomp downstream) is preserved.
    # See rescale_narrowband_to_reference's own docstring.
    print(f"[run ] rescaling {args.boost_filter} into {args.boost_channel}'s own real units")
    rescaled_narrowband = work_dir / f"{args.boost_filter.lower()}_rescaled_to_{args.boost_channel}.fit"
    _, channel_median = rescale_narrowband_to_reference(reprojected_narrowband, target_channel, rescaled_narrowband)

    print(f"[run ] boosting {args.boost_channel} with {args.boost_filter} (factor={args.boost_factor})")
    boosted_channel = boost_channel_with_narrowband(
        target_channel, rescaled_narrowband, work_dir,
        output_stem=f"boosted_{args.boost_channel}", boost_factor=args.boost_factor,
        channel_median=channel_median,
    )
    channels[args.boost_channel] = boosted_channel

    print("[run ] recombining boosted channels via rgbcomp")
    for name, path in (("red", channels["red"]), ("green", channels["green"]), ("blue", channels["blue"])):
        shutil.copy2(path, work_dir / f"{name}.fit")
    run_script(["rgbcomp red green blue -out=rgb_boosted"], workdir=work_dir)
    rgb_boosted = work_dir / "rgb_boosted.fit"

    composite_stem = f"{'rgb' if args.no_luminance else 'lrgb'}_haboost_final"
    if args.no_luminance:
        print(f"[run ] stretch ({args.stretch_method}), RGB-only (no Luminance to compose)")
        compose = stretch_rgb(rgb_boosted, final_dir, output_stem=composite_stem, method=args.stretch_method)
    else:
        lum_bg = _find_existing(final_dir, "lum_bg.fits")
        print(f"[run ] stretch ({args.stretch_method}) + rgbcomp -lum (Luminance: {lum_bg})")
        compose = stretch_and_compose(
            lum_bg, rgb_boosted, final_dir, output_stem=composite_stem, method=args.stretch_method,
        )
    composite = compose.composite_path

    suffix = "rgb_haboost" if args.no_luminance else "lrgb_haboost"
    final_result = export(composite, output_dir=final_dir, stem=f"{args.target}_{suffix}")
    print(f"[done] {final_result.tiff_path}")
    print(
        f"       clipped low {final_result.clipped_low_fraction:.4f} / "
        f"high {final_result.clipped_high_fraction:.4f}"
    )

    # Real ingredients for manual Photoshop tuning, if the automated blend
    # ratio isn't to taste -- exported standalone, faithful (no stretch
    # beyond the linear->display mapping export() itself applies).
    narrowband_layer = export(
        reprojected_narrowband, output_dir=final_dir, stem=f"{args.target}_{args.boost_filter.lower()}_layer"
    )
    print(f"       raw {args.boost_filter} layer (registered): {narrowband_layer.tiff_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
