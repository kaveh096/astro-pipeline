"""CLI driver for run_narrowband() (pure SHO/HOO narrowband composites),
mirroring run_stage.py's own rationale: one call = one `run_narrowband`
invocation, printing `result.notes`/`result.checkpoints` the same way, so
SKILL.md doesn't ask for a hand-typed Python snippet at each checkpoint.

Distinct from run_stage.py (LRGB/RGB-only): no `--stop-after`/`--force`
stage vocabulary -- run_narrowband has exactly one force switch (rebuild
everything) and no reconciliation-tier boundary, since there is only ever
one colour contributor (see run_narrowband's own docstring).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.calibration import DEFAULT_PEDESTAL, CalibrationMode, FlatPolicy  # noqa: E402
from astro_pipeline.narrowband_filters import NARROWBAND_PALETTES  # noqa: E402
from astro_pipeline.narrowband_orchestrator import run_narrowband  # noqa: E402
from astro_pipeline import preflight  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build a pure narrowband (SHO/HOO) false-colour composite for one project "
        "folder, printing notes/checkpoints/preview paths as it goes.",
    )
    p.add_argument("project_dir", help="the target's project folder (raw iTelescope delivery)")
    p.add_argument("--telescope", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--ra-hours", type=float, required=True)
    p.add_argument("--dec-deg", type=float, required=True)
    p.add_argument("--palette", choices=sorted(NARROWBAND_PALETTES), default="sho")
    p.add_argument("--binning", type=int, default=2)
    p.add_argument("--stretch-method", choices=["autostretch", "autoghs", "autoghs+auto"], default="autostretch")
    p.add_argument("--pedestal", type=float, default=DEFAULT_PEDESTAL)
    p.add_argument("--force", action="store_true", help="rebuild everything, ignoring what's already on disk")
    p.add_argument(
        "--calibration-mode", choices=["raw_local", "precalibrated"], default=None,
        help="override this run's single telescope's inferred CalibrationMode (no TEL= prefix -- "
        "this entry point already pins exactly one --telescope)",
    )
    p.add_argument(
        "--calibration-header-fallback", action="store_true",
        help="opt-in header-based recognition of calibration frames a filename pattern can't see (Step 4b)",
    )
    p.add_argument(
        "--flat-policy", choices=["require", "skip_if_missing"], default=None,
        help="override this run's inferred FlatPolicy",
    )
    p.add_argument(
        "--skip-preflight", action="store_true",
        help="skip the tool/Siril-version check before running",
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
            needs_siril=True,
            needs_astap=True,
            needs_graxpert=True,
        )
        if problems:
            print("Cannot run -- fix these first (or pass --skip-preflight):")
            for problem in problems:
                print(f"  - {problem}")
            return 2

    result = run_narrowband(
        project_dir=args.project_dir,
        telescope=args.telescope,
        target=args.target,
        ra_hours=args.ra_hours,
        dec_deg=args.dec_deg,
        palette=args.palette,
        binning=args.binning,
        stretch_method=args.stretch_method,
        pedestal=args.pedestal,
        force=args.force,
        calibration_mode=CalibrationMode(args.calibration_mode) if args.calibration_mode else None,
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
