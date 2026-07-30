from pathlib import Path

import pytest
from astropy.io import fits

from astro_pipeline.calibration import run_calibration
from astro_pipeline.ingest import scan_session
from astro_pipeline.registration_stacking import (
    StackResult,
    _register_command,
    _stack_command,
    register_and_stack,
)
from astro_pipeline.siril_driver import find_siril_cli

try:
    find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")

REAL_SESSION_DIR = Path(r"C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025")
requires_real_session = pytest.mark.skipif(
    not REAL_SESSION_DIR.exists(), reason="Real sample session not present on this machine"
)


def test_register_command_without_drizzle() -> None:
    cmd = _register_command("pp_lights_", drizzle=False, pixfrac=1.0, kernel="square")
    assert cmd == "register pp_lights_"


def test_register_command_with_drizzle() -> None:
    cmd = _register_command("pp_lights_", drizzle=True, pixfrac=0.8, kernel="turbo")
    assert cmd == "register pp_lights_ -drizzle -pixfrac=0.8 -kernel=turbo"


def test_stack_command_with_default_filters() -> None:
    cmd = _stack_command("r_pp_lights_", "master", "rej", 3.0, 3.0, 90.0, 90.0)
    assert cmd == "stack r_pp_lights_ rej 3.0 3.0 -filter-fwhm=90.0% -filter-round=90.0% -out=master"


def test_stack_command_with_filters_disabled() -> None:
    cmd = _stack_command("r_pp_lights_", "master", "rej", 3.0, 3.0, None, None)
    assert cmd == "stack r_pp_lights_ rej 3.0 3.0 -out=master"


# --- real end-to-end test: calibrate -> register -> stack ------------------


@requires_siril
@requires_real_session
def test_register_and_stack_real_luminance_bin1(tmp_path: Path) -> None:
    report = scan_session(REAL_SESSION_DIR)
    groups = report.light_groups()
    lum_lights = groups[("T24", "kaveh096", "M51", "Luminance", 1)]
    cal_index = report.calibration_index()
    bias = cal_index[("T24", "Bias", 1, 0.0)]
    dark = cal_index[("T24", "Dark", 1, 300.0)]

    cal_result = run_calibration(lum_lights, bias, dark, work_dir=tmp_path, flat_frames=None)
    lights_dir = tmp_path / "lights"

    result = register_and_stack("pp_lights_", lights_dir, out_name="master_lum")

    assert isinstance(result, StackResult)
    assert result.master_path.exists()
    assert result.registered_sequence == "r_pp_lights_"

    # Quality filters must have actually run and be visible in the log --
    # not just "some file got created."
    stack_text = "\n".join(result.stack_log.log_lines)
    assert "filter" in stack_text.lower()

    master_data = fits.getdata(result.master_path)
    calibrated_data = fits.getdata(cal_result.calibrated_lights[0])
    assert master_data.shape == calibrated_data.shape
    # A stack of >1 frames must have real, non-degenerate signal (not a
    # blank/zero image from a failed integration silently "succeeding").
    assert master_data.std() > 0
