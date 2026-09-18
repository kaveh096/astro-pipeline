"""Discover and pick which Luminance/OSC contributor drives a composite.

Deliberately plain functions, not a class: each one is already
independently unit-tested via direct import, none holds state across calls,
and `select_luminance_source()` takes pre-measured FWHM values as plain
tuples (the actual `contributor_fwhm_arcsec()` measurement happens inside
`run_lrgb` itself). Wrapping these in a class would only relocate the same
code under `self.` with no reduction in parameter count or duplication --
contrast with `colour_contributor.ColourContributorBuilder`, where a class
earns its place by cutting a shared parameter prefix.
"""

from __future__ import annotations

from pathlib import Path

from .filter_constants import LUMINANCE_FILTER, OSC_FILTER
from .logging_utils import log as _log

LumCandidate = tuple[tuple[str, int], Path, float | None]


def select_luminance_source(
    candidates: list[LumCandidate],
    lum_source: tuple[str, int] | None,
    fallback_key: tuple[str, int],
    notes: list[str],
) -> LumCandidate:
    """Pick which already-built Luminance contributor drives the composite,
    and log why every other one didn't -- Slice 2's measured recommendation,
    factored out of run_lrgb as its own function (side effect: log lines
    only) so the selection rule itself is directly testable without
    invoking the full Siril/GraXpert/SPCC pipeline that builds `candidates`
    in the first place.

    Rule, in order:
    1. `lum_source`, if given, always wins -- an explicit human override,
       logged as such (not a measured choice). Raises ValueError if it
       names a combo that isn't actually among `candidates` (Slice 1's
       discovery would have to have missed it, or the caller made a typo --
       either way, silently ignoring the override would be worse).
    2. Otherwise, the candidate with the lowest measured FWHM wins ("lowest
       FWHM" = sharpest), logged with the number that decided it.
    3. If NOTHING could be measured (every contributor's `.seq` file was
       missing or unreadable), fall back to `fallback_key` -- the caller's
       own (telescope, lum_binning), i.e. the pre-Slice-2 behaviour --
       rather than raising, since a missing sharpness figure is a real but
       survivable degraded mode (see contributor_fwhm_arcsec).
    """
    if lum_source is not None:
        match = next((c for c in candidates if c[0] == lum_source), None)
        if match is None:
            raise ValueError(
                f"lum_source={lum_source!r} was not among the discovered Luminance "
                f"contributors {[c[0] for c in candidates]}"
            )
        selected = match
        _log(
            f"[run ] Luminance source: {selected[0][0]}-bin{selected[0][1]} selected via "
            "EXPLICIT OVERRIDE (lum_source=...) -- not a measured choice",
            notes,
        )
    else:
        measured = [c for c in candidates if c[2] is not None]
        if measured:
            selected = min(measured, key=lambda c: c[2])
            _log(
                f"[run ] Luminance source: {selected[0][0]}-bin{selected[0][1]} selected as "
                f"sharpest (FWHM {selected[2]:.2f}\")",
                notes,
            )
        else:
            fallback = next((c for c in candidates if c[0] == fallback_key), None)
            selected = fallback or candidates[0]
            _log(
                f"[warn] no Luminance contributor FWHM could be measured; falling back to "
                f"{selected[0][0]}-bin{selected[0][1]}",
                notes,
            )

    for key, _path, fwhm in candidates:
        if key == selected[0]:
            continue
        if fwhm is not None and selected[2] is not None:
            reason = f"FWHM {fwhm:.2f}\" vs selected {selected[2]:.2f}\""
        else:
            reason = "not selected"
        _log(
            f"       Luminance-{key[0]}-bin{key[1]}: master built but NOT selected for the "
            f"composite ({reason})",
            notes,
        )

    return selected


def discover_luminance_contributors(
    report, target: str, telescope: str, lum_binning: int
) -> list[tuple[str, int]]:
    """Every (telescope, binning) with Luminance data for `target`, across
    ALL telescopes -- not scoped to `telescope` -- so a telescope that only
    ever contributes colour under the user's colour-only rule (T21 today)
    still gets its own Luminance master built (Slice 2 will select among
    these; this only discovers them). Mirrors exactly how RGB binning
    discovery already handles "caller-supplied but not necessarily
    discovered first" (see run_lrgb's `rgb_binnings` below): sort every
    discovered (telescope, binning), then prepend the caller's own
    (telescope, lum_binning) only if it wasn't already found -- so the
    single-contributor case (T24-only data) returns exactly
    `[(telescope, lum_binning)]`, unchanged from today.
    """
    contributors = sorted(
        {
            (key[0], key[3])
            for key in report.instrument_groups()
            if key[1] == target and key[2] == LUMINANCE_FILTER
        }
    )
    if (telescope, lum_binning) not in contributors:
        contributors = [(telescope, lum_binning), *contributors]
    return contributors


def discover_osc_contributors(
    report, target: str, telescope: str, osc_binning: int
) -> list[tuple[str, int]]:
    """Every (telescope, binning) with one-shot-colour (Color/OSC) data
    for `target` (RGB-only/OSC plan, 2026-09) -- mirrors
    discover_luminance_contributors() exactly: same all-telescopes-for-
    this-target scope, same caller-binning-prepended-if-missing shape."""
    contributors = sorted(
        {
            (key[0], key[3])
            for key in report.instrument_groups()
            if key[1] == target and key[2] == OSC_FILTER
        }
    )
    if (telescope, osc_binning) not in contributors:
        contributors = [(telescope, osc_binning), *contributors]
    return contributors
