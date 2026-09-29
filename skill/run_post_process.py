"""Optional, non-destructive post-processing on an already-finished final
composite: star removal (nebula targets), GraXpert denoise, and black-point
darkening (capabilities C/D, 2026-09).

Never touches the original `<target>_lrgb.tif`/`<target>_rgb.tif` or its
`_preview.png` -- every output here is a new, separately-named file in the
same `final/` directory, so nothing already delivered is at risk.

Two real, distinct chains, chosen by `--nebula`:

  - Galaxy (default, omit --nebula): denoise the full composite (stars
    included -- a galaxy's star field isn't a thing to remove), then
    export with the given black point.
      <target>_denoised_darkened.tif

  - Nebula (--nebula): star-removal FIRST on the original composite
    (denoising blurs faint stars and degrades detection), then denoise
    the STARLESS result, then export with the given black point. The
    star layer is exported standalone (faithful, no denoise/darken) for
    optional manual recombination in your own image editor --
    deliberately not auto-recombined, same scope boundary as every
    other creative step this skill declines to automate.
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
from astro_pipeline import preflight  # noqa: E402


CANDIDATE_STEMS = [
    "lrgb_haboost_final", "rgb_haboost_final", "lrgb_final", "rgb_final", "sho_final", "hoo_final",
]


def _tag_for(stem: str) -> str | None:
    """None means "keep today's exact output naming" (lrgb_final/
    rgb_final -- the original two candidates, unchanged since before
    narrowband/boost existed). Every other real input gets a tag inserted
    into every output filename, so a boosted/narrowband run's outputs
    never collide with a plain LRGB/RGB-only run's."""
    if stem in ("lrgb_final", "rgb_final"):
        return None
    if stem in ("lrgb_haboost_final", "rgb_haboost_final"):
        return "haboost"
    # "sho_final" -> "sho", "hoo_final" -> "hoo"; an unrecognized stem (an
    # explicit --input with a custom filename) falls through to using the
    # whole stem as its own tag, rather than silently using no tag -- that
    # would risk colliding with a real plain-LRGB output for this target.
    return stem.split("_", 1)[0]


def resolve_input_composite(final_dir: Path, explicit_input: str | None) -> tuple[Path, str | None]:
    """(path, tag) for whichever final composite this run should
    post-process. `--input` bypasses auto-detection entirely. Otherwise:
    each candidate stem is looked for in `final/` then `final/
    _intermediate/` (final/ wins if both exist for the SAME stem -- an
    _intermediate/ copy is presumed stale once a fresh one lands in
    final/); if MULTIPLE DISTINCT stems are present (e.g. a target that
    has both a plain LRGB composite and a later narrowband-boost one),
    that's a real ambiguity -- ask for --input rather than silently
    guessing, since guessing wrong here means denoising/darkening the
    WRONG image with no obvious sign anything went wrong.
    """
    if explicit_input:
        path = Path(explicit_input)
        if not path.exists():
            raise FileNotFoundError(f"--input {path} does not exist.")
        return path, _tag_for(path.stem)

    found: dict[str, Path] = {}
    for stem in CANDIDATE_STEMS:
        name = f"{stem}.fit"
        for candidate_dir in (final_dir, final_dir / "_intermediate"):
            candidate = candidate_dir / name
            if candidate.exists():
                found[stem] = candidate
                break

    if not found:
        raise FileNotFoundError(
            f"No final composite found under {final_dir} or its _intermediate/ "
            f"(looked for: {', '.join(CANDIDATE_STEMS)}). Run the target's LRGB/RGB/"
            "narrowband/boost pipeline first, or pass --input explicitly."
        )
    if len(found) > 1:
        options = "; ".join(f"{stem} ({path})" for stem, path in sorted(found.items()))
        raise RuntimeError(
            f"Multiple final composites found under {final_dir} -- ambiguous which one to "
            f"post-process: {options}. Pass --input <path> to pick one explicitly."
        )
    [(stem, path)] = found.items()
    return path, _tag_for(stem)


def _black_point(value: str) -> float:
    parsed = float(value)
    if not (0.0 <= parsed < 1.0):
        raise argparse.ArgumentTypeError(f"--black-point must be in [0, 1) -- got {parsed} (0.0 means no darkening)")
    return parsed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Optional post-processing on an already-finished final composite: star "
        "removal (--nebula), GraXpert denoise, and black-point export. Every output is a new, "
        "separately-named file -- nothing already delivered is touched.",
    )
    p.add_argument("final_dir", help="the target's _pipeline/final directory")
    p.add_argument("--target-name", required=True, help='output file stem, e.g. "M51"')
    p.add_argument(
        "--input", default=None,
        help="explicit path to the composite FITS to post-process, if more than one final "
        "composite exists under final_dir (e.g. a plain LRGB run alongside a later "
        "narrowband-boost run) and auto-detection can't pick one",
    )
    p.add_argument("--nebula", action="store_true", help="run star removal before denoising")
    p.add_argument("--black-point", type=_black_point, required=True)
    p.add_argument("--denoise-gpu", action="store_true", help="attempt GPU denoise (may crash/hang on older or integrated GPUs -- CPU is the safe default)")
    p.add_argument("--graxpert-exe", default=None)
    p.add_argument("--starnet-exe", default=None)
    p.add_argument(
        "--skip-preflight", action="store_true",
        help="skip the tool check before running",
    )
    args = p.parse_args(argv)

    final_dir = Path(args.final_dir)
    graxpert_exe = Path(args.graxpert_exe) if args.graxpert_exe else None
    starnet_exe = Path(args.starnet_exe) if args.starnet_exe else None

    if not final_dir.exists():
        print(f"{final_dir} does not exist.")
        return 2

    if not args.skip_preflight:
        problems = preflight.check_prerequisites(
            needs_graxpert=True,
            needs_starnet=args.nebula,
            graxpert_exe=graxpert_exe,
            starnet_exe=starnet_exe,
        )
        if problems:
            print("Cannot run -- fix these first (or pass --skip-preflight):")
            for problem in problems:
                print(f"  - {problem}")
            return 2

    composite, tag = resolve_input_composite(final_dir, args.input)
    print(f"[run ] source composite: {composite}" + (f" (tag={tag!r})" if tag else ""))
    name_stem = f"{args.target_name}_{tag}" if tag else args.target_name

    if args.nebula:
        starless_fits = final_dir / "_intermediate" / f"{name_stem}_starless.fit"
        stars_fits = final_dir / "_intermediate" / f"{name_stem}_stars.fit"
        if starless_fits.exists() and stars_fits.exists():
            print(f"[skip] star removal already done -- reusing {starless_fits.name}")

            class _Resumed:
                starless_path = starless_fits
                stars_path = stars_fits

            star_result = _Resumed()
        else:
            print("[run ] star removal (on the original, un-denoised composite)")
            star_result = run_star_removal(
                composite, output_dir=final_dir / "_intermediate", output_stem=name_stem,
                starnet_exe=starnet_exe,
            )
            starless_tiff = export(star_result.starless_path, output_dir=final_dir, stem=f"{name_stem}_starless")
            stars_tiff = export(star_result.stars_path, output_dir=final_dir, stem=f"{name_stem}_stars")
            print(f"       starless TIFF: {starless_tiff.tiff_path}")
            print(f"       stars TIFF:    {stars_tiff.tiff_path}")

        print("[run ] denoising the starless composite")
        denoise_input = star_result.starless_path
        output_stem = f"{name_stem}_starless_denoised"
    else:
        print("[run ] denoising the full composite")
        denoise_input = composite
        output_stem = f"{name_stem}_denoised"

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
