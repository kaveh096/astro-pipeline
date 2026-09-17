"""Narrowband filter-name/channel bookkeeping, extracted from pipeline.py
(Task 5 refactor, 2026-09-16). Exists purely to support run_narrowband's
own needs (filter-name spelling variance and the per-channel SPCC
substitute) -- unrelated to narrowband_boost.py's separate HaRGB-blend-
into-an-existing-RGB-composite feature; don't merge the two despite the
shared "narrowband" word.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits

# Narrowband palette mappings (capability A, 2026-09): each maps to a
# (red, green, blue) filter-name triplet, reusing the exact same
# `_build_colour_contributor` machinery as broadband RGB (register+stack
# per filter, reproject onto the first filter's grid, crop to common
# coverage, rgbcomp) -- confirmed real via fresh research that Siril's
# `rgbcomp` is genuinely filter-name-agnostic (see docs/
# colour-calibration-catalogues.md's SPCC section for the related,
# narrowband-specific SPCC caveat). "sho" (the stylized Hubble palette,
# NOT physically-real relative line intensities) maps SII/Ha/OIII to
# R/G/B. "hoo" (bicolor) reuses OIII for both G and B -- Siril's own real
# HOO tutorial passes `-nosum` specifically for this reuse case, to avoid
# double-counting exposure/stack-count metadata; the OIII master file is
# duplicated to a second path rather than referencing the same file
# twice, as the safe, verified-mechanical choice (untested whether Siril
# accepts the identical path twice for `rgbcomp` -- not worth the risk
# for the one extra file copy this avoids).
NARROWBAND_PALETTES: dict[str, tuple[str, str, str]] = {
    "sho": ("SII", "Ha", "OIII"),
    "hoo": ("Ha", "OIII", "OIII"),
}

# Real FILTER header spelling varies across the wider amateur/network
# community (confirmed real for THIS project's own telescopes: T20's
# real FITS headers use exactly "Ha"/"OIII"/"SII", verified 2026-09 --
# but other deliveries, especially non-iTelescope ones, commonly spell
# these "H-Alpha"/"Halpha", "O3"/"O-III", "S2"/"S-II"). This table
# normalizes known real-world variants to this codebase's own internal
# canonical spelling (matching NARROWBAND_PALETTES above) -- resolve_
# lights() callers for narrowband should normalize through this before
# matching, so a differently-spelled real delivery gets a real match
# instead of a silent "no lights found".
NARROWBAND_FILTER_ALIASES: dict[str, str] = {
    "ha": "Ha", "h-alpha": "Ha", "halpha": "Ha", "hydrogen-alpha": "Ha",
    "oiii": "OIII", "o3": "OIII", "o-iii": "OIII", "oxygen-iii": "OIII",
    "sii": "SII", "s2": "SII", "s-ii": "SII", "sulfur-ii": "SII",
}


def normalize_narrowband_filter_name(filter_name: str) -> str:
    """Map a real FITS FILTER header string to this codebase's canonical
    Ha/OIII/SII spelling, case- and punctuation-insensitively, via
    NARROWBAND_FILTER_ALIASES. Returns the input unchanged if it doesn't
    match any known narrowband alias (e.g. "Luminance", "Red") -- this
    function is deliberately NOT a general-purpose filter normalizer,
    only a narrowband one."""
    key = filter_name.strip().lower()
    return NARROWBAND_FILTER_ALIASES.get(key, filter_name)


class _NarrowbandNormalizingReport:
    """Thin proxy around a real IngestReport that normalizes narrowband
    FILTER-name spelling variants (see NARROWBAND_FILTER_ALIASES) to this
    codebase's canonical Ha/OIII/SII spelling, so `resolve_lights()`'s
    exact-string dict lookups (inside `_build_colour_contributor`) find
    real data delivered under a different real-world spelling convention.

    Real motivation (round-1b adversarial review finding on the original
    narrowband plan): this project's own T20 data happens to already use
    the canonical spelling, verified directly against real headers -- but
    a stranger's own OSC/narrowband gear very plausibly does not (NINA/
    SGP/ASIAIR deliveries commonly write "H-Alpha", "O-III", "S-II", or
    similar). Without this, a real, valid narrowband delivery under a
    different spelling would silently resolve to "no lights found" rather
    than a genuine match.

    Only touches `instrument_groups()`/`calibrated_instrument_groups()`/
    `flat_index()` -- the three methods `_build_colour_contributor` (via
    `resolve_lights`) actually reads a filter name out of. Every other
    real `IngestReport` method (`calibration_index`, `light_groups`,
    etc.) is delegated through unchanged via `__getattr__`.

    If two real filter names collide onto the same canonical name for the
    same (telescope, target, binning) -- e.g. a delivery genuinely mixing
    "Ha" and "H-Alpha" spellings for what is really the same filter --
    their light lists are merged rather than one silently overwriting the
    other. Rare in practice; documented rather than silently ignored.
    """

    def __init__(self, report) -> None:
        self._report = report

    def _normalize_groups(self, groups: dict) -> dict:
        out: dict = {}
        for (telescope, target, filter_name, binning), frames in groups.items():
            key = (telescope, target, normalize_narrowband_filter_name(filter_name), binning)
            out[key] = out.get(key, []) + list(frames)
        return out

    def instrument_groups(self):
        return self._normalize_groups(self._report.instrument_groups())

    def calibrated_instrument_groups(self):
        return self._normalize_groups(self._report.calibrated_instrument_groups())

    def flat_index(self):
        out: dict = {}
        for (telescope, binning, filter_name), frames in self._report.flat_index().items():
            key = (telescope, binning, normalize_narrowband_filter_name(filter_name))
            out[key] = out.get(key, []) + list(frames)
        return out

    def __getattr__(self, name):
        return getattr(self._report, name)


def equalize_narrowband_channels(
    fits_path: str | Path,
    output_path: str | Path,
    high_percentile: float = 99.5,
) -> dict[str, float]:
    """Per-channel background-and-scale normalization for a narrowband
    composite (capability A, 2026-09) -- the real substitute for SPCC,
    which is deliberately skipped on narrowband (see
    `_build_colour_contributor`'s `run_colour_calibration` docstring:
    SPCC would enforce physically real relative line intensities, which
    looks wrong for the stylized Hubble-palette convention users
    actually want).

    Round-2 adversarial review of the original plan correctly found that
    "GraXpert's background extraction already gives per-channel
    background levels for free" was FALSE -- GraXpert returns only a
    whole-image background-subtracted result, no reusable per-channel
    scalar. This function is the real, new code that claim was missing:
    for each channel independently, subtract its own robust background
    level (median, over finite pixels only) and rescale so its own
    `high_percentile`-th pixel value hits 1.0.

    This is deliberately simple and NOT a physically-modeled colour
    calibration -- it's the numeric equivalent of a manual per-channel
    "levels" adjustment done before combining channels, which is common,
    real practice in narrowband processing (per this session's own
    research into real HaRGB/SHO workflows) specifically BECAUSE no
    physically-correct relative intensity is wanted here. Narrowband
    colour remains a stylized palette after this step, not a scientific
    one -- exactly as intended.

    Returns the real per-channel (median, high-percentile) values
    actually measured, for logging -- inspectable, not a black box,
    consistent with this project's checkpointed-not-black-box principle.
    """
    fits_path = Path(fits_path)
    data, header = fits.getdata(fits_path, header=True, memmap=False)
    if data.ndim != 3 or data.shape[0] != 3:
        raise ValueError(
            f"equalize_narrowband_channels expects a 3-channel (3, ny, nx) composite, "
            f"got shape {data.shape}"
        )

    out = np.empty_like(data, dtype=np.float32)
    stats: dict[str, float] = {}
    for i in range(3):
        channel = data[i]
        finite = channel[np.isfinite(channel)]
        median = float(np.median(finite)) if finite.size else 0.0
        high = float(np.percentile(finite, high_percentile)) if finite.size else 1.0
        span = high - median
        if span <= 0:
            # Degenerate channel (e.g. all-zero/all-equal) -- avoid a
            # divide-by-zero; leaves this channel near-zero rather than
            # inventing signal that isn't there.
            span = 1.0
        out[i] = np.clip((channel - median) / span, 0.0, None).astype(np.float32)
        stats[f"ch{i}_median"] = median
        stats[f"ch{i}_high_p{high_percentile:g}"] = high

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, out, header=header, overwrite=True)
    return stats
