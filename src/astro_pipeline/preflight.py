"""Pre-flight checks: verify external tools, Siril's version, and (for the
run's primary telescope) an SPCC colour-calibration profile, all BEFORE a
run writes so much as a byte into `_pipeline/`.

Side-effect free: uses `classify_tree()` (zip-peeking, never extracts),
not `scan_session()`. `check_prerequisites()` collects every problem it
finds rather than raising on the first one, so a caller can print them
all at once instead of the user fixing and retrying one at a time.

The Gaia/SPCC catalogue install is deliberately NOT checked here --
its location is a Siril setting this code has no way to read reliably,
so a missing-looking default path doesn't mean it's actually missing.
See interview.py's Prerequisites section for an informational (never
blocking) note instead.
"""

from __future__ import annotations

from pathlib import Path

from astro_pipeline.background_extraction import find_graxpert
from astro_pipeline.color_calibration import (
    UnknownInstrumentError,
    resolve_instrument_profile,
    resolve_osc_instrument_profile,
)
from astro_pipeline.filter_constants import OSC_FILTER, RGB_FILTERS
from astro_pipeline.ingest import classify_tree
from astro_pipeline.siril_driver import find_siril_cli, get_version
from astro_pipeline.solving import find_astap_cli
from astro_pipeline.star_removal import find_starnet

MIN_SIRIL_VERSION = (1, 4, 4)


def _check_siril(problems: list[str]) -> None:
    try:
        siril_cli = find_siril_cli()
    except FileNotFoundError as exc:
        problems.append(str(exc))
        return
    try:
        version = get_version(siril_cli, timeout=30)
    except Exception as exc:  # broad on purpose: a hung/misbehaving CLI
        # shouldn't crash preflight itself with an unrelated traceback --
        # this class of failure will surface again, more concretely, the
        # moment the real run actually tries to call Siril.
        problems.append(f"Could not determine Siril's version ({exc}).")
        return
    if version < MIN_SIRIL_VERSION:
        problems.append(
            f"Siril {'.'.join(map(str, version))} is older than the required "
            f"{'.'.join(map(str, MIN_SIRIL_VERSION))}+ (SPCC crashes the process "
            "outright on 1.4.3). Upgrade Siril."
        )


def _check_findable(finder, problems: list[str]) -> None:
    try:
        finder()
    except FileNotFoundError as exc:
        problems.append(str(exc))


def _check_explicit_or_findable(explicit: Path | None, finder, problems: list[str]) -> None:
    if explicit is not None:
        if not Path(explicit).exists():
            problems.append(f"{explicit} does not exist.")
        return
    _check_findable(finder, problems)


def _check_spcc_profile(
    project_dir: str | Path,
    telescope: str,
    target: str,
    calibration_header_fallback: bool,
    problems: list[str],
) -> None:
    """SPCC only ever runs for the run's primary `--telescope` -- colour
    discovery is hard-scoped to it (see lrgb_orchestrator.py's own
    comment to that effect). Checking every telescope the scan happens to
    see would be wrong: M51's own T21 (Luminance-only) has no SPCC
    profile at all, and that's fine -- it's never the primary telescope
    for a colour run.
    """
    report = classify_tree(project_dir, calibration_header_fallback=calibration_header_fallback)
    groups = {**report.instrument_groups(), **report.calibrated_instrument_groups()}
    filters_seen = {filt for (t, tgt, filt, _b) in groups if t == telescope and tgt == target}
    if not filters_seen:
        return  # nothing recognized yet for this telescope/target -- a
        # different check (or the run itself) will report that; guessing
        # a profile requirement over zero real data would be noise.
    if filters_seen & set(RGB_FILTERS):
        try:
            resolve_instrument_profile(telescope)
        except UnknownInstrumentError as exc:
            problems.append(f"{exc} (needed for SPCC on {telescope}'s mono R/G/B data)")
    if OSC_FILTER in filters_seen:
        try:
            resolve_osc_instrument_profile(telescope)
        except UnknownInstrumentError as exc:
            problems.append(f"{exc} (needed for SPCC on {telescope}'s OSC/Color data)")


def check_prerequisites(
    *,
    project_dir: str | Path | None = None,
    telescope: str | None = None,
    target: str | None = None,
    needs_siril: bool = False,
    needs_astap: bool = False,
    needs_graxpert: bool = False,
    needs_starnet: bool = False,
    needs_spcc: bool = False,
    graxpert_exe: Path | None = None,
    starnet_exe: Path | None = None,
    calibration_header_fallback: bool = False,
) -> list[str]:
    """Every problem that would stop this run before it starts, or an
    empty list if none. Never raises for an expected failure (a missing
    tool, an old Siril, a missing SPCC profile) -- those all become
    entries in the returned list instead.
    """
    problems: list[str] = []

    if needs_siril:
        _check_siril(problems)
    if needs_astap:
        _check_findable(find_astap_cli, problems)
    if needs_graxpert:
        _check_explicit_or_findable(graxpert_exe, find_graxpert, problems)
    if needs_starnet:
        _check_explicit_or_findable(starnet_exe, find_starnet, problems)

    if needs_spcc and project_dir is not None and telescope is not None and target is not None:
        _check_spcc_profile(project_dir, telescope, target, calibration_header_fallback, problems)

    return problems
