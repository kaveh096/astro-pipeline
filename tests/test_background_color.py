import shutil
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from astro_pipeline.background_color import (
    BackgroundExtractionError,
    CatalogueUnavailableError,
    ColorCalibrationError,
    _parse_pcc_result,
    calibrate_color_and_background,
    find_graxpert,
    run_graxpert_background_extraction,
    run_pcc,
)
from astro_pipeline.siril_driver import SirilResult, find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

try:
    GRAXPERT_EXE = find_graxpert()
    GRAXPERT_AVAILABLE = True
except FileNotFoundError:
    GRAXPERT_EXE = None
    GRAXPERT_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")
requires_graxpert = pytest.mark.skipif(not GRAXPERT_AVAILABLE, reason="GraXpert not installed on this machine")

# Real RGB composite (Red/Green/Blue BIN2 masters of M51 on T24, plate-solved,
# combined via rgbcomp) built via the pedestal-corrected calibration pipeline
# (see calibration.py -- an earlier, non-pedestal build of this same fixture
# silently produced a >99.9%-exact-zero background that broke GraXpert with
# 100% NaN output and no error; this fixture must always come from a
# pedestal-corrected build, not a bare rgbcomp of raw masters).
from conftest import RGB_NATIVE as REAL_RGB_COMPOSITE
from conftest import requires

requires_real_composite = requires(REAL_RGB_COMPOSITE)



