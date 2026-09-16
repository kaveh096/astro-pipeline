"""Shared locations for real-data test fixtures.

Fixtures live next to the raw data, under the project folder's generated
`_pipeline/` directory -- this project's own working convention
(everything for a target stays together) and, importantly, a durable
one.

An earlier version of these tests hardcoded paths into the OS temp
directory. Those got cleaned up between sessions, which silently turned
every real-data test into a SKIP while the suite still reported green --
the same false-confidence failure mode as the GraXpert NaN bug the suite
was supposed to be guarding against. Fixtures under the project folder
survive, so a green run actually means real data was exercised.

Real personal paths (where YOUR OWN raw data/project folders live) are
NOT hardcoded here -- this repo is public. Configure them via
`tests/local_paths.py` (gitignored -- copy `local_paths.py.example` to
get started) or the `ASTRO_PIPELINE_DESKTOP_DIR`/
`ASTRO_PIPELINE_ITELESCOPE_DIR` environment variables. Every real-data
test is gated by a `pytest.mark.skipif` that checks whether its fixture
actually exists, so running this suite with neither configured just
skips them -- the large majority of the suite needs nothing here.

Regenerate the M51 fixtures with:  python scripts/run_m51.py
(resumable -- it skips whatever is already present; edit its
PROJECT_DIR constant to point at your own real M51 folder first).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    import local_paths  # tests/local_paths.py, gitignored, optional
except ImportError:
    local_paths = None


def _configured_dir(env_var: str, local_attr: str) -> Path | None:
    if local_paths is not None and getattr(local_paths, local_attr, None):
        return Path(getattr(local_paths, local_attr))
    env_val = os.environ.get(env_var)
    return Path(env_val) if env_val else None


def _under(base: Path | None, subpath: str) -> Path:
    # A guaranteed-nonexistent path when unconfigured, so every
    # `pytest.mark.skipif(not path.exists(), ...)` below just works --
    # never raises, never accidentally matches something real.
    return (base / subpath) if base is not None else Path("/__unconfigured_test_data__") / subpath


_DESKTOP_DIR = _configured_dir("ASTRO_PIPELINE_DESKTOP_DIR", "DESKTOP_DIR")
_ITELESCOPE_DIR = _configured_dir("ASTRO_PIPELINE_ITELESCOPE_DIR", "ITELESCOPE_DIR")
LUM_USER = getattr(local_paths, "LUM_USER", None) or os.environ.get("ASTRO_PIPELINE_LUM_USER", "observer1")
RGB_USER = getattr(local_paths, "RGB_USER", None) or os.environ.get("ASTRO_PIPELINE_RGB_USER", "observer1")
# The two real per-user components of LUM_USER (a real multi-collaborator
# group name like "collaborator1+observer1") -- exposed separately so
# real-scan tests can assert against whatever's actually configured,
# rather than hardcoding a literal username string that would silently
# stop matching real disk data once a different local_paths.py is used.
LUM_USERS = frozenset(LUM_USER.split("+"))

PROJECT_DIR = _under(_DESKTOP_DIR, "M51 - Whirlpool galaxy - T24 & T21 - Jan 2025")
PIPELINE_DIR = PROJECT_DIR / "_pipeline"
FINAL_DIR = PIPELINE_DIR / "final"


def _master(filter_name: str, binning: int, user: str = RGB_USER) -> Path:
    group = f"T24-{user}-M51-{filter_name}-bin{binning}"
    return PIPELINE_DIR / group / "lights" / f"master_{filter_name.lower()}.fit"


# Plate-solved per-filter masters (Stages 2-4). Luminance is the merged
# multi-user group (two real collaborators both shoot Luminance/BIN1 on
# T24 -- pipeline.py combines them at the raw-sub level, see its module
# docstring), so its group directory name reflects both real iTelescope
# usernames, sorted (see local_paths.py.example for how to configure
# these for your own real data). Red/Green/Blue at BIN2 are single-user,
# so their group names are unchanged.
LUM_MASTER = _master("Luminance", 1, user=LUM_USER)
RED_MASTER = _master("Red", 2)
GREEN_MASTER = _master("Green", 2)
BLUE_MASTER = _master("Blue", 2)

# Colour/luminance products (Stages 6-9). All produced by the
# pedestal-corrected pipeline -- background-EXTRACTED, not raw masters,
# which matters for stretch tests (a raw master's background sits almost
# entirely at the calibration pedestal, leaving GHT nothing to work with).
RGB_NATIVE = FINAL_DIR / "rgb_native.fit"
RGB_NATIVE_BG = FINAL_DIR / "rgb_native_bg.fits"
RGB_COLOUR_CALIBRATED = FINAL_DIR / "rgb_colour_calibrated.fit"
LUM_BG = FINAL_DIR / "lum_bg.fits"
RGB_RECONCILED = FINAL_DIR / "rgb_reconciled.fit"
LRGB_FINAL = FINAL_DIR / "lrgb_final.fit"

# Reconciled-but-not-yet-cropped RGB contributors (post reproject_to_reference,
# pre crop_to_common_coverage) -- the real input crop_to_common_coverage()
# actually sees in production. Two same-telescope binnings today (T24
# BIN1/BIN2); used as the real-data fixture for its memory-streaming
# regression test.
RGB_RECONCILED_CONTRIB_BIN1 = FINAL_DIR / "rgb_reconciled_contrib_T24_bin1.fit"
RGB_RECONCILED_CONTRIB_BIN2 = FINAL_DIR / "rgb_reconciled_contrib_T24_bin2.fit"


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

# NGC 3628/T73 (Feb 2025) real delivery -- iTelescope server-side-calibrated
# only (no local Dark frames at all, CALSTAT="BF" on real headers), the real
# fixture for CalibrationMode.PRECALIBRATED (see plan-precalibrated-path.md).
# Raw data lives directly under your own configured ITELESCOPE_DIR, not this
# repo's usual Desktop project-folder convention -- this target has not yet
# been run through the pipeline, so there is no `_pipeline/` output here yet.
NGC3628_PROJECT_DIR = _under(_ITELESCOPE_DIR, "NGC 3628 - Hamburger Galaxy - Chile - LRGB - Feb 2025")
requires_ngc3628_project = pytest.mark.skipif(
    not NGC3628_PROJECT_DIR.exists(), reason=f"raw project folder not present: {NGC3628_PROJECT_DIR}"
)

# Abell 6 and HFG1/T02 (Dec 2022) real delivery -- one-shot-colour (OSC),
# genuine undemosaiced Bayer CFA data (RGGB), no Luminance filter at all --
# the real fixture for RGB-only mode + debayer support (see
# plan-rgb-only-mode.md). _pipeline/ output lives directly under this folder.
ABELL6_PROJECT_DIR = _under(_ITELESCOPE_DIR, "Abell 6 and HFG1 - RGB - Dec 2022")
requires_abell6_project = pytest.mark.skipif(
    not ABELL6_PROJECT_DIR.exists(), reason=f"raw project folder not present: {ABELL6_PROJECT_DIR}"
)

# IC 1396/T68 (Sep 2021) real delivery -- one-shot-colour (OSC), genuine
# undemosaiced Bayer CFA data, with REAL LOCAL BIAS (48 subs, in bias2/)
# but NO real local dark frames at all -- the real fixture for capability
# B's bias-only + debayer RAW_LOCAL calibration path (2026-09 publish
# roadmap). Real BAYERPAT header holds a non-standard placeholder
# ("VALID"), confirmed via direct header inspection, not a real pattern
# code -- bayer_pattern must be passed explicitly (RGGB=0), never
# auto-detected from this delivery's header.
IC1396_PROJECT_DIR = _under(_ITELESCOPE_DIR, "IC 1396 - Elephant Trunk - RGB - T68 - Sep 2021")
requires_ic1396_project = pytest.mark.skipif(
    not IC1396_PROJECT_DIR.exists(), reason=f"raw project folder not present: {IC1396_PROJECT_DIR}"
)
