"""CLI driver SKILL.md uses to actually call run_lrgb stage-by-stage
(Slice 4.4).

Not pipeline logic -- a thin, testable wrapper so the skill's instructions
don't ask the assistant to hand-type a fresh Python snippet into Bash at
every checkpoint (error-prone, unreviewable, and each typo would be a
silent divergence from the real `run_lrgb` calling convention in
examples/run_lrgb_example.py). One call = one `run_lrgb` invocation with a given
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

from astro_pipeline.calibration import DEFAULT_PEDESTAL, CalibrationMode, FlatPolicy  # noqa: E402
from astro_pipeline.lrgb_orchestrator import run_lrgb  # noqa: E402
from astro_pipeline import preflight  # noqa: E402


def _parse_lum_source(value: str) -> tuple[str, int]:
    """Used as --lum-source's `type=` -- a bad value raises
    ArgumentTypeError DURING parse_args(), which argparse turns into its
    own normal usage error (exit 2), not a raw traceback after parsing
    already finished."""
    telescope, _, binning = value.partition(":")
    if not binning:
        raise argparse.ArgumentTypeError(
            f"--lum-source must be TELESCOPE:BINNING (e.g. T21:1), got {value!r}"
        )
    try:
        return telescope, int(binning)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--lum-source's BINNING must be an integer, got {binning!r} in {value!r}"
        ) from None


def _parse_calibration_mode_item(value: str) -> tuple[str, CalibrationMode]:
    """Used as --calibration-mode's `type=`, applied to each repeated
    `TEL=raw_local|precalibrated` occurrence individually (a genuine
    multi-telescope override, since one LRGB run can have a different
    Luminance telescope and colour telescope) -- `main()` collects the
    resulting (telescope, mode) pairs into the
    `dict[str, CalibrationMode]` shape `run_lrgb`/`LRGBOrchestrator`
    already accept."""
    telescope, sep, mode = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"--calibration-mode must be TEL=raw_local|precalibrated, got {value!r}"
        )
    try:
        return telescope, CalibrationMode(mode)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--calibration-mode's value must be 'raw_local' or 'precalibrated', got {mode!r} in {value!r}"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the LRGB/RGB-only/OSC pipeline stage-by-stage for one project folder, "
        "printing notes/checkpoints/preview paths as it goes.",
    )
    p.add_argument("project_dir", help="the target's project folder (raw iTelescope delivery)")
    p.add_argument("--telescope", required=True, help="primary telescope (RGB discovery scope + lum fallback)")
    p.add_argument("--target", required=True)
    p.add_argument("--ra-hours", type=float, required=True)
    p.add_argument("--dec-deg", type=float, required=True)
    p.add_argument("--lum-binning", type=int, default=1)
    p.add_argument("--rgb-binning", type=int, default=2)
    p.add_argument("--stretch-method", choices=["autostretch", "autoghs", "autoghs+auto"], default="autostretch")
    p.add_argument("--lum-source", type=_parse_lum_source, default=None, help="explicit override, TELESCOPE:BINNING")
    p.add_argument("--pedestal", type=float, default=DEFAULT_PEDESTAL)
    p.add_argument(
        "--stop-after", choices=["masters", "reconciled", "final"], default=None,
        help="omit for a full run",
    )
    p.add_argument(
        "--force", action="append", default=[], choices=["masters", "reconciled", "final"],
        help="repeatable; invalidates the named stage and every stage after it",
    )
    p.add_argument(
        "--calibration-mode", action="append", default=[], type=_parse_calibration_mode_item,
        metavar="TEL=raw_local|precalibrated",
        help="repeatable per-telescope override, e.g. --calibration-mode T20=raw_local",
    )
    p.add_argument(
        "--calibration-header-fallback", action="store_true",
        help="opt-in header-based recognition of calibration frames a filename pattern can't see",
    )
    p.add_argument(
        "--flat-policy", choices=["require", "skip_if_missing"], default=None,
        help="override every telescope's inferred FlatPolicy uniformly for this run",
    )
    p.add_argument(
        "--skip-preflight", action="store_true",
        help="skip the tool/Siril-version/SPCC-profile check before running",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    project_dir_path = Path(args.project_dir)
    if not project_dir_path.exists():
        print(f"{project_dir_path} does not exist.")
        return 2

    if not args.skip_preflight:
        problems = preflight.check_prerequisites(
            project_dir=args.project_dir,
            telescope=args.telescope,
            target=args.target,
            needs_siril=True,
            needs_astap=True,
            needs_graxpert=True,
            needs_spcc=True,
            calibration_header_fallback=args.calibration_header_fallback,
        )
        if problems:
            print("Cannot run -- fix these first (or pass --skip-preflight):")
            for problem in problems:
                print(f"  - {problem}")
            return 2

    result = run_lrgb(
        project_dir=args.project_dir,
        telescope=args.telescope,
        target=args.target,
        ra_hours=args.ra_hours,
        dec_deg=args.dec_deg,
        lum_binning=args.lum_binning,
        rgb_binning=args.rgb_binning,
        stretch_method=args.stretch_method,
        lum_source=args.lum_source,  # already parsed via --lum-source's type=
        pedestal=args.pedestal,
        stop_after=args.stop_after,
        force=set(args.force) or None,
        calibration_mode=dict(args.calibration_mode) if args.calibration_mode else None,
        calibration_header_fallback=args.calibration_header_fallback,
        flat_policy=FlatPolicy(args.flat_policy) if args.flat_policy else None,
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
