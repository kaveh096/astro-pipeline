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
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .siril_driver import SirilResult, run_script


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

    Real bug worth noting: Siril's `save <stem>` refuses to overwrite an
    existing file (unlike `stack -out=`/`calibrate -prefix=`, which
    overwrite freely) -- verified real, "FITS error: failed to create new
    file (already exists?)". A naive `load <stem> / ght / save <stem>`
    only appears to work when the input's extension happens to differ from
    Siril's own save default (verified: this masked the bug in earlier
    manual testing using .fits-suffixed inputs, while Siril defaults to
    writing .fit) -- with the .fit-suffixed files this pipeline actually
    uses everywhere else (masters, calibrated lights), it would hit the
    collision every time. Fixed by saving to a temp stem inside the Siril
    script, then having Python overwrite fits_path afterward (os.replace
    is atomic and allows overwriting on Windows, unlike a raw file write).
    """
    fits_path = Path(fits_path)
    work_dir = Path(work_dir)
    stem = fits_path.stem
    tmp_stem = f"{stem}__ght_tmp"

    cmd = _ght_command(strength, black_point, linear_point, symmetry_point, highlight_point, weighting, channels)

    result = run_script([f"load {stem}", cmd, f"save {tmp_stem}"], workdir=work_dir, siril_cli=siril_cli)

    tmp_path = work_dir / f"{tmp_stem}.fit"
    if not tmp_path.exists():
        tmp_path = work_dir / f"{tmp_stem}.fits"
    if not tmp_path.exists():
        raise RuntimeError(f"Siril reported success but no '{tmp_stem}.fit(s)' was created in {work_dir}.")
    os.replace(tmp_path, fits_path)

    return result


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


def stretch_and_compose(
    lum_path: str | Path,
    rgb_path: str | Path,
    work_dir: str | Path,
    output_stem: str = "lrgb_composite",
    lum_strength: float = 0.5,
    rgb_strength: float = 0.5,
    siril_cli: Path | None = None,
) -> ComposeResult:
    """Convenience wrapper: independently GHT-stretch lum_path and
    rgb_path, then combine via rgbcomp -lum=. Strength is exposed per
    channel-set since L and RGB typically need different amounts of
    stretch -- this is meant to be the tunable parameter surfaced to the
    user at the checkpoint, not a fixed default.

    Default of 0.5 (GHT's -D= range is 0-10) is deliberately conservative,
    verified against real data: an earlier, untested default of 3.0-4.0
    crushed an entire real M51 LRGB composite into the top ~1.5% of the
    value range (median 0.988, essentially blown-out white) -- 0.5
    produced a real, properly distributed stretch (background ~0.11,
    genuine spread to saturated highlights) on the same data. Still just
    a starting point for interactive tuning, not a value to trust blindly
    on different data.
    """
    lum_path = Path(lum_path)
    rgb_path = Path(rgb_path)
    work_dir = Path(work_dir)

    lum_log = ght_stretch(lum_path, work_dir, strength=lum_strength, weighting=None)
    rgb_log = ght_stretch(rgb_path, work_dir, strength=rgb_strength, weighting="human")

    composite_path, compose_log = rgbcomp_lum(lum_path, rgb_path, output_stem, work_dir, siril_cli=siril_cli)

    return ComposeResult(
        composite_path=composite_path,
        lum_stretch_log=lum_log,
        rgb_stretch_log=rgb_log,
        compose_log=compose_log,
    )
