"""Narrowband-boost (HaRGB): blend a narrowband channel into an existing
broadband RGB/LRGB composite to boost emission-nebula contrast, without
losing natural star colours (capability A2, 2026-09).

Distinct from the pure narrowband false-colour composite (SHO/HOO, see
pipeline.py's run_narrowband) -- this produces a still-recognizable
"normal" RGB/LRGB image with nebula emission regions enhanced, not an
entirely narrowband-derived one.

Real technique, sourced not invented (fresh research, 2026-09): the
dominant real recipe among amateur astrophotographers blends a
narrowband channel (typically Ha) into the RED channel only via a
lighten/screen blend, `Red' = max(Red, Ha * k)` -- see Chaotic Nebula's
PixInsight LRGB+HA tutorial (chaoticnebula.com/pixinsight-lrgbha-combination),
Galactic Hunter's HaRGB tutorial (galactic-hunter.com/post/hargb-combination-pixinsight),
and AstroBackyard's HaRGB tutorial (astrobackyard.com/tutorials/hargb-astrophotography-tutorial).
No universal standard blend factor `k` exists -- every real source
treats it as a manual, per-image creative call; commonly cited STARTING
POINTS are roughly 0.3-0.5 (a 25-30% opacity blend, in Photoshop-layer
terms), not validated standards. `DEFAULT_BOOST_FACTOR` here is
explicitly a starting point for this reason, not a claim of correctness
-- final ratio tuning is deliberately left to the manual Photoshop step,
consistent with every other creative decision this project declines to
automate.

Continuum subtraction (scaling and subtracting a broadband filter from
the narrowband channel before blending, to isolate pure emission signal)
is real and used professionally, but confirmed niche even among amateurs
during this session's research -- deliberately NOT implemented here.

Real, verified Siril 1.4.4 CLI capability (measured directly against the
real installed binary, not assumed from the "PixelMath" doc page title):
the scriptable command is `pm`, NOT `pixelmath`. Real syntax, from
`help pm`: `pm "expression" [-rescale [low] [high]] [-nosum]` -- images
are referenced as `$name$` tokens, where `name` is a bare filename (no
extension) already present in the working directory; max 10 images per
expression. Critically, `pm` does NOT take an `-out=`-style output
argument the way `stack`/`rgbcomp` do -- its result becomes Siril's
in-memory "loaded image" and is lost unless a separate, explicit
`save <name>` command follows in the same script. Verified real: a `pm`
call with no following `save` reported success but produced no output
file on disk at all.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from astropy.io import fits

from .siril_driver import run_script

DEFAULT_BOOST_FACTOR = 0.4
DEFAULT_HIGH_PERCENTILE = 99.5

CHANNEL_INDEX = {"red": 0, "green": 1, "blue": 2}


class NarrowbandBoostError(RuntimeError):
    pass


def equalize_mono_for_boost(
    fits_path: str | Path,
    output_path: str | Path,
    high_percentile: float = DEFAULT_HIGH_PERCENTILE,
) -> tuple[float, float]:
    """Background-subtract and rescale a single mono FITS onto a
    comparable [0, ~1] scale, by its OWN median (background) and
    `high_percentile`-th pixel value -- the real, necessary step this
    session's own real M42 data proved is required before a lighten
    blend (`max(channel, narrowband*k)`) means anything.

    REAL FINDING, not theoretical: on real M42 T20 data, the raw Red
    master and a raw Ha master (both straight off Siril `stack`, no
    background extraction or normalization) had near-identical absolute
    percentile ranges (Red's 1st-99.9th percentile ~0.106-0.388, Ha's
    ~0.103-0.322) -- at the researched real-world starting boost factor
    (0.4), `Ha*0.4` was LOWER than Red almost everywhere, so
    `max(Red, Ha*0.4)` boosted ZERO pixels. The PixInsight/AstroBackyard
    tutorials this recipe is sourced from all implicitly assume both
    inputs were already background-subtracted and normalized onto a
    comparable scale first (a real, standard step in those tools'
    workflows) -- Siril's raw `stack` output has no such normalization
    applied. This function is that missing real step, using the exact
    same median/high-percentile approach already used for the SHO/HOO
    case (see pipeline.equalize_narrowband_channels), applied to a
    single mono image instead of a 3-channel composite.

    Returns the (median, high_percentile_value) actually measured.
    """
    fits_path = Path(fits_path)
    data, header = fits.getdata(fits_path, header=True, memmap=False)
    finite = data[np.isfinite(data)]
    median = float(np.median(finite)) if finite.size else 0.0
    high = float(np.percentile(finite, high_percentile)) if finite.size else 1.0
    span = high - median
    if span <= 0:
        span = 1.0

    out = np.clip((data - median) / span, 0.0, None).astype(np.float32)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, out, header=header, overwrite=True)
    return median, high


def rescale_narrowband_to_reference(
    narrowband_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
    high_percentile: float = DEFAULT_HIGH_PERCENTILE,
) -> tuple[Path, float]:
    """Re-express a narrowband layer in the REFERENCE channel's own real
    units, rather than normalizing the reference channel itself -- the
    correct real fix for the same scale-mismatch problem
    `equalize_mono_for_boost` documents, but safe to feed into `rgbcomp`
    afterward. `equalize_mono_for_boost` rescales an image in place, which
    would silently break the target channel's real relationship to the
    OTHER two RGB channels (Red would land on a different absolute scale
    than an un-touched Green/Blue) -- exactly the kind of colour-balance
    corruption `pipeline._build_colour_contributor`'s real RGB alignment
    work elsewhere in this project is careful to avoid. This function
    instead maps the narrowband layer's own (background, bright-signal)
    range onto the reference channel's own real (median, high-percentile)
    range, so the reference channel's absolute scale is never touched --
    only the narrowband layer is remapped, and only as an intermediate
    used inside the lighten-blend, never written back over the reference.

    Real formula: `narrowband' = reference_median + (narrowband -
    narrowband_median) / (narrowband_high - narrowband_median) *
    (reference_high - reference_median)`. Returns `(output_path,
    reference_median)` -- the caller (see `boost_channel_with_narrowband`'s
    `channel_median` parameter) needs `reference_median` to blend
    correctly: a bare `max(reference, narrowband' * boost_factor)` would
    ALSO shrink narrowband's own background offset by `boost_factor`
    (real bug, caught on real M42 data: at boost_factor=0.4, that made
    `narrowband' * 0.4` fall BELOW the reference's real background almost
    everywhere, boosting 0% of pixels even in regions with real, strong
    narrowband-only signal) -- only the signal ABOVE background should be
    scaled by `boost_factor`, not the absolute value including its
    background floor.
    """
    narrowband_path = Path(narrowband_path)
    reference_path = Path(reference_path)

    narrowband_data = fits.getdata(narrowband_path, memmap=False)
    reference_data, reference_header = fits.getdata(reference_path, header=True, memmap=False)

    nb_finite = narrowband_data[np.isfinite(narrowband_data)]
    ref_finite = reference_data[np.isfinite(reference_data)]
    nb_median = float(np.median(nb_finite)) if nb_finite.size else 0.0
    nb_high = float(np.percentile(nb_finite, high_percentile)) if nb_finite.size else 1.0
    ref_median = float(np.median(ref_finite)) if ref_finite.size else 0.0
    ref_high = float(np.percentile(ref_finite, high_percentile)) if ref_finite.size else 1.0

    nb_span = nb_high - nb_median
    if nb_span <= 0:
        nb_span = 1.0

    rescaled = ref_median + (narrowband_data - nb_median) / nb_span * (ref_high - ref_median)
    rescaled = rescaled.astype(np.float32)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(output_path, rescaled, header=reference_header, overwrite=True)
    return output_path, ref_median


def split_rgb_channels(
    rgb_path: str | Path,
    work_dir: str | Path,
    stems: tuple[str, str, str] = ("split_red", "split_green", "split_blue"),
) -> tuple[Path, Path, Path]:
    """Split a (3, ny, nx) composite into 3 separate mono FITS files --
    Siril's `pm`/`rgbcomp` commands operate on named mono files in a
    working directory, not on slices of an already-combined multi-channel
    cube, so boosting one channel requires the channels to exist as
    separate real files first.
    """
    rgb_path = Path(rgb_path)
    data, header = fits.getdata(rgb_path, header=True, memmap=False)
    if data.ndim != 3 or data.shape[0] != 3:
        raise ValueError(f"split_rgb_channels expects a 3-channel (3, ny, nx) composite, got shape {data.shape}")

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, stem in enumerate(stems):
        path = work_dir / f"{stem}.fit"
        fits.writeto(path, data[i].astype(np.float32), header=header, overwrite=True)
        paths.append(path)
    return paths[0], paths[1], paths[2]


def boost_channel_with_narrowband(
    channel_path: str | Path,
    narrowband_path: str | Path,
    work_dir: str | Path,
    output_stem: str,
    boost_factor: float = DEFAULT_BOOST_FACTOR,
    channel_median: float | None = None,
) -> Path:
    """Real lighten blend via Siril's real `pm` command (see module
    docstring for the sourced technique and the real `pm`+`save`
    two-step call shape).

    `channel_path` and `narrowband_path` must already share the same
    pixel grid (same shape + WCS) -- reproject the narrowband master
    onto the channel's grid first if they don't (see
    reconciliation.reproject_to_reference).

    `channel_median`, when given, uses the background-PRESERVING
    formula `max(channel, channel_median + (narrowband - channel_median)
    * boost_factor)` instead of the naive `max(channel, narrowband *
    boost_factor)`. Real, necessary distinction (caught on real M42
    data): the naive formula scales narrowband's own background floor
    down by `boost_factor` too, which can push it BELOW the channel's
    real background almost everywhere, boosting 0% of pixels even where
    real narrowband-only signal exists. Pass `channel_median` (e.g. from
    `rescale_narrowband_to_reference`'s own return value) whenever
    `narrowband_path` was produced by that function; omit it only for a
    narrowband layer that is already correctly background-referenced
    (e.g. a synthetic image already centred at the channel's own
    background level).

    Staged under fixed, short names (`_boost_channel`/
    `_boost_narrowband`) rather than referencing the real input
    filenames directly in the `pm` expression string -- real project
    folder names in this codebase are already known to contain spaces
    and other characters Siril's own script-argument splitting has real,
    documented problems with elsewhere (see calibration.py's own staging
    convention for the same reason).
    """
    channel_path = Path(channel_path)
    narrowband_path = Path(narrowband_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    staged_channel = work_dir / "_boost_channel.fit"
    staged_narrowband = work_dir / "_boost_narrowband.fit"
    shutil.copy2(channel_path, staged_channel)
    shutil.copy2(narrowband_path, staged_narrowband)

    if channel_median is not None:
        expression = (
            f"max($_boost_channel$, {channel_median}*(1-{boost_factor}) "
            f"+ $_boost_narrowband$*{boost_factor})"
        )
    else:
        expression = f"max($_boost_channel$, $_boost_narrowband$*{boost_factor})"
    result = run_script([f'pm "{expression}" -nosum', f"save {output_stem}"], workdir=work_dir)

    output_path = work_dir / f"{output_stem}.fit"
    if not output_path.exists():
        raise NarrowbandBoostError(
            f"Siril pm did not produce {output_path.name}. Log tail:\n"
            + "\n".join(result.log_lines[-15:])
        )
    return output_path
