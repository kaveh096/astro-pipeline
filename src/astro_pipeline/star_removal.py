"""Optional pipeline stage: star removal / star-layer separation
(capability C, 2026-09).

Given an already-stretched final composite, produce a starless version
and a separate star-layer version. Deliberately does NOT recombine them
-- that stays a manual creative step, consistent with this skill's
existing framing/cropping/colour-grading boundary (see SKILL.md).

Tool choice, verified real, not guessed: StarNet2 (starnetastro.com), the
actively maintained successor to the original open-source StarNet++
(unmaintained since ~2021). Siril 1.4.4 has its own `starnet` wrapper
command, but it requires a separately-installed external StarNet CLI
anyway (with its path set in Siril's own preferences) and a
TIFF-I/O-capable Siril build -- calling StarNet2 directly is simpler and
accepts FITS natively, no TIFF round-trip needed.

Licensing, verified against the real bundled LICENSE.txt (StarNet2 CLI
2.6.0, downloaded 2026-09): free to use for astrophotography image
processing, but explicitly prohibits sharing "full-scale raw input
images, such as original raw, TIFF, or FITS files, that demonstrate the
software's performance." This is why every test in this module's test
suite uses small SYNTHETIC star fields, never real astronomical data --
not just this project's usual convention (see background_color.py's
GraXpert tests), but a real license requirement here specifically. No
restriction on CLI/scripting automation itself, or on documenting the
command-line invocation.

Real CLI syntax, verified against the installed 2.6.0 CLI's own --help
(differs slightly from this tool's own web docs): `-i` (input, required),
`-o` (starless output), `-n` (unscreen star-layer output -- the actual
star image, not a binary mask), `-m` (optional binary mask, unused by
this wrapper), `--linear` (apply automatic MTF to linear input and
restore the result afterward -- NOT needed when the input is already a
stretched final composite, which is this wrapper's default target).

Real, measured minimum input size: a 256x256 and a 300x300 synthetic
test image both failed with "Image size is too small for the window!";
600x600 worked. Real astrophotography final composites are always far
larger than this threshold, so it does not matter for real usage --
noted here only because it constrains synthetic test fixture sizes.

Real, measured performance: CPU-only (DirectML/GPU execution provider is
unavailable on this development machine, same underlying limitation as
GraXpert's -- see background_color.py's run_graxpert_denoise docstring),
a 2048x2048 synthetic image took 3m30s; a 600x600 image took a few
seconds. Extrapolating to a real 4096x4096 final composite (~4x the
2048x2048 pixel count) suggests roughly 14 minutes on CPU -- far more
practical than GraXpert's denoising (which extrapolated to multiple
HOURS at full resolution on this same machine), so no unusually long
default timeout is needed here.

Real, measured NaN behavior, differs from GraXpert: StarNet2 does NOT
crash or silently produce all-NaN garbage on NaN input (unlike
GraXpert's real, twice-confirmed silent-corruption bug). Instead it
silently treats NaN as literal 0.0 and processes it as real dark signal
-- deterministic and non-corrupting, but it does not preserve the "no
data" semantic either. This module reuses the same NaN-fill-before/
optional-restore-after pattern as background_color.py's GraXpert
wrappers anyway, for behavioral consistency across the pipeline's
external-tool call sites, even though StarNet2 itself would not crash
without it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

DEFAULT_STARNET_CANDIDATES = [
    Path.home()
    / "AppData"
    / "Local"
    / "Programs"
    / "StarNet2"
    / "starnet2_win_2.6.0-0231_ORT_x64_cli"
    / "starnet2.exe",
]


class StarNetError(RuntimeError):
    pass


@dataclass
class StarRemovalResult:
    starless_path: Path
    stars_path: Path


def find_starnet() -> Path:
    for candidate in DEFAULT_STARNET_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"starnet2.exe not found in known locations: {[str(c) for c in DEFAULT_STARNET_CANDIDATES]}"
    )


def run_star_removal(
    fits_path: str | Path,
    output_dir: str | Path,
    output_stem: str,
    starnet_exe: Path | None = None,
    linear: bool = False,
    stride: int | None = None,
    timeout: float | None = 1800,
    restore_nan: bool = False,
) -> StarRemovalResult:
    """Run StarNet2 to separate an image into a starless version and a
    star-layer version. Neither output is a recombination of the other --
    both are exported as siblings of the input, for a human to work with
    manually afterward.

    `linear=True` should be passed when `fits_path` is a LINEAR composite
    (e.g. the reconciled pre-stretch RGB) rather than this wrapper's
    default target, an already-stretched final composite -- StarNet2
    applies its own automatic MTF stretch to linear input and restores
    linearity in its output when this flag is set.

    Output paths are `<output_dir>/<output_stem>_starless.fit` and
    `<output_dir>/<output_stem>_stars.fit`. Raises StarNetError if either
    expected output file is missing after the call.
    """
    fits_path = Path(fits_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    exe = starnet_exe or find_starnet()

    starless_path = output_dir / f"{output_stem}_starless.fit"
    stars_path = output_dir / f"{output_stem}_stars.fit"

    source_data = fits.getdata(fits_path, memmap=False)
    nan_mask = ~np.isfinite(source_data)
    input_path = fits_path
    if nan_mask.any():
        data, header = fits.getdata(fits_path, header=True, memmap=False)
        fill_value = float(np.nanmedian(data))
        input_path = output_dir / f"{fits_path.stem}__nanfilled.fit"
        fits.writeto(
            input_path,
            np.where(nan_mask, fill_value, data).astype(np.float32),
            header=header,
            overwrite=True,
        )

    command = [
        str(exe),
        "-i", str(input_path),
        "-o", str(starless_path),
        "-n", str(stars_path),
    ]
    if linear:
        command.append("--linear")
    if stride is not None:
        command += ["-s", str(stride)]

    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if input_path != fits_path:
        input_path.unlink(missing_ok=True)

    missing = [p.name for p in (starless_path, stars_path) if not p.exists()]
    if missing:
        raise StarNetError(
            f"StarNet2 did not produce {missing} (exit code {proc.returncode}). "
            "stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-20:])
        )

    if restore_nan and nan_mask.any():
        for path in (starless_path, stars_path):
            data, header = fits.getdata(path, header=True, memmap=False)
            data = np.where(nan_mask, np.nan, data).astype(np.float32)
            fits.writeto(path, data, header=header, overwrite=True)

    return StarRemovalResult(starless_path=starless_path, stars_path=stars_path)
