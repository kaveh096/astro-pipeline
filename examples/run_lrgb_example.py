"""Worked example: calling run_lrgb() directly, without the Claude Code
skill or the interview step.

Resumable: re-run after an interruption and completed stages are skipped.
Edit PROJECT_DIR, TELESCOPE, TARGET, RA_HOURS and DEC_DEG below for your
own project folder before running. See skill/SKILL.md and README.md's
"Quick start" section for the CLI entry points this same call sits behind.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.lrgb_orchestrator import run_lrgb

PROJECT_DIR = Path(r"C:\path\to\your\Target - Telescope(s) - Date")
TELESCOPE = "T24"
TARGET = "YourTarget"

# Decimal hours / decimal degrees -- look these up on Simbad or a
# planetarium app. Passed explicitly so the run needs no network name
# resolution.
RA_HOURS = 0.0
DEC_DEG = 0.0

if __name__ == "__main__":
    result = run_lrgb(
        PROJECT_DIR,
        telescope=TELESCOPE,
        target=TARGET,
        ra_hours=RA_HOURS,
        dec_deg=DEC_DEG,
    )
    print()
    print("composite:", result.composite_path)
    if result.export_result:
        print("tiff:     ", result.export_result.tiff_path)
        print("preview:  ", result.export_result.preview_path)
