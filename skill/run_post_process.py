"""Optional, non-destructive post-processing on an already-finished final
composite: star removal (nebula targets), GraXpert denoise, and black-point
darkening (capabilities C/D, 2026-09).

Never touches the original `<target>_lrgb.tif`/`<target>_rgb.tif` or its
`_preview.png` -- every output here is a new, separately-named file in the
same `final/` directory, so nothing already delivered is at risk.

Two real, distinct chains, chosen by `--nebula`:

  - Galaxy (--no-nebula, default): denoise the full composite (stars
    included -- a galaxy's star field isn't a thing to remove), then
    export with the given black point.
      <target>_denoised_darkened.tif

  - Nebula (--nebula): star-removal FIRST on the original composite (not
    the denoised one -- denoising blurs faint stars and degrades
    detection, confirmed during this capability's own development), then
    denoise the STARLESS result (avoids star-blur artifacts entirely),
    then export with the given black point. The star layer is exported
    standalone (faithful, no denoise/darken) for optional manual
    recombination in Photoshop -- deliberately not auto-recombined, same
    scope boundary as every other creative step this skill declines to
    automate.
      <target>_starless.tif          (faithful, standalone)
      <target>_stars.tif             (faithful, standalone)
      <target>_starless_denoised_darkened.tif

`--black-point` has no default baked into the underlying
`export_with_black_point()` on purpose (a genuine aesthetic preference,
not a pipeline default) -- this script requires it explicitly for the
same reason, rather than picking one silently.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.background_color import run_graxpert_denoise  # noqa: E402
from astro_pipeline.export_image import export, export_with_black_point  # noqa: E402
from astro_pipeline.star_removal import run_star_removal  # noqa: E402


def find_final_composite(final_dir: Path) -> Path:
    """The real, already-computed final composite FITS -- lrgb_final.fit
    (LRGB) or rgb_final.fit (RGB-only mode) -- moved into `_intermediate/`
    by this session's own final/-directory cleanup, alongside every other
    working file. Prefers lrgb_final.fit if somehow both exist (should
    never happen for one real target)."""
    candidates = [
        final_dir / "_intermediate" / "lrgb_final.fit",
        final_dir / "_intermediate" / "rgb_final.fit",
        final_dir / "lrgb_final.fit",
        final_dir / "rgb_final.fit",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"No lrgb_final.fit/rgb_final.fit found under {final_dir} or its _intermediate/")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("final_dir", help="the target's _pipeline/final directory")
    p.add_argument("--target-name", required=True, help='output file stem, e.g. "M51", "Abell 31"')
    p.add_argument("--nebula", action="store_true", help="run star removal before denoising")
    p.add_argument("--black-point", type=float, required=True)
    p.add_argument("--denoise-gpu", action="store_true", help="attempt GPU denoise (known unreliable on this machine)")
    p.add_argument("--graxpert-exe", default=None)
    p.add_argument("--starnet-exe", default=None)
    args = p.parse_args(argv)

    final_dir = Path(args.final_dir)
    graxpert_exe = Path(args.graxpert_exe) if args.graxpert_exe else None
    starnet_exe = Path(args.starnet_exe) if args.starnet_exe else None

    composite = find_final_composite(final_dir)
    print(f"[run ] source composite: {composite}")

    if args.nebula:
        starless_fits = final_dir / "_intermediate" / f"{args.target_name}_starless.fit"
        stars_fits = final_dir / "_intermediate" / f"{args.target_name}_stars.fit"
        if starless_fits.exists() and stars_fits.exists():
            print(f"[skip] star removal already done -- reusing {starless_fits.name}")

            class _Resumed:
                starless_path = starless_fits
                stars_path = stars_fits

            star_result = _Resumed()
        else:
            print("[run ] star removal (on the original, un-denoised composite)")
            star_result = run_star_removal(
                composite, output_dir=final_dir / "_intermediate", output_stem=args.target_name,
                starnet_exe=starnet_exe,
            )
            starless_tiff = export(star_result.starless_path, output_dir=final_dir, stem=f"{args.target_name}_starless")
            stars_tiff = export(star_result.stars_path, output_dir=final_dir, stem=f"{args.target_name}_stars")
            print(f"       starless TIFF: {starless_tiff.tiff_path}")
            print(f"       stars TIFF:    {stars_tiff.tiff_path}")

        print("[run ] denoising the starless composite")
        denoise_input = star_result.starless_path
        output_stem = f"{args.target_name}_starless_denoised"
    else:
        print("[run ] denoising the full composite")
        denoise_input = composite
        output_stem = f"{args.target_name}_denoised"

    denoised = run_graxpert_denoise(
        denoise_input, output_stem=output_stem,
        graxpert_exe=graxpert_exe, gpu=args.denoise_gpu, timeout=None,
    )
    print(f"       denoised FITS: {denoised}")

    final_result = export_with_black_point(
        denoised, black_point=args.black_point, output_dir=final_dir,
        stem=f"{output_stem}_darkened",
    )
    print(f"[done] {final_result.tiff_path}")
    print(
        f"       clipped low {final_result.clipped_low_fraction:.4f} / "
        f"high {final_result.clipped_high_fraction:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
