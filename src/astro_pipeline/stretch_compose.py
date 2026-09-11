"""Stages 8-9: independent GHT stretch for L and RGB, then LRGB composition.

Command syntax verified against the real installed Siril 1.4.3 CLI
(`help ght`, `help rgbcomp`). Siril's own LRGB workflow guidance expects
pre-stretched inputs on both sides of `rgbcomp -lum=` -- L and the RGB
composite each get their own independent GHT pass here, not one shared
stretch step (correcting an early assumption in the original design
sketch, before any Siril command syntax had been checked).

`rgbcomp -lum=` requires the luminance and RGB images to share the same
pixel dimensions -- this is why Stage 5's reconciliation (reprojecting R/G/B
onto L's grid) must run before this stage, not after.

Stretch method, and why the default is what it is -- established by looking
at the actual rendered image, which is the only thing that caught this:

- `ght` with hand-picked parameters (the original default) is a trap. Its
  symmetry point SP defaults to 0, i.e. the stretch is centred on black,
  while real calibrated data here sits at a background of ~0.1 (partly from
  calibration.py's own pedestal). The result passed every numeric check --
  0% NaN, no clipping, "non-degenerate" spread, 225 stars for PCC -- and
  still rendered as a nearly black frame with the galaxy invisible.
- `autoghs` places SP at k*sigma from each channel's median, so it adapts to
  the actual background. Much better, but it applies no shadow clipping, so
  the background stayed a flat grey (~0.30) with no black point.
- `autostretch` (Siril's own auto-render: MTF with shadow clipping) sets a
  real black point AND a target background, and produced the first output
  that actually looks like an astrophoto. It is the default here.

`-linked` is passed for every method. Siril's own documentation warns that
the unlinked (per-channel) forms alter white balance, which would silently
undo the photometric colour calibration done in Stage 6/7.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .siril_driver import SirilResult, run_load_process_save, run_script

DEFAULT_SHADOWS_CLIP = -2.8
DEFAULT_TARGET_BACKGROUND = 0.25


@dataclass
class ComposeResult:
    composite_path: Path
    lum_stretch_log: SirilResult
    rgb_stretch_log: SirilResult
    compose_log: SirilResult


def _ght_command(
    strength: float,
    black_point: float,
    linear_point: float,
    symmetry_point: float,
    highlight_point: float,
    weighting: str | None,
    channels: str | None,
) -> str:
    cmd = f"ght -D={strength} -B={black_point} -LP={linear_point} -SP={symmetry_point} -HP={highlight_point}"
    if weighting is not None:
        cmd += f" -{weighting}"
    if channels is not None:
        cmd += f" {channels}"
    return cmd


def ght_stretch(
    fits_path: str | Path,
    work_dir: str | Path,
    strength: float,
    black_point: float = 0.0,
    linear_point: float = 0.0,
    symmetry_point: float = 0.0,
    highlight_point: float = 1.0,
    weighting: str | None = "human",
    channels: str | None = None,
    siril_cli: Path | None = None,
) -> SirilResult:
    """Apply Siril's Generalised Hyperbolic Stretch to fits_path in place
    (load/ght/save, ending with fits_path holding the stretched result).
    strength (-D=) is the only mandatory parameter, 0-10. weighting is
    ignored for mono images (Siril's own behavior); pass None to omit it.

    Saving is routed through run_load_process_save rather than a direct
    `save <stem>`, because Siril's `save` refuses to overwrite an existing
    file -- see that function's docstring for the full story (it was found
    here first, via "FITS error: failed to create new file (already
    exists?)" on a .fit input).
    """
    fits_path = Path(fits_path)
    work_dir = Path(work_dir)

    cmd = _ght_command(strength, black_point, linear_point, symmetry_point, highlight_point, weighting, channels)

    return run_load_process_save(
        fits_path, [cmd], work_dir, siril_cli=siril_cli, tmp_suffix="__ght_tmp"
    )


def auto_stretch(
    fits_path: str | Path,
    work_dir: str | Path,
    shadows_clip: float = DEFAULT_SHADOWS_CLIP,
    target_background: float = DEFAULT_TARGET_BACKGROUND,
    siril_cli: Path | None = None,
) -> SirilResult:
    """Siril's `autostretch`: histogram transform with shadow clipping, so
    it sets a real black point as well as lifting the midtones. This is the
    default stretch -- see module docstring for why the hand-tuned `ght`
    path it replaced produced an essentially black image.

    Always `-linked`, to avoid altering white balance post-colour-calibration.
    """
    return run_load_process_save(
        Path(fits_path),
        [f"autostretch -linked {shadows_clip} {target_background}"],
        Path(work_dir),
        siril_cli=siril_cli,
        tmp_suffix="__as_tmp",
    )


def auto_ghs_stretch(
    fits_path: str | Path,
    work_dir: str | Path,
    shadows_clip: float = DEFAULT_SHADOWS_CLIP,
    strength: float = 2.0,
    siril_cli: Path | None = None,
) -> SirilResult:
    """Siril's `autoghs`: generalised hyperbolic stretch with the symmetry
    point derived from each channel's own median, so it adapts to the actual
    background level rather than assuming zero.

    Lifts more faint structure than `autostretch` but applies no shadow
    clipping, so on its own it leaves the background a flat grey. Useful
    followed by auto_stretch() when the goal is maximum faint signal; that
    combination is also noticeably noisier.
    """
    return run_load_process_save(
        Path(fits_path),
        [f"autoghs -linked {shadows_clip} {strength}"],
        Path(work_dir),
        siril_cli=siril_cli,
        tmp_suffix="__ags_tmp",
    )


def rgbcomp_lum(
    lum_path: str | Path,
    rgb_path: str | Path,
    output_stem: str,
    work_dir: str | Path,
    siril_cli: Path | None = None,
) -> tuple[Path, SirilResult]:
    """Combine a stretched luminance image with a stretched RGB composite
    via Siril's rgbcomp -lum=. Both inputs must already share the same
    pixel dimensions (see module docstring) and must already be stretched
    (Siril's LRGB workflow guidance) -- this function does not stretch
    for you, call ght_stretch on each input first.
    """
    lum_path = Path(lum_path)
    rgb_path = Path(rgb_path)
    work_dir = Path(work_dir)

    cmd = f"rgbcomp -lum={lum_path.stem} {rgb_path.stem} -out={output_stem}"
    result = run_script([cmd], workdir=work_dir, siril_cli=siril_cli)

    output_path = work_dir / f"{output_stem}.fit"
    if not output_path.exists():
        raise RuntimeError(f"Siril reported success but {output_path} was not created.")
    return output_path, result


def _apply_stretch(
    path: Path,
    work_dir: Path,
    method: str,
    shadows_clip: float,
    target_background: float,
    ghs_strength: float,
    siril_cli: Path | None = None,
) -> SirilResult:
    """Dispatch one image to the real stretch call for `method`, in place
    (load/process/save -- see auto_stretch/auto_ghs_stretch's own
    docstrings). Factored out of stretch_and_compose() (RGB-only mode,
    2026-09) so stretch_rgb() below can apply the identical method
    dispatch to a single RGB composite with no Luminance to pair it
    against, without duplicating the branching logic."""
    if method == "autostretch":
        return auto_stretch(path, work_dir, shadows_clip, target_background, siril_cli=siril_cli)
    if method == "autoghs":
        return auto_ghs_stretch(path, work_dir, shadows_clip, ghs_strength, siril_cli=siril_cli)
    if method == "autoghs+auto":
        auto_ghs_stretch(path, work_dir, shadows_clip, ghs_strength, siril_cli=siril_cli)
        return auto_stretch(path, work_dir, shadows_clip, target_background, siril_cli=siril_cli)
    raise ValueError(
        f"Unknown stretch method {method!r}; expected 'autostretch', 'autoghs' or 'autoghs+auto'."
    )


def stretch_and_compose(
    lum_path: str | Path,
    rgb_path: str | Path,
    work_dir: str | Path,
    output_stem: str = "lrgb_composite",
    method: str = "autostretch",
    shadows_clip: float = DEFAULT_SHADOWS_CLIP,
    target_background: float = DEFAULT_TARGET_BACKGROUND,
    ghs_strength: float = 2.0,
    siril_cli: Path | None = None,
) -> ComposeResult:
    """Independently stretch lum_path and rgb_path, then combine via
    `rgbcomp -lum=`.

    method:
      "autostretch"  (default) shadow-clipped histogram transform -- the
                     only one of the three that produced a viewable image
                     on real data straight off.
      "autoghs"      GHS with a data-derived symmetry point; lifts more
                     faint signal, leaves the background grey (no black
                     point) and is noisier.
      "autoghs+auto" autoghs followed by autostretch: most faint detail,
                     most noise.

    Both images get the same treatment, since `rgbcomp -lum=` expects
    comparably-stretched inputs. Parameters are exposed rather than baked
    in because this is the step meant to be tuned interactively at a
    checkpoint -- but unlike the previous hand-picked GHT defaults, these
    adapt to the data instead of assuming a background of zero.
    """
    lum_path = Path(lum_path)
    rgb_path = Path(rgb_path)
    work_dir = Path(work_dir)

    def apply(path: Path) -> SirilResult:
        return _apply_stretch(path, work_dir, method, shadows_clip, target_background, ghs_strength, siril_cli)

    lum_log = apply(lum_path)
    rgb_log = apply(rgb_path)

    composite_path, compose_log = rgbcomp_lum(lum_path, rgb_path, output_stem, work_dir, siril_cli=siril_cli)

    return ComposeResult(
        composite_path=composite_path,
        lum_stretch_log=lum_log,
        rgb_stretch_log=rgb_log,
        compose_log=compose_log,
    )


@dataclass
class RGBComposeResult:
    composite_path: Path
    rgb_stretch_log: SirilResult


def stretch_rgb(
    rgb_path: str | Path,
    work_dir: str | Path,
    output_stem: str = "rgb_composite",
    method: str = "autostretch",
    shadows_clip: float = DEFAULT_SHADOWS_CLIP,
    target_background: float = DEFAULT_TARGET_BACKGROUND,
    ghs_strength: float = 2.0,
    siril_cli: Path | None = None,
) -> RGBComposeResult:
    """Stretch rgb_path alone and export it under output_stem -- the
    RGB-only-mode (2026-09) counterpart of stretch_and_compose(), for a
    run with no Luminance to composite onto (see pipeline.run_lrgb's
    is_rgb_only branch). No `rgbcomp -lum=` call.

    Mirrors rgbcomp_lum()'s own output-path convention (a NEW file at
    `work_dir / f"{output_stem}.fit"`, inputs left untouched) rather than
    stretching rgb_path in place: copy first, stretch the copy -- so a
    caller passing the same rgb_path used elsewhere in the pipeline (e.g.
    the reconciled composite still needed for a resumed run) doesn't get
    it silently mutated as a side effect of exporting a stretched version.
    """
    rgb_path = Path(rgb_path)
    work_dir = Path(work_dir)
    output_path = work_dir / f"{output_stem}.fit"
    shutil.copy2(rgb_path, output_path)

    rgb_log = _apply_stretch(output_path, work_dir, method, shadows_clip, target_background, ghs_strength, siril_cli)

    return RGBComposeResult(composite_path=output_path, rgb_stretch_log=rgb_log)
