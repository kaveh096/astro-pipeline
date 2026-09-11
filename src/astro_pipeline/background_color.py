"""Stages 6-7: color calibration (SPCC) and background extraction (GraXpert).

THE ACTUAL RULE, arrived at over three wrong turns: **colour calibration
requires NaN-free input.** Everything else about ordering follows from
that, and nothing else about ordering matters. This was discovered using
PCC (Siril's older, broadband method, since removed from this codebase in
favour of SPCC -- see docs/colour-calibration-catalogues.md for that
history) but the constraint is in Siril's star-photometry engine, which
SPCC also depends on, so it applies just the same.

The wrong turns are worth recording, because each looked convincing:

  1. "Colour calibration must run before GraXpert" -- concluded when it
     failed on GraXpert's output. The real cause was that GraXpert's
     output was 100% NaN (from a background clipped to exact zero
     upstream), and photometry was correctly refusing to run on garbage.
  2. "Order does not matter" -- concluded after fixing that, when it then
     succeeded in both orders. True for that data, but only because it
     happened to be NaN-free by then.
  3. Both were symptoms of the same underlying constraint, which only
     became visible when NaN was reintroduced deliberately: it dies with
     "Error computing FWHM for photometry settings adjustment" the moment
     any NaN is present, at a fraction as small as 0.27%.

Siril tolerates NaN perfectly well in stretching and compositing. It is
specifically star photometry that cannot. So the invariant to preserve is
that whatever reaches SPCC has no NaN in it -- see the nan-filling in
run_graxpert_background_extraction, and why `restore_nan` defaults to off.
The root cause behind wrong turn #1 was actually upstream (see
calibration.py's `pedestal` parameter): Siril's stack output was clipping
a slightly-negative-on-average background to exact 0.0, leaving >99.9% of
the master exactly zero with only star peaks nonzero. GraXpert's
background model silently produced 100% NaN output on that degenerate
input (no error, no warning beyond a "divide by zero" that misleadingly
also appears on healthy runs) -- and colour calibration then, correctly,
failed to compute photometry on NaN garbage. Once the pedestal fix
resolved the root cause, it succeeded both before AND after GraXpert.
Background-extraction-first is kept as the *default* order here only
because it's the more conventional practice (cleaner background for star
photometry), not because the other order is broken.

GraXpert quirk verified empirically: it always appends '.fits' to
whatever -output value is given, regardless of any extension already
present (e.g. '-output foo.fit' produces 'foo.fit.fits'). Always pass a
bare stem and expect '<stem>.fits'.

GraXpert silent-NaN-corruption quirk: GraXpert can exit 0 and write a
fully-formed, WCS-intact FITS file that is 100% (or partially) NaN, with
no error. run_graxpert_background_extraction() checks for this and raises
BackgroundExtractionError rather than reporting a false success -- this
class of bug is exactly why "file exists" was never a sufficient success
check (see the design review's original B3 finding, now proven, not just
theoretical).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from .siril_driver import SirilError, SirilResult, run_load_process_save

DEFAULT_GRAXPERT_CANDIDATES = [
    Path.home() / "AppData" / "Local" / "Programs" / "GraXpert" / "GraXpert.exe",
]


class ColorCalibrationError(RuntimeError):
    def __init__(self, message: str, result: SirilResult) -> None:
        super().__init__(message)
        self.result = result


class CatalogueUnavailableError(ColorCalibrationError):
    """SPCC's star catalogue could not be read.

    Distinguished from a genuine colour-calibration failure because the fix
    is completely different: nothing is wrong with the data, the pipeline,
    or the parameters. With `-catalog=localgaia`, this almost always means
    the local Gaia extract is missing or misconfigured -- see
    docs/colour-calibration-catalogues.md. (This class predates SPCC: it
    was written for PCC's online VizieR dependency, which returned HTTP 403
    for hours during development. PCC has since been removed in favour of
    SPCC's local catalogue, which has no such outage exposure.)
    """


_CATALOGUE_FAILURE_MARKERS = (
    "server unreachable",
    "unable to retrieve the remote catalogue",
    "catalog error, no stars identified",
    "cannot create catalogue file",
)


def _is_catalogue_unavailable(log_text: str) -> bool:
    lowered = log_text.lower()
    return any(marker in lowered for marker in _CATALOGUE_FAILURE_MARKERS)


class BackgroundExtractionError(RuntimeError):
    pass


class UnknownInstrumentError(RuntimeError):
    """A telescope has no registered InstrumentProfile in
    INSTRUMENT_PROFILES.

    Slice 3 safety fix: `INSTRUMENT_PROFILES.get(telescope, T24_PROFILE)`
    used to silently mis-profile any unrecognized telescope as T24's
    sensor/filters. SPCC models the actual spectral response of the
    sensor and filters that produced the data (see InstrumentProfile's
    docstring) -- guessing wrong here doesn't fail loudly, it just
    produces a plausible-looking but physically wrong colour solution.
    Never guess: register the telescope's real InstrumentProfile in
    INSTRUMENT_PROFILES, or refuse to run SPCC for it.
    """


@dataclass
class SPCCResult:
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


def _parse_spcc_result(result: SirilResult) -> SPCCResult:
    text = "\n".join(result.log_lines)
    k_values: dict[int, float] = {}
    for match in re.finditer(r"^K(\d): ([\d.]+)", text, re.MULTILINE):
        k_values[int(match.group(1))] = float(match.group(2))
    white_balance = (
        (k_values[0], k_values[1], k_values[2]) if all(i in k_values for i in (0, 1, 2)) else None
    )

    stars_match = re.search(r"Found a solution for color calibration using (\d+) stars", text)
    stars_used = int(stars_match.group(1)) if stars_match else None

    return SPCCResult(white_balance=white_balance, stars_used=stars_used, log=result)


@dataclass(frozen=True)
class InstrumentProfile:
    """Sensor and filter names for SPCC, as they appear in Siril's
    spcc-database.

    SPCC models the actual spectral response of the optical train, so it
    needs to know which sensor and filters produced the data -- unlike PCC,
    which only matches broadband photometry. Names must match the `name`
    field in the database JSON exactly, spaces and punctuation included.
    """

    mono_sensor: str
    red_filter: str
    green_filter: str
    blue_filter: str


# iTelescope T24: the FITS headers give 9um pixels at 4096x4096, which is a
# KAF-16803, and iTelescope run Astrodon filters on it.
T24_PROFILE = InstrumentProfile(
    mono_sensor="KAF16803",
    red_filter="Astrodon Red (E series)",
    green_filter="Astrodon Green (E series)",
    blue_filter="Astrodon Blue (E / I series)",
)

# iTelescope T73 (Chile): sensor and filters verified against real sources,
# not guessed -- real T73 FITS header (`INSTRUME='T73 ZWO ASI2600MM'`) plus
# iTelescope's own published T73 support page (2026-09 fetch:
# https://support.itelescope.net/support/solutions/articles/261371-telescope-t73),
# which states the camera is a "ZWO ASI2600MM Pro Mono (16-bit CMOS, IMX571
# sensor, 26 MP)" and the filters are "Chroma LRGB, Chroma 3nm Ha, OIII,
# SII" (narrowband irrelevant here -- SPCC only calibrates RGB, no
# `-lfilter=` parameter exists). Cross-checked against the real installed
# Siril 1.4.4 spcc-database on this machine
# (%LOCALAPPDATA%\siril\siril-spcc-database): mono_sensors/Sony_IMX.json's
# one entry has `"name": "Sony IMX411/455/461/533/571"` with its own
# comment explicitly listing "ZWO ASI2600MM Pro" as an example camera using
# that sensor -- the `name` field is what this codebase's own convention
# uses (see InstrumentProfile's docstring), verified against T24_PROFILE's
# working values, which match KAF_16803.json's `name` field, not `model`.
# mono_filters/Chroma_RGB.json has exactly "Chroma Red"/"Chroma Green"/
# "Chroma Blue" (no separate Luminance filter needed, same reason).
T73_PROFILE = InstrumentProfile(
    mono_sensor="Sony IMX411/455/461/533/571",
    red_filter="Chroma Red",
    green_filter="Chroma Green",
    blue_filter="Chroma Blue",
)

INSTRUMENT_PROFILES: dict[str, InstrumentProfile] = {"T24": T24_PROFILE, "T73": T73_PROFILE}


def run_spcc(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    profile: InstrumentProfile = T24_PROFILE,
    catalog: str = "localgaia",
    siril_cli: Path | None = None,
) -> SPCCResult:
    """Run Siril's spectrophotometric colour calibration.

    Reads the LOCAL Gaia extract, so it has no runtime dependency on any
    third-party server, and it calibrates against modelled spectral
    response rather than broadband colour. (An earlier version of this
    pipeline used PCC, Siril's broadband method, which depends on VizieR at
    runtime -- that server returned HTTP 403 for hours during development
    and blocked the pipeline entirely. PCC has been removed; see
    docs/colour-calibration-catalogues.md for the comparison that was run
    before dropping it.)

    Requires Siril >= 1.4.4. On 1.4.3 this crashed the process outright
    with an access violation at the aperture-photometry step, regardless of
    catalogue source or sensor/filter configuration; 1.4.4 fixed it,
    despite its changelog not mentioning SPCC.

    Expect relatively few stars used -- 43 on real M51/T24 data -- since
    SPCC can only use stars that have Gaia XP sampled spectra, a much
    smaller population than plain photometry. That is normal, not a sign of
    a coverage gap; verified for this field, which lies entirely inside one
    catalogue chunk even out to 3 degrees.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)

    # Filter names contain spaces, and Siril's .ssf parser splits arguments
    # on whitespace, so each argument has to be quoted.
    command = (
        f"spcc -catalog={catalog} "
        f'"-monosensor={profile.mono_sensor}" '
        f'"-rfilter={profile.red_filter}" '
        f'"-gfilter={profile.green_filter}" '
        f'"-bfilter={profile.blue_filter}"'
    )

    try:
        result = run_load_process_save(
            rgb_composite_path, [command], work_dir, siril_cli=siril_cli
        )
    except SirilError as exc:
        log_text = "\n".join(exc.result.log_lines) if exc.result else ""
        if _is_catalogue_unavailable(log_text):
            raise CatalogueUnavailableError(
                "SPCC could not read its star catalogue. With -catalog=localgaia this "
                "usually means the local Gaia extract is missing, or "
                "core.catalogue_gaia_photo points somewhere wrong -- note Siril expects a "
                "DIRECTORY of chunk files despite the .dat name. See "
                "docs/colour-calibration-catalogues.md.",
                exc.result,
            ) from exc
        raise ColorCalibrationError(
            f"SPCC failed on {rgb_composite_path.name}: {exc}", exc.result
        ) from exc

    return _parse_spcc_result(result)