def skip_if_catalogue_down(fn):
    """PCC fetches reference photometry from VizieR over the network. When
    that server is down or rate-limiting (seen for real as HTTP 403), these
    tests are measuring a third party's uptime rather than this code, so
    they skip instead of reporting the pipeline as broken. A genuine
    colour-calibration failure still fails, because it raises the base
    ColorCalibrationError rather than CatalogueUnavailableError.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CatalogueUnavailableError as exc:
            pytest.skip(f"online star catalogue unavailable: {exc}")

    return wrapper


def make_result(log_lines: list[str]) -> SirilResult:
    return SirilResult(returncode=0, log_lines=log_lines, raw_stdout="", raw_stderr="")


def assert_valid_pixel_data(path: Path, max_nan_fraction: float = 0.05) -> None:
    """A file existing and having the right WCS/shape is NOT sufficient --
    verified real: GraXpert can produce a fully-formed, WCS-intact FITS
    file that is 100% NaN. Every real-data test in this module must check
    actual pixel validity, not just file/header properties.
    """
    data = fits.getdata(path)
    nan_fraction = float(np.isnan(data).sum()) / data.size
    assert nan_fraction <= max_nan_fraction, f"{path.name} is {nan_fraction:.1%} NaN -- degenerate output"
    valid = data[~np.isnan(data)]
    assert valid.std() > 0, f"{path.name} has no real signal (degenerate/constant data)"


def test_parse_pcc_result_extracts_white_balance_and_star_count() -> None:
    # Real log lines captured from an actual PCC run on a real M51 RGB composite.
    log_lines = [
        "Found a solution for color calibration using 226 stars. Factors:",
        "K0: 0.558\t(deviation: 0.412)",
        "K1: 0.698\t(deviation: 0.301)",
        "K2: 1.000\t(deviation: 1.008)",
        "Photometric Color Calibration succeeded.",
    ]
    result = _parse_pcc_result(make_result(log_lines))
    assert result.white_balance == (0.558, 0.698, 1.000)
    assert result.stars_used == 226


def test_parse_pcc_result_handles_missing_data_gracefully() -> None:
    result = _parse_pcc_result(make_result(["something unrelated"]))
    assert result.white_balance is None
    assert result.stars_used is None


def test_graxpert_output_path_uses_bare_stem_convention(tmp_path: Path) -> None:
    """Regression guard for the real, verified quirk: GraXpert always
    appends .fits to -output, even if given a name with an extension."""
    if not GRAXPERT_AVAILABLE:
        pytest.skip("GraXpert not installed")
    # A synthetic gradient (not random noise -- verified that pure noise
    # with no smooth structure behaves differently in GraXpert's background
    # model) so this is a meaningful smoke test of the plumbing.
    y, x = np.mgrid[0:128, 0:128]
    synthetic = (1000 + (x + y).astype(np.float32) * 2.0)
    src = tmp_path / "tiny.fit"
    fits.writeto(src, synthetic)

    output_path = run_graxpert_background_extraction(src, output_stem="tiny_bg", timeout=120)

    assert output_path == tmp_path / "tiny_bg.fits"
    assert_valid_pixel_data(output_path)


def test_run_graxpert_raises_when_output_missing(tmp_path: Path) -> None:
    if not GRAXPERT_AVAILABLE:
        pytest.skip("GraXpert not installed")
    missing = tmp_path / "does_not_exist.fit"
    with pytest.raises((BackgroundExtractionError, FileNotFoundError, Exception)):
        run_graxpert_background_extraction(missing, output_stem="whatever", timeout=30)


def test_run_graxpert_raises_on_degenerate_nan_output(tmp_path: Path, monkeypatch) -> None:
    """Regression guard for the real silent-NaN-corruption bug: if GraXpert
    (or a future tool swapped in behind this same interface) writes a file
    that is mostly NaN, this must raise, not return a false success."""
    if not GRAXPERT_AVAILABLE:
        pytest.skip("GraXpert not installed")

    # Simulate the exact failure mode observed: valid-looking file, all-NaN data.
    fake_output = tmp_path / "fake_bg.fits"
    fits.writeto(fake_output, np.full((32, 32), np.nan, dtype=np.float32))

    import subprocess as subprocess_module

    class FakeCompletedProcess:
        returncode = 0
        stdout = "fake success output"

    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: FakeCompletedProcess())

    src = tmp_path / "input.fit"
    fits.writeto(src, np.ones((32, 32), dtype=np.float32))

    with pytest.raises(BackgroundExtractionError, match="NaN"):
        run_graxpert_background_extraction(src, output_stem="fake_bg", timeout=30)


# --- real end-to-end tests against a pedestal-corrected real composite -----


@requires_siril
@requires_graxpert
@requires_real_composite
@skip_if_catalogue_down
def test_calibrate_color_and_background_real_composite(tmp_path: Path) -> None:
    staged = tmp_path / "rgb_composite.fit"
    shutil.copy2(REAL_RGB_COMPOSITE, staged)

    pcc_result, bg_output = calibrate_color_and_background(staged, tmp_path)

    # PCC must have found a real, non-degenerate solution -- not just "ran".
    assert pcc_result.white_balance is not None
    assert pcc_result.stars_used is not None
    assert pcc_result.stars_used > 50  # real data: 225+ stars on clean input

    # GraXpert's output must exist, still carry the WCS solution, AND
    # contain real pixel data -- file-exists/WCS-survives is not sufficient
    # (verified real: both can be true on a 100%-NaN file).
    assert bg_output.exists()
    header = fits.getheader(bg_output)
    assert header.get("PLTSOLVD") is True
    assert header["NAXIS"] == 3  # still a 3-channel RGB image
    assert_valid_pixel_data(bg_output)


@requires_siril
@requires_graxpert
@requires_real_composite
@skip_if_catalogue_down
def test_pcc_works_both_before_and_after_background_extraction(tmp_path: Path) -> None:
    """Corrects an earlier, wrong conclusion from this same investigation:
    a first pass found "PCC after GraXpert fails" and concluded the order
    was hard-required. Re-tested after fixing the real root cause (see
    calibration.py's pedestal parameter, which prevents the zero-clipped
    background that was silently corrupting GraXpert's output to 100%
    NaN) -- PCC succeeds in BOTH orders on clean data. This test guards
    against reintroducing the false "order matters" belief.
    """
    staged_a = tmp_path / "pcc_first.fit"
    shutil.copy2(REAL_RGB_COMPOSITE, staged_a)
    pcc_before = run_pcc(staged_a, tmp_path)
    assert pcc_before.stars_used is not None and pcc_before.stars_used > 50

    staged_b = tmp_path / "graxpert_first.fit"
    shutil.copy2(REAL_RGB_COMPOSITE, staged_b)
    bg_output = run_graxpert_background_extraction(staged_b, output_stem="graxpert_first_bg")
    assert_valid_pixel_data(bg_output)
    pcc_after = run_pcc(bg_output, tmp_path)
    assert pcc_after.stars_used is not None and pcc_after.stars_used > 50


def test_catalogue_outage_is_distinguished_from_a_real_failure() -> None:
    """Siril fetches PCC's reference photometry from VizieR over the
    network. That server returned HTTP 403 mid-session after several
    pipeline reruns in quick succession -- an outage or rate limit, not a
    data problem. It must not look like a colour-calibration failure,
    because the fix is entirely different."""
    from astro_pipeline.background_color import _is_catalogue_unavailable

    outage = (
        "Contacting server\n"
        "Server unreachable or unresponsive (HTTP code 403 ...)\n"
        "Error: unable to retrieve the remote catalogue from the server at ...\n"
        "Catalog error, no stars identified!"
    )
    assert _is_catalogue_unavailable(outage)


def test_genuine_pcc_failure_is_not_mistaken_for_an_outage() -> None:
    """The real photometry failure seen on NaN input must still surface as
    an ordinary colour-calibration error."""
    from astro_pipeline.background_color import _is_catalogue_unavailable

    real_failure = (
        "Findstar: processing for channel 0...\n"
        "Error computing FWHM for photometry settings adjustment\n"
        "Script execution failed."
    )
    assert not _is_catalogue_unavailable(real_failure)


def test_siril_version_meets_spcc_minimum() -> None:
    """SPCC crashes the whole process on Siril 1.4.3 -- an access violation
    at the aperture-photometry step, independent of catalogue source and of
    sensor/filter configuration. 1.4.4 fixed it, with no mention of SPCC in
    its changelog. Since the pipeline now defaults to SPCC, a downgrade
    would break colour calibration in a way that looks like a crash rather
    than an error, so pin the requirement explicitly.
    """
    import re
    import subprocess

    if not SIRIL_AVAILABLE:
        pytest.skip("Siril not installed")

    out = subprocess.run(
        [str(find_siril_cli()), "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    assert match, f"could not parse Siril version from {out!r}"
    version = tuple(int(g) for g in match.groups())
    assert version >= (1, 4, 4), (
        f"Siril {'.'.join(map(str, version))} is too old for SPCC -- it crashes on "
        "1.4.3. Upgrade, or pass colour_calibration='pcc'."
    )


def test_t24_profile_names_exist_in_the_spcc_database() -> None:
    """SPCC sensor/filter names must match the spcc-database JSON `name`
    field exactly. A typo does not error -- Siril logs "(NULL)" and
    calibrates against nothing in particular, so guard the strings."""
    import json
    from pathlib import Path as _Path

    from astro_pipeline.background_color import T24_PROFILE

    db = _Path.home() / "AppData/Local/siril/siril-spcc-database"
    if not db.is_dir():
        pytest.skip("spcc-database not cloned; see docs/colour-calibration-catalogues.md")

    names = set()
    for jf in db.rglob("*.json"):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in (data if isinstance(data, list) else [data]):
            if isinstance(entry, dict) and "name" in entry:
                names.add(entry["name"])

    for label, value in (
        ("mono sensor", T24_PROFILE.mono_sensor),
        ("red filter", T24_PROFILE.red_filter),
        ("green filter", T24_PROFILE.green_filter),
        ("blue filter", T24_PROFILE.blue_filter),
    ):
        assert value in names, f"T24 {label} {value!r} not found in the SPCC database"
