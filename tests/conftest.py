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
get started) or the `ASTRO_PIPELINE_PROJECT_DIR_BASE`/
`ASTRO_PIPELINE_ITELESCOPE_DIR` environment variables. Every real-data
test is gated by a `pytest.mark.skipif` that checks whether its fixture
actually exists, so running this suite with neither configured just
skips them -- the large majority of the suite needs nothing here.

Regenerate the M51 fixtures with:  python tests/regenerate_m51_fixtures.py
(resumable -- it skips whatever is already present; reads PROJECT_DIR from
this same file, so configure your real M51 folder the same way as any
other real-data test, via local_paths.py/the env vars above).
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


_PROJECT_DIR_BASE = _configured_dir("ASTRO_PIPELINE_PROJECT_DIR_BASE", "PROJECT_DIR_BASE")
_ITELESCOPE_DIR = _configured_dir("ASTRO_PIPELINE_ITELESCOPE_DIR", "ITELESCOPE_DIR")
LUM_USER = getattr(local_paths, "LUM_USER", None) or os.environ.get("ASTRO_PIPELINE_LUM_USER", "observer1")
RGB_USER = getattr(local_paths, "RGB_USER", None) or os.environ.get("ASTRO_PIPELINE_RGB_USER", "observer1")
# The two real per-user components of LUM_USER (a real multi-collaborator
# group name like "collaborator1+observer1") -- exposed separately so
# real-scan tests can assert against whatever's actually configured,
# rather than hardcoding a literal username string that would silently
# stop matching real disk data once a different local_paths.py is used.
LUM_USERS = frozenset(LUM_USER.split("+"))

_M51_PROJECT_NAME = (
    getattr(local_paths, "M51_PROJECT_NAME", None)
    or os.environ.get("ASTRO_PIPELINE_M51_PROJECT_NAME")
    or "M51 - Whirlpool galaxy - T24 & T21 - Jan 2025"
)
PROJECT_DIR = _under(_PROJECT_DIR_BASE, _M51_PROJECT_NAME)
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
            + " -- regenerate with: python tests/regenerate_m51_fixtures.py"
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
# roadmap), exercised there via MANUALLY-constructed CalibrationFrame
# objects (a direct glob of bias2/), not via scan_session. **Corrected,
# plan-flats-v4.md**: via the real ingest pipeline (scan_session), T68 is
# NOT RAW_LOCAL today -- its real bias/dark carry no "T68" folder token
# anywhere in their path, so filename-based recognition sees zero local
# Bias/Dark for T68 at all, and infer_calibration_mode resolves
# PRECALIBRATED (real calibrated-provenance Color lights exist). RAW_LOCAL
# OSC is real, tested code (this fixture proves the mechanics work), but
# unreachable end-to-end for T68 today without an explicit
# --calibration-mode override AND fixing build_osc's own cal_index={}
# KeyError bug (deferred, plan-flats-v4.md §5). Real BAYERPAT header
# holds a non-standard placeholder ("VALID"), confirmed via direct header
# inspection, not a real pattern code -- bayer_pattern must be passed
# explicitly (RGGB=0), never auto-detected from this delivery's header.
IC1396_PROJECT_DIR = _under(_ITELESCOPE_DIR, "IC 1396 - Elephant Trunk - RGB - T68 - Sep 2021")
requires_ic1396_project = pytest.mark.skipif(
    not IC1396_PROJECT_DIR.exists(), reason=f"raw project folder not present: {IC1396_PROJECT_DIR}"
)

# Abell 31/T59 (Mar 2023) real delivery -- PRECALIBRATED, CALSTAT=BDF, no
# unrecognised calibration frames of any kind (plan-flats-v4.md §2.1) --
# one of that plan's seven gate-(D) data-invariant projects (§4.0),
# included for completeness even though it has nothing of its own to
# recognise.
ABELL31_PROJECT_DIR = _under(_ITELESCOPE_DIR, "Abell 31 - LRGB - Mar 2023")
requires_abell31_project = pytest.mark.skipif(
    not ABELL31_PROJECT_DIR.exists(), reason=f"raw project folder not present: {ABELL31_PROJECT_DIR}"
)

# M42/T20 (Jan 2022) real delivery -- mono LRGB + narrowband (Ha/OIII/SII),
# with a real raw flat library (70 files, dated 2020-03-19, ~21 months
# older than the lights) and real bias/dark under a "Bias-Darks-T20-..."
# naming convention -- none of it recognised today because the flats/
# darks carry no "T20" folder token at all (plan-flats-v4.md §2.1) -- the
# real fixture for Step 4b's opt-in header-based calibration recognition.
M42_PROJECT_DIR = _under(_ITELESCOPE_DIR, "M42 - Orion Nebula - LRGBHOS - Jan 2022")
requires_m42_project = pytest.mark.skipif(
    not M42_PROJECT_DIR.exists(), reason=f"raw project folder not present: {M42_PROJECT_DIR}"
)

# M31/T05 (Aug 2021) real delivery -- mono RGB, real bias/dark filenames
# literally spelled "T5" (not "T05") and a folder literally named "T5" --
# the real fixture for Step 4b's T5->T05 telescope-token normalisation
# (plan-flats-v4.md §2.1). Its darks are also the real, unfixed G15
# temperature mismatch (-15C darks vs -10C lights), documented not fixed.
M31_PROJECT_DIR = _under(_ITELESCOPE_DIR, "M31 - Andromeda - T5 - RGB - Aug 2021")
requires_m31_project = pytest.mark.skipif(
    not M31_PROJECT_DIR.exists(), reason=f"raw project folder not present: {M31_PROJECT_DIR}"
)
