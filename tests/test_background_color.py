import shutil
from pathlib import Path

import pytest
from astropy.io import fits

from astro_pipeline.background_color import (
    BackgroundExtractionError,
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
# combined via rgbcomp) built during development of this module.
REAL_RGB_COMPOSITE = Path(
    r"C:\Users\Kaveh\AppData\Local\Temp\claude\C--dev-astro-pipeline"
    r"\030735e3-fa3c-4002-a37d-180b9f73ef2a\scratchpad\recon_test\rgb_test\rgb_composite.fit"
)
requires_real_composite = pytest.mark.skipif(
    not REAL_RGB_COMPOSITE.exists(), reason="Real RGB composite not present on this machine"
)


def make_result(log_lines: list[str]) -> SirilResult:
    return SirilResult(returncode=0, log_lines=log_lines, raw_stdout="", raw_stderr="")


def test_parse_pcc_result_extracts_white_balance_and_star_count() -> None:
    # Real log lines captured from an actual PCC run on the M51 RGB composite.
    log_lines = [
        "Found a solution for color calibration using 19 stars. Factors:",
        "K0: 0.524\t(deviation: 0.523)",
        "K1: 0.535\t(deviation: 0.490)",
        "K2: 1.000\t(deviation: 1.008)",
        "Photometric Color Calibration succeeded.",
    ]
    result = _parse_pcc_result(make_result(log_lines))
    assert result.white_balance == (0.524, 0.535, 1.000)
    assert result.stars_used == 19


def test_parse_pcc_result_handles_missing_data_gracefully() -> None:
    result = _parse_pcc_result(make_result(["something unrelated"]))
    assert result.white_balance is None
    assert result.stars_used is None


def test_graxpert_output_path_uses_bare_stem_convention(tmp_path: Path) -> None:
    """Regression guard for the real, verified quirk: GraXpert always
    appends .fits to -output, even if given a name with an extension."""
    if not GRAXPERT_AVAILABLE:
        pytest.skip("GraXpert not installed")
    # A tiny real FITS so GraXpert has something to process.
    import numpy as np

    src = tmp_path / "tiny.fit"
    fits.writeto(src, np.random.default_rng(0).normal(size=(64, 64)).astype("float32"))

    output_path = run_graxpert_background_extraction(src, output_stem="tiny_bg", timeout=120)

    assert output_path == tmp_path / "tiny_bg.fits"
    assert output_path.exists()


def test_run_graxpert_raises_when_output_missing(tmp_path: Path) -> None:
    if not GRAXPERT_AVAILABLE:
        pytest.skip("GraXpert not installed")
    missing = tmp_path / "does_not_exist.fit"
    with pytest.raises((BackgroundExtractionError, FileNotFoundError, Exception)):
        run_graxpert_background_extraction(missing, output_stem="whatever", timeout=30)


# --- real end-to-end test: PCC then GraXpert, in the required order -------


@requires_siril
@requires_graxpert
@requires_real_composite
def test_calibrate_color_and_background_real_composite(tmp_path: Path) -> None:
    staged = tmp_path / "rgb_composite.fit"
    shutil.copy2(REAL_RGB_COMPOSITE, staged)

    pcc_result, bg_output = calibrate_color_and_background(staged, tmp_path)

    # PCC must have found a real, non-degenerate solution -- not just "ran".
    assert pcc_result.white_balance is not None
    assert pcc_result.stars_used is not None
    assert pcc_result.stars_used > 0

    # GraXpert's output must exist and still carry the WCS solution --
    # verified empirically to survive (see module docstring elsewhere).
    assert bg_output.exists()
    header = fits.getheader(bg_output)
    assert header.get("PLTSOLVD") is True
    assert header["NAXIS"] == 3  # still a 3-channel RGB image


@requires_siril
@requires_real_composite
def test_pcc_after_background_extraction_fails(tmp_path: Path) -> None:
    """Documents the empirical finding driving this module's hard-enforced
    ordering: running PCC on an already background-subtracted composite
    fails outright on real data (not just "less accurate"). If a future
    Siril/GraXpert version fixes this, this test failing is a prompt to
    revisit the ordering constraint, not a regression to silently patch
    around.
    """
    if not GRAXPERT_AVAILABLE:
        pytest.skip("GraXpert not installed")

    staged = tmp_path / "rgb_composite.fit"
    shutil.copy2(REAL_RGB_COMPOSITE, staged)

    bg_output = run_graxpert_background_extraction(staged, output_stem="bg_first")
    renamed = tmp_path / "bg_first_as_input.fit"
    shutil.copy2(bg_output, renamed)

    with pytest.raises(ColorCalibrationError):
        run_pcc(renamed, tmp_path)
