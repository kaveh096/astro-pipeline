"""Regenerate the real M51/T24 fixtures that tests/conftest.py's
real-data-gated tests read.

Resumable: re-run after an interruption and completed stages are skipped.
Not a pytest test file (doesn't match the `test_*.py` collection pattern),
and not a public usage example -- see examples/run_lrgb_example.py for
that. This script is deliberately coupled to conftest.py's own PROJECT_DIR
(and its ASTRO_PIPELINE_PROJECT_DIR_BASE / tests/local_paths.py
configuration) so it always writes exactly where the tests read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from astro_pipeline.lrgb_orchestrator import run_lrgb  # noqa: E402
from conftest import PROJECT_DIR  # noqa: E402

# M51: RA 13h29m52.7s, Dec +47:11:43 -- passed explicitly so the run needs
# no network name resolution.
M51_RA_HOURS = 13.4980
M51_DEC_DEG = 47.1953

if __name__ == "__main__":
    result = run_lrgb(
        PROJECT_DIR,
        telescope="T24",
        target="M51",
        ra_hours=M51_RA_HOURS,
        dec_deg=M51_DEC_DEG,
    )
    print()
    print("composite:", result.composite_path)
    if result.export_result:
        print("tiff:     ", result.export_result.tiff_path)
        print("preview:  ", result.export_result.preview_path)
