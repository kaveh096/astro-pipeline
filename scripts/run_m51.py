"""Regenerate the M51 T24 LRGB result into the project's _pipeline/ folder.

Resumable: re-run after an interruption and completed stages are skipped.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.pipeline import run_lrgb

PROJECT_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025")

# M51: RA 13h29m52.7s, Dec +47:11:43 -- passed explicitly so the run needs
# no network name resolution.
M51_RA_HOURS = 13.4980
M51_DEC_DEG = 47.1953

if __name__ == "__main__":
    result = run_lrgb(
        PROJECT_DIR,
        telescope="T24",
        user="kaveh096",
        target="M51",
        ra_hours=M51_RA_HOURS,
        dec_deg=M51_DEC_DEG,
    )
    print()
    print("composite:", result.composite_path)
    if result.export_result:
        print("tiff:     ", result.export_result.tiff_path)
        print("preview:  ", result.export_result.preview_path)
