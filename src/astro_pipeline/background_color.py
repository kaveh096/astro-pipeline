"""Stages 6-7: color calibration (PCC) and background extraction (GraXpert).

Order is HARD-ENFORCED: PCC (or SPCC, once a local Gaia catalog is
installed) runs before GraXpert's background extraction, never after --
proven empirically on real data, not assumed from research. Running
Siril's PCC on a GraXpert-background-subtracted real M51 RGB composite
failed outright ("Error computing FWHM for photometry settings
adjustment", "stats failed for fit", script exit 1) -- background
subtraction disrupts the pixel statistics PCC's star photometry depends
on. This directly resolves what was, before this test, a genuinely
unresolved question from the design review (two rounds of research had
disagreed on the order); a real test settled it where research couldn't.

SPCC vs PCC: SPCC (Gaia DR3 spectrophotometric) is Siril's more accurate
method, but needs a local Gaia photometric catalog (~20GB, chunked,
normally installed via Siril's GUI download manager). Without it, SPCC
falls back to an online catalog query that crashed outright (access
violation, 0xC0000005 -- reproduced twice, deterministically) partway
through aperture photometry on real data. PCC (NOMAD-based) works
reliably online with no large local catalog, at some cost to accuracy
(it used only 19 of 652 candidate stars on the same real composite, most
of the rest rejected as "image is invalid" near frame edges). **PCC is
the default for v1**; SPCC can be revisited if a local Gaia catalog is
ever installed.

GraXpert quirk verified empirically: it always appends '.fits' to
whatever -output value is given, regardless of any extension already
present (e.g. '-output foo.fit' produces 'foo.fit.fits'). Always pass a
bare stem and expect '<stem>.fits'.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .siril_driver import SirilError, SirilResult, run_script

DEFAULT_GRAXPERT_CANDIDATES = [
    Path.home() / "AppData" / "Local" / "Programs" / "GraXpert" / "GraXpert.exe",
]


class ColorCalibrationError(RuntimeError):
    def __init__(self, message: str, result: SirilResult) -> None:
        super().__init__(message)
        self.result = result


class BackgroundExtractionError(RuntimeError):
    pass


@dataclass
class PCCResult:
    white_balance: tuple[float, float, float] | None
    stars_used: int | None
    log: SirilResult


def find_graxpert() -> Path:
    for candidate in DEFAULT_GRAXPERT_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"GraXpert.exe not found in known locations: {[str(c) for c in DEFAULT_GRAXPERT_CANDIDATES]}"
    )


def _parse_pcc_result(result: SirilResult) -> PCCResult:
    text = "\n".join(result.log_lines)
    k_values: dict[int, float] = {}
    for match in re.finditer(r"^K(\d): ([\d.]+)", text, re.MULTILINE):
        k_values[int(match.group(1))] = float(match.group(2))
    white_balance = (
        (k_values[0], k_values[1], k_values[2]) if all(i in k_values for i in (0, 1, 2)) else None
    )

    stars_match = re.search(r"Found a solution for color calibration using (\d+) stars", text)
    stars_used = int(stars_match.group(1)) if stars_match else None

    return PCCResult(white_balance=white_balance, stars_used=stars_used, log=result)


def run_pcc(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    siril_cli: Path | None = None,
) -> PCCResult:
    """Run Siril's PCC on an RGB composite (already plate-solved). Must be
    called BEFORE any background extraction on this composite -- see
    module docstring for why, verified empirically. Raises
    ColorCalibrationError if Siril's script fails (e.g. too few usable
    stars) rather than silently reporting a null result.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)
    stem = rgb_composite_path.stem

    try:
        result = run_script(
            [f"load {stem}", "pcc", f"save {stem}"],
            workdir=work_dir,
            siril_cli=siril_cli,
        )
    except SirilError as exc:
        raise ColorCalibrationError(
            f"PCC failed on {rgb_composite_path.name}: {exc}", exc.result
        ) from exc

    return _parse_pcc_result(result)


def run_graxpert_background_extraction(
    fits_path: str | Path,
    output_stem: str,
    graxpert_exe: Path | None = None,
    smoothing: float = 0.0,
    correction: str = "Subtraction",
    gpu: bool = True,
    timeout: float | None = 300,
) -> Path:
    """Run GraXpert's AI background extraction. GraXpert always appends
    '.fits' to whatever -output value is given -- output_stem must be a
    bare stem (no extension); the real output path is '<output_stem>.fits'.
    """
    fits_path = Path(fits_path)
    exe = graxpert_exe or find_graxpert()
    output_dir = fits_path.parent
    output_path = output_dir / f"{output_stem}.fits"

    proc = subprocess.run(
        [
            str(exe),
            "-cli", "-cmd", "background-extraction",
            "-output", output_stem,
            "-smoothing", str(smoothing),
            "-correction", correction,
            "-gpu", "true" if gpu else "false",
            str(fits_path),
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if not output_path.exists():
        raise BackgroundExtractionError(
            f"GraXpert did not produce {output_path.name} (exit code {proc.returncode}). "
            "stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-20:])
        )
    return output_path


def calibrate_color_and_background(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    siril_cli: Path | None = None,
    graxpert_exe: Path | None = None,
) -> tuple[PCCResult, Path]:
    """Orchestrates Stages 6-7 in the empirically-required order: PCC
    first, GraXpert background extraction second. Do not reorder these --
    see module docstring."""
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)

    pcc_result = run_pcc(rgb_composite_path, work_dir, siril_cli=siril_cli)

    bg_output = run_graxpert_background_extraction(
        rgb_composite_path,
        output_stem=f"{rgb_composite_path.stem}_bg",
        graxpert_exe=graxpert_exe,
    )
    return pcc_result, bg_output
