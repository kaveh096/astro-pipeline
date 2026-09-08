"""CLI driver SKILL.md uses to actually call run_lrgb stage-by-stage
(Slice 4.4).

Not pipeline logic -- a thin, testable wrapper so the skill's instructions
don't ask the assistant to hand-type a fresh Python snippet into Bash at
every checkpoint (error-prone, unreviewable, and each typo would be a
silent divergence from the real `run_lrgb` calling convention in
scripts/run_m51.py). One call = one `run_lrgb` invocation with a given
`stop_after`, printing:

  - every `result.notes` line, verbatim -- this project's own established
    log format (Luminance-selection reasoning, gain/offset fit numbers,
    dark-scaling notes, [skip]/[run] lines) is already human-readable;
    this script doesn't reformat it, just relays it.
  - each checkpoint's own `.summary()` text (background/noise/SNR/stars,
    warnings, comparison-to-previous).
  - each checkpoint's preview PNG path, under a PREVIEWS: section, so the
    assistant can Read() them as images without re-deriving paths from
    `_pipeline/checkpoints/`.
  - the composite/TIFF/preview paths once a run reaches that far.

`--lum-source TELESCOPE:BINNING` and `--force STAGE` (repeatable) expose
exactly the two knobs plan-rev4.md's Slice 4.4 spec asks the skill's
"Adjust" menu option to drive -- both already real `run_lrgb` parameters
(Slice 2's override, Slice 4.3's cascade), not new pipeline behaviour.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.calibration import DEFAULT_PEDESTAL  # noqa: E402
from astro_pipeline.pipeline import run_lrgb  # noqa: E402


def _parse_lum_source(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    telescope, _, binning = value.partition(":")
    if not binning:
        raise argparse.ArgumentTypeError(
            f"--lum-source must be TELESCOPE:BINNING (e.g. T21:1), got {value!r}"
        )
    return telescope, int(binning)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project_dir")
    p.add_argument("--telescope", required=True, help="primary telescope (RGB discovery scope + lum fallback)")
    p.add_argument("--target", required=True)
    p.add_argument("--ra-hours", type=float, required=True)
    p.add_argument("--dec-deg", type=float, required=True)
    p.add_argument("--lum-binning", type=int, default=1)
    p.add_argument("--rgb-binning", type=int, default=2)
    p.add_argument("--stretch-method", default="autostretch")
    p.add_argument("--lum-source", default=None, help="explicit override, TELESCOPE:BINNING")
    p.add_argument("--pedestal", type=float, default=DEFAULT_PEDESTAL)
    p.add_argument(
        "--stop-after", choices=["masters", "reconciled", "final"], default=None,
        help="omit for a full run",
    )
    p.add_argument(
        "--force", action="append", default=[], choices=["masters", "reconciled", "final"],
        help="repeatable; invalidates the named stage and every stage after it",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    result = run_lrgb(
        project_dir=args.project_dir,
        telescope=args.telescope,
        target=args.target,
        ra_hours=args.ra_hours,
        dec_deg=args.dec_deg,
        lum_binning=args.lum_binning,
        rgb_binning=args.rgb_binning,
        stretch_method=args.stretch_method,
        lum_source=_parse_lum_source(args.lum_source),
        pedestal=args.pedestal,
        stop_after=args.stop_after,
        force=set(args.force) or None,
    )

    print("=== NOTES ===")
    for line in result.notes:
        print(line)

    print()
    print("=== CHECKPOINTS ===")
    for cp in result.checkpoints:
        print(cp.summary())
        print()

    print("=== PREVIEWS ===")
    for cp in result.checkpoints:
        if cp.preview_path:
            print(f"{cp.label}\t{cp.preview_path}")

    if result.composite_path:
        print()
        print(f"COMPOSITE\t{result.composite_path}")
    if result.export_result:
        print(f"TIFF\t{result.export_result.tiff_path}")
        print(f"PREVIEW_PNG\t{result.export_result.preview_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
