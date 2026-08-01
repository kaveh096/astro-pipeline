"""Stage 3: plate solving via ASTAP.

ASTAP is the primary solver here, not Siril's internal `platesolve` as the
original design sketch assumed -- verified working standalone (no "loaded
session" state to manage) and fast once given a coordinate hint, so it's the
pragmatic choice for now. Siril's own solver remains a option to add later
if ASTAP proves insufficient for some field; nothing here rules it out.

Two things verified empirically against the real installed ASTAP CLI, not
assumed from docs:

- **Exit code is 0 whether or not a solution was found.** "No solution
  found!" only shows up in stdout text. The only trustworthy success signal
  is re-reading the FITS header's PLTSOLVD key after the run -- checking
  the return code is not sufficient and will silently treat failed solves
  as successful.
- **-spd (south pole distance) is 90 + declination, not 90 - declination.**
  Got this backwards on the first attempt; ASTAP's own echoed "Start
  position" line came back with the wrong-sign declination, which is a
  good sanity check to keep an eye on.

IMPORTANT: `-update` rewrites the WCS solution directly into the given FITS
file's header, in place. Never call solve() on original source data --
always operate on a staged copy (see staging.py), same as calibration.py
does for every other Siril/tool invocation in this pipeline.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from astropy.coordinates import SkyCoord
from astropy.io import fits

DEFAULT_ASTAP_CLI_CANDIDATES = [
    Path(r"C:\Program Files\astap\astap_cli.exe"),
]


class PlateSolveError(RuntimeError):
    def __init__(self, message: str, stdout: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout


class TargetResolutionError(RuntimeError):
    pass


@dataclass
class SolveResult:
    fits_path: Path
    ra_deg: float
    dec_deg: float
    stdout: str


def find_astap_cli() -> Path:
    for candidate in DEFAULT_ASTAP_CLI_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"astap_cli.exe not found in known locations: "
        f"{[str(c) for c in DEFAULT_ASTAP_CLI_CANDIDATES]}"
    )


def resolve_target_coords(target: str) -> SkyCoord:
    """Resolve a target name (e.g. 'M51') to coordinates via an online name
    resolver. Requires internet access -- raises a clear, actionable error
    rather than silently falling back to a slow blind solve, consistent
    with the checkpoint-not-full-auto design: the caller decides whether to
    retry, supply ra_hours/dec_deg explicitly, or blind-solve.
    """
    try:
        return SkyCoord.from_name(target)
    except Exception as exc:
        raise TargetResolutionError(
            f"Could not resolve target '{target}' to coordinates (needs internet "
            f"access to a name resolver). Pass ra_hours/dec_deg explicitly instead."
        ) from exc


def _south_pole_distance_deg(dec_deg: float) -> float:
    """ASTAP's -spd argument: degrees from the south celestial pole.
    dec_deg=+90 (north pole) -> spd=180; dec_deg=-90 (south pole) -> spd=0.
    """
    return 90.0 + dec_deg


def solve(
    fits_path: str | Path,
    target: str | None = None,
    ra_hours: float | None = None,
    dec_deg: float | None = None,
    search_radius_deg: float = 5.0,
    astap_cli: Path | None = None,
    timeout: float | None = 120,
) -> SolveResult:
    """Plate-solve fits_path in place via ASTAP, using a coordinate hint
    (either resolved from `target` or given directly) to keep the search
    fast rather than fully blind. Raises PlateSolveError if PLTSOLVD isn't
    set in the header afterward -- never trust ASTAP's exit code alone.

    Retries once with downsampling disabled (-z 1) if the default (auto)
    pass fails: verified real on a Blue-filter master, which had far fewer
    detectable stars (9) than Luminance/Red/Green (100+) -- plausibly lower
    sensor QE / more atmospheric scattering in blue -- and only solved once
    ASTAP's auto-downsampling was turned off so it didn't throw away the
    faint stars that were the only ones available.
    """
    fits_path = Path(fits_path)

    if ra_hours is None or dec_deg is None:
        if target is None:
            raise ValueError("Must provide either target or both ra_hours and dec_deg.")
        coord = resolve_target_coords(target)
        ra_hours = coord.ra.hour
        dec_deg = coord.dec.deg

    exe = astap_cli or find_astap_cli()
    spd_deg = _south_pole_distance_deg(dec_deg)

    def run_astap(downsample: int | None) -> subprocess.CompletedProcess:
        args = [
            str(exe),
            "-f", str(fits_path),
            "-ra", f"{ra_hours:.4f}",
            "-spd", f"{spd_deg:.4f}",
            "-fov", "0",
            "-r", str(search_radius_deg),
            "-update",
            "-log",
        ]
        if downsample is not None:
            args += ["-z", str(downsample)]
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    proc = run_astap(downsample=None)
    header = fits.getheader(fits_path)

    if not header.get("PLTSOLVD"):
        proc_retry = run_astap(downsample=1)
        header = fits.getheader(fits_path)
        if not header.get("PLTSOLVD"):
            raise PlateSolveError(
                f"ASTAP did not solve {fits_path.name} (PLTSOLVD not set after run, "
                f"including a -z 1 retry). Exit codes {proc.returncode}/{proc_retry.returncode} "
                f"-- not a reliable success signal on their own.",
                stdout=proc.stdout + "\n--- retry with -z 1 ---\n" + proc_retry.stdout,
            )

    return SolveResult(
        fits_path=fits_path,
        ra_deg=float(header["CRVAL1"]),
        dec_deg=float(header["CRVAL2"]),
        stdout=proc.stdout,
    )