def run_graxpert_background_extraction(
    fits_path: str | Path,
    output_stem: str,
    graxpert_exe: Path | None = None,
    smoothing: float = 0.0,
    correction: str = "Subtraction",
    gpu: bool = True,
    timeout: float | None = 300,
    restore_nan: bool = False,
) -> Path:
    """Run GraXpert's AI background extraction. GraXpert always appends
    '.fits' to whatever -output value is given -- output_stem must be a
    bare stem (no extension); the real output path is '<output_stem>.fits'.
    """
    fits_path = Path(fits_path)
    exe = graxpert_exe or find_graxpert()
    output_dir = fits_path.parent
    output_path = output_dir / f"{output_stem}.fits"

    # GraXpert cannot tolerate ANY NaN in its input: feeding it a frame that
    # was 0.27% NaN (harmless edge slivers left by reprojecting one channel
    # onto another's grid) produced 100% NaN output, silently, exit code 0.
    # So NaN is filled with the frame's own median before the call and
    # restored afterwards -- the filled pixels carry no real data either
    # way, but keeping them NaN downstream preserves the honest "no data"
    # marker instead of inventing background there.
    source_data = fits.getdata(fits_path, memmap=False)
    nan_mask = ~np.isfinite(source_data)
    filled_path = fits_path
    if nan_mask.any():
        data, header = fits.getdata(fits_path, header=True, memmap=False)
        fill_value = float(np.nanmedian(data))
        filled_path = output_dir / f"{fits_path.stem}__nanfilled.fit"
        fits.writeto(
            filled_path,
            np.where(nan_mask, fill_value, data).astype(np.float32),
            header=header,
            overwrite=True,
        )

    proc = subprocess.run(
        [
            str(exe),
            "-cli", "-cmd", "background-extraction",
            "-output", output_stem,
            "-smoothing", str(smoothing),
            "-correction", correction,
            "-gpu", "true" if gpu else "false",
            str(filled_path),
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        # Explicit encoding -- text=True alone uses the Windows locale
        # codec and can raise UnicodeDecodeError mid-run (verified real).
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if filled_path != fits_path:
        filled_path.unlink(missing_ok=True)

    if not output_path.exists():
        raise BackgroundExtractionError(
            f"GraXpert did not produce {output_path.name} (exit code {proc.returncode}). "
            "stdout tail:\n" + "\n".join(proc.stdout.splitlines()[-20:])
        )

    # File existing is not sufficient -- verified real, twice, from two
    # different causes: GraXpert exits 0 and writes a fully-formed FITS file
    # that is 100% NaN, with no error beyond a "divide by zero" warning that
    # also appears on healthy runs. (Causes seen so far: a background
    # clipped to exact zero upstream, see calibration.py's pedestal; and any
    # NaN at all in the input, handled above.)
    output_data = fits.getdata(output_path, memmap=False)
    nan_fraction = float(np.isnan(output_data).sum()) / output_data.size
    if nan_fraction > 0.5:
        raise BackgroundExtractionError(
            f"GraXpert produced {output_path.name} but {nan_fraction:.1%} of pixels are NaN "
            "-- treating this as a failure, not a degraded success."
        )

    # Optionally put the input's no-data regions back. Off by default,
    # because the immediate downstream consumer is colour calibration
    # (SPCC), and Siril's star photometry cannot handle NaN either --
    # restoring it here made colour calibration fail with "Error computing
    # FWHM for photometry settings adjustment", the same symptom that was
    # previously (and wrongly) read as evidence that background extraction
    # had to run *after* colour calibration.
    #
    # Siril tolerates NaN fine in stretching and compositing; it is
    # specifically photometry that cannot. So the no-data slivers stay
    # filled with background through colour calibration, and genuine NaN
    # reappears later when the colour image is reprojected onto L's grid.
    if restore_nan and nan_mask.any():
        data, header = fits.getdata(output_path, header=True, memmap=False)
        data = np.where(nan_mask, np.nan, data).astype(np.float32)
        fits.writeto(output_path, data, header=header, overwrite=True)

    return output_path


def calibrate_color_and_background(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    profile: InstrumentProfile = T24_PROFILE,
    siril_cli: Path | None = None,
    graxpert_exe: Path | None = None,
) -> tuple[SPCCResult, Path]:
    """Orchestrates Stages 6-7: GraXpert background extraction first, then
    SPCC on the result. Both orders are verified to work for colour
    calibration in general (see module docstring); background-first is
    used here as the conventional default (cleaner background for star
    photometry), not because calibration-first is broken -- swap freely if
    there's a reason to.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)

    bg_output = run_graxpert_background_extraction(
        rgb_composite_path,
        output_stem=f"{rgb_composite_path.stem}_bg",
        graxpert_exe=graxpert_exe,
    )

    spcc_result = run_spcc(bg_output, work_dir, profile=profile, siril_cli=siril_cli)

    return spcc_result, bg_output
