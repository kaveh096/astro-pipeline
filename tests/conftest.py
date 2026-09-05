"""Shared locations for real-data test fixtures.

Fixtures live next to the raw data, under the project folder's generated
`_pipeline/` directory -- Kaveh's own working convention (everything for a
target stays together) and, importantly, a durable one.

An earlier version of these tests hardcoded paths into the OS temp
directory. Those got cleaned up between sessions, which silently turned
every real-data test into a SKIP while the suite still reported green --
the same false-confidence failure mode as the GraXpert NaN bug the suite
was supposed to be guarding against. Fixtures under the project folder
survive, so a green run actually means real data was exercised.

Regenerate all of these with:  python scripts/run_m51.py
(resumable -- it skips whatever is already present).
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025")
PIPELINE_DIR = PROJECT_DIR / "_pipeline"
FINAL_DIR = PIPELINE_DIR / "final"


def _master(filter_name: str, binning: int) -> Path:
    group = f"T24-kaveh096-M51-{filter_name}-bin{binning}"
    return PIPELINE_DIR / group / "lights" / f"master_{filter_name.lower()}.fit"


# Plate-solved per-filter masters (Stages 2-4).
LUM_MASTER = _master("Luminance", 1)
RED_MASTER = _master("Red", 2)
GREEN_MASTER = _master("Green", 2)
BLUE_MASTER = _master("Blue", 2)

# Colour/luminance products (Stages 6-9). All produced by the
# pedestal-corrected pipeline -- background-EXTRACTED, not raw masters,
# which matters for stretch tests (a raw master's background sits almost
# entirely at the calibration pedestal, leaving GHT nothing to work with).
RGB_NATIVE = FINAL_DIR / "rgb_native.fit"
RGB_NATIVE_BG = FINAL_DIR / "rgb_native_bg.fits"
RGB_PCC = FINAL_DIR / "rgb_pcc.fit"
LUM_BG = FINAL_DIR / "lum_bg.fits"
RGB_RECONCILED = FINAL_DIR / "rgb_reconciled.fit"
LRGB_FINAL = FINAL_DIR / "lrgb_final.fit"


def requires(*paths: Path):
    """Skip marker naming exactly which fixture is missing, so a skip is
    actionable rather than a shrug."""
    missing = [p for p in paths if not p.exists()]
    return pytest.mark.skipif(
        bool(missing),
        reason=(
            "missing real fixture(s): "
            + ", ".join(str(p.relative_to(PROJECT_DIR)) for p in missing)
            + " -- regenerate with: python scripts/run_m51.py"
        ),
    )


requires_project = pytest.mark.skipif(
    not PROJECT_DIR.exists(), reason=f"raw project folder not present: {PROJECT_DIR}"
)
