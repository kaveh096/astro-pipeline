"""Stage 4: register a calibrated sequence and stack it into one master.

Command syntax verified against the real installed Siril 1.4.3 CLI
(`help register`, `help stack`), not assumed from docs. Chains directly
onto calibration.py's output: `calibrate_lights()` leaves a Siril sequence
named `pp_<basename>_` on disk in its work_dir -- pass that exact sequence
name in here, don't re-derive it.

Quality-based subframe rejection (the plan's "automatic bad-frame
rejection" requirement) is Siril's own `-filter-fwhm=`/`-filter-round=`
stack options, verified working: on a real 13-frame set, `-filter-fwhm=90%
-filter-round=90%` correctly dropped 3 frames and stacked the remaining 10.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .siril_driver import SirilResult, run_script


@dataclass
class StackResult:
    master_path: Path
    registered_sequence: str
    register_log: SirilResult
    stack_log: SirilResult


def _register_command(sequence_name: str, drizzle: bool, pixfrac: float, kernel: str) -> str:
    cmd = f"register {sequence_name}"
    if drizzle:
        cmd += f" -drizzle -pixfrac={pixfrac} -kernel={kernel}"
    return cmd


def _stack_command(
    registered_sequence: str,
    out_name: str,
    rejection: str,
    sigma_low: float,
    sigma_high: float,
    filter_fwhm_pct: float | None,
    filter_round_pct: float | None,
) -> str:
    cmd = f"stack {registered_sequence} {rejection} {sigma_low} {sigma_high}"
    if filter_fwhm_pct is not None:
        cmd += f" -filter-fwhm={filter_fwhm_pct}%"
    if filter_round_pct is not None:
        cmd += f" -filter-round={filter_round_pct}%"
    cmd += f" -out={out_name}"
    return cmd


def register(
    sequence_name: str,
    work_dir: str | Path,
    drizzle: bool = False,
    pixfrac: float = 1.0,
    kernel: str = "square",
    siril_cli: Path | None = None,
) -> tuple[str, SirilResult]:
    """Register `sequence_name` (an existing Siril sequence already present
    in work_dir, e.g. 'pp_lights_') against its own reference frame.
    Returns (registered_sequence_name, SirilResult) -- the registered
    sequence is named 'r_<sequence_name>' by Siril's own convention.

    drizzle=True is only valid for dithered captures (confirmed enabled for
    this project) -- without dithering it produces upsampling artifacts
    rather than a real resolution gain. Not the default; callers doing
    cross-binning reconciliation opt in explicitly.
    """
    work_dir = Path(work_dir)
    cmd = _register_command(sequence_name, drizzle, pixfrac, kernel)
    result = run_script([cmd], workdir=work_dir, siril_cli=siril_cli)
    return f"r_{sequence_name}", result


def stack(
    registered_sequence: str,
    work_dir: str | Path,
    out_name: str = "master",
    rejection: str = "rej",
    sigma_low: float = 3.0,
    sigma_high: float = 3.0,
    filter_fwhm_pct: float | None = 90.0,
    filter_round_pct: float | None = 90.0,
    siril_cli: Path | None = None,
) -> tuple[Path, SirilResult]:
    """Stack `registered_sequence` with rejection and, by default,
    quality-based frame filtering. Pass filter_fwhm_pct/filter_round_pct=
    None to disable a given filter."""
    work_dir = Path(work_dir)
    cmd = _stack_command(
        registered_sequence, out_name, rejection, sigma_low, sigma_high, filter_fwhm_pct, filter_round_pct
    )
    result = run_script([cmd], workdir=work_dir, siril_cli=siril_cli)

    master_path = work_dir / f"{out_name}.fit"
    if not master_path.exists():
        raise RuntimeError(f"Siril reported success but {master_path} was not created.")
    return master_path, result


def register_and_stack(
    sequence_name: str,
    work_dir: str | Path,
    out_name: str = "master",
    drizzle: bool = False,
    pixfrac: float = 1.0,
    kernel: str = "square",
    rejection: str = "rej",
    sigma_low: float = 3.0,
    sigma_high: float = 3.0,
    filter_fwhm_pct: float | None = 90.0,
    filter_round_pct: float | None = 90.0,
    siril_cli: Path | None = None,
) -> StackResult:
    """Convenience wrapper chaining register() -> stack()."""
    registered_seq, register_log = register(
        sequence_name,
        work_dir,
        drizzle=drizzle,
        pixfrac=pixfrac,
        kernel=kernel,
        siril_cli=siril_cli,
    )
    master_path, stack_log = stack(
        registered_seq,
        work_dir,
        out_name=out_name,
        rejection=rejection,
        sigma_low=sigma_low,
        sigma_high=sigma_high,
        filter_fwhm_pct=filter_fwhm_pct,
        filter_round_pct=filter_round_pct,
        siril_cli=siril_cli,
    )
    return StackResult(
        master_path=master_path,
        registered_sequence=registered_seq,
        register_log=register_log,
        stack_log=stack_log,
    )
