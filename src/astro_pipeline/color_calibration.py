"""Stage 6: color calibration (SPCC), split out of background_color.py
(Task 5 Step 16) -- see that module's own docstring for the full
background-extraction-vs-colour-calibration ordering history, which
applies to this half unchanged. This half owns everything about WHICH
sensor/filter profile SPCC uses and the SPCC Siril command itself;
`background_extraction.py` owns the separate GraXpert half.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .siril_driver import SirilError, SirilResult, run_load_process_save


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

# iTelescope T59 (Siding Spring, Australia): sensor and filters verified
# against real sources, not guessed -- iTelescope's own published T59
# support page (2026-09 fetch: support.itelescope.net/support/solutions/
# articles/254307-telescope-59) states the camera is an "FLI Proline 16803"
# with a "KAF-16803" sensor and "Astrodon E-Series Luminance Red, Green,
# Blue" filters -- identical make/model to T24's own profile. Cross-checked
# against the real T59 FITS header (`XPIXSZ=9.0`, `NAXIS1/2=4096`,
# `INSTRUME='FLI'`), which matches the KAF-16803's published 9um/4096x4096
# spec exactly (T59's own header has no INSTRUME/TELESCOP identity string
# beyond the bare camera vendor, so this cross-check is what confirms the
# sensor, not the header alone).
T59_PROFILE = InstrumentProfile(
    mono_sensor="KAF16803",
    red_filter="Astrodon Red (E series)",
    green_filter="Astrodon Green (E series)",
    blue_filter="Astrodon Blue (E / I series)",
)

INSTRUMENT_PROFILES: dict[str, InstrumentProfile] = {
    "T24": T24_PROFILE,
    "T73": T73_PROFILE,
    "T59": T59_PROFILE,
}


@dataclass(frozen=True)
class OSCInstrumentProfile:
    """Sensor (+ optional filter/LPF in front of it) for a one-shot-colour
    (OSC) camera's SPCC calibration, as they appear in Siril's spcc-database
    -- the OSC-shaped counterpart of InstrumentProfile (RGB-only/OSC plan,
    2026-09). Siril's `spcc` command has a genuinely separate argument
    shape for OSC (`-oscsensor=`/`-oscfilter=`/`-osclpf=`, verified via the
    real installed Siril 1.4.4's own `help spcc`), not a reuse of
    `-monosensor=`/`-rfilter=`/etc -- a bare OSC sensor (no filter/LPF in
    front of it, T02's real case) only ever needs `osc_sensor`.

    IMPORTANT ASYMMETRY, confirmed by a real `spcc -oscsensor=...` run
    (not assumed by analogy to InstrumentProfile): `osc_sensor` must be
    the database's `model` field, NOT its `name` field the way
    InstrumentProfile.mono_sensor/red_filter/etc use. Siril's
    osc_sensors/*.json entries split one physical sensor into per-channel
    Red/Green/Blue rows (`"model": "Sony IMX071", "name": "Sony IMX071
    Red"`, etc) -- `-oscsensor="Sony IMX071"` (the shared `model`) is what
    Siril's combo box actually resolves; the per-channel `name` values are
    Siril's own internal detail, never passed as an argument here.
    Verified real: `spcc -catalog=localgaia "-oscsensor=Sony IMX071"`
    against a real debayered, plate-solved T02 master logged `SPCC will
    use OSC sensor "Sony IMX071" and filter "No filter"` and found a real
    solution (798 stars) -- not silently guessed.
    """

    osc_sensor: str
    osc_filter: str | None = None
    osc_lpf: str | None = None


# iTelescope T02 (New Mexico): QHY168C One Shot Color CMOS, confirmed via
# iTelescope's own published T02 support page (2026-09 fetch:
# https://support.itelescope.net/support/solutions/articles/231901) and
# cross-checked against the real T02 FITS header (XPIXSZ=3.76um, matching
# the QHY168C's published pixel size exactly). The QHY168C's sensor is the
# Sony IMX071 (public spec). `osc_sensor="Sony IMX071"` confirmed working
# against the real installed Siril 1.4.4's spcc-database -- see
# OSCInstrumentProfile's own docstring for the model-vs-name asymmetry
# this required discovering, not assuming.
T02_OSC_PROFILE = OSCInstrumentProfile(osc_sensor="Sony IMX071")

OSC_INSTRUMENT_PROFILES: dict[str, OSCInstrumentProfile] = {"T02": T02_OSC_PROFILE}


def resolve_instrument_profile(telescope: str) -> InstrumentProfile:
    """SPCC's InstrumentProfile for `telescope`, or raise -- never guess.

    Factored out of _build_colour_contributor as its own function (Slice
    3.1) so this safety check is directly unit-testable without invoking
    the full calibrate/stack/solve/rgbcomp/GraXpert chain that runs before
    it in the real pipeline. Previously
    `INSTRUMENT_PROFILES.get(telescope, T24_PROFILE)` silently mis-
    profiled ANY unrecognized telescope as T24's KAF16803/Astrodon
    sensor+filters -- SPCC models the actual spectral response of the
    optical train that produced the data, so a wrong profile doesn't fail
    loudly, it just produces a plausible-looking, physically wrong colour
    solution. Raises UnknownInstrumentError instead.
    """
    profile = INSTRUMENT_PROFILES.get(telescope)
    if profile is None:
        raise UnknownInstrumentError(
            f"No SPCC InstrumentProfile registered for telescope {telescope!r} "
            f"(known: {sorted(INSTRUMENT_PROFILES)}) -- refusing to guess a "
            "sensor/filter profile for colour calibration. Register an "
            f"InstrumentProfile for {telescope!r} in INSTRUMENT_PROFILES first."
        )
    return profile


def resolve_osc_instrument_profile(telescope: str) -> OSCInstrumentProfile:
    """OSCInstrumentProfile equivalent of resolve_instrument_profile()
    (RGB-only/OSC plan, 2026-09) -- same never-guess policy: raise
    UnknownInstrumentError for any unregistered telescope rather than
    silently mis-profiling OSC data with the wrong sensor."""
    profile = OSC_INSTRUMENT_PROFILES.get(telescope)
    if profile is None:
        raise UnknownInstrumentError(
            f"No OSC SPCC InstrumentProfile registered for telescope {telescope!r} "
            f"(known: {sorted(OSC_INSTRUMENT_PROFILES)}) -- refusing to guess an OSC "
            f"sensor for colour calibration. Register an OSCInstrumentProfile for "
            f"{telescope!r} in OSC_INSTRUMENT_PROFILES first."
        )
    return profile


def run_spcc(
    rgb_composite_path: str | Path,
    work_dir: str | Path,
    profile: InstrumentProfile | OSCInstrumentProfile = T24_PROFILE,
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

    `profile` accepts either shape (RGB-only/OSC plan, 2026-09): an
    `InstrumentProfile` (mono sensor + 3 named filters, `-monosensor=`/
    `-rfilter=`/`-gfilter=`/`-bfilter=`) or an `OSCInstrumentProfile` (one
    OSC sensor + optional filter/LPF in front of it, `-oscsensor=`/
    `-oscfilter=`/`-osclpf=`) -- genuinely separate argument shapes in
    Siril's real `spcc` command (verified via `help spcc` against the real
    installed Siril 1.4.4), not a guessed reuse of the mono path.
    """
    rgb_composite_path = Path(rgb_composite_path)
    work_dir = Path(work_dir)

    # Filter names contain spaces, and Siril's .ssf parser splits arguments
    # on whitespace, so each argument has to be quoted.
    if isinstance(profile, OSCInstrumentProfile):
        command = f'spcc -catalog={catalog} "-oscsensor={profile.osc_sensor}"'
        if profile.osc_filter is not None:
            command += f' "-oscfilter={profile.osc_filter}"'
        if profile.osc_lpf is not None:
            command += f' "-osclpf={profile.osc_lpf}"'
        return _run_spcc_command(rgb_composite_path, work_dir, command, siril_cli)

    command = (
        f"spcc -catalog={catalog} "
        f'"-monosensor={profile.mono_sensor}" '
        f'"-rfilter={profile.red_filter}" '
        f'"-gfilter={profile.green_filter}" '
        f'"-bfilter={profile.blue_filter}"'
    )
    return _run_spcc_command(rgb_composite_path, work_dir, command, siril_cli)


def _run_spcc_command(
    rgb_composite_path: Path, work_dir: Path, command: str, siril_cli: Path | None
) -> SPCCResult:
    """Shared execution + error-handling for both run_spcc() argument
    shapes (mono and OSC, RGB-only/OSC plan) -- only the command string
    itself differs between them."""
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
