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

from astro_pipeline.calibration import DEFAULT_PEDESTAL  # noqa: E402
from astro_pipeline.pipeline import NARROWBAND_PALETTES, run_narrowband  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project_dir")
    p.add_argument("--telescope", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--ra-hours", type=float, required=True)
    p.add_argument("--dec-deg", type=float, required=True)
    p.add_argument("--palette", choices=sorted(NARROWBAND_PALETTES), default="sho")
    p.add_argument("--binning", type=int, default=2)
    p.add_argument("--stretch-method", default="autostretch")
    p.add_argument("--pedestal", type=float, default=DEFAULT_PEDESTAL)
    p.add_argument("--force", action="store_true", help="rebuild everything, ignoring what's already on disk")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
