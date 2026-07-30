import shutil
from pathlib import Path

import pytest
from astropy.io import fits

from astro_pipeline.solving import (
    PlateSolveError,
    SolveResult,
    _south_pole_distance_deg,
    find_astap_cli,
    resolve_target_coords,
    solve,
)

try:
    ASTAP_CLI = find_astap_cli()
    ASTAP_AVAILABLE = True
except FileNotFoundError:
    ASTAP_CLI = None
    ASTAP_AVAILABLE = False

requires_astap = pytest.mark.skipif(not ASTAP_AVAILABLE, reason="ASTAP not installed on this machine")

REAL_LIGHT = Path(
    r"C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025"
    r"\Uncalibrated Lights - Jan 2025\T24 - 20250115"
    r"\raw-T24-kaveh096-M51-20250115-045740-Luminance-BIN1-E-300-001.fit"
)
requires_real_light = pytest.mark.skipif(
    not REAL_LIGHT.exists(), reason="Real sample light frame not present on this machine"
)

# M51: RA 13h29m52.7s, Dec +47:11:43 -- used directly (no network dependency)
# for tests that don't specifically exercise name resolution.
M51_RA_HOURS = 13.4980
M51_DEC_DEG = 47.1953


def test_south_pole_distance_north_pole() -> None:
    assert _south_pole_distance_deg(90.0) == 180.0


def test_south_pole_distance_south_pole() -> None:
    assert _south_pole_distance_deg(-90.0) == 0.0


def test_south_pole_distance_m51() -> None:
    # Regression test for the sign error caught empirically: spd is
    # 90 + dec, not 90 - dec.
    assert _south_pole_distance_deg(M51_DEC_DEG) == pytest.approx(137.1953, abs=1e-3)


def test_solve_requires_target_or_explicit_coords(tmp_path: Path) -> None:
    fake_fits = tmp_path / "fake.fit"
    fake_fits.write_bytes(b"")
    with pytest.raises(ValueError):
        solve(fake_fits)


@pytest.mark.skipif(True, reason="Requires live internet access to a name resolver -- run manually")
def test_resolve_target_coords_m51() -> None:
    coord = resolve_target_coords("M51")
    assert coord.ra.hour == pytest.approx(M51_RA_HOURS, abs=0.01)
    assert coord.dec.deg == pytest.approx(M51_DEC_DEG, abs=0.01)


# --- integration tests against the real ASTAP CLI and a real light frame ---


@requires_astap
@requires_real_light
def test_solve_real_light_with_correct_hint(tmp_path: Path) -> None:
    staged = tmp_path / "test_solve.fit"
    shutil.copy2(REAL_LIGHT, staged)

    result = solve(staged, ra_hours=M51_RA_HOURS, dec_deg=M51_DEC_DEG, search_radius_deg=5.0)

    assert isinstance(result, SolveResult)
    assert result.ra_deg == pytest.approx(202.47, abs=0.1)
    assert result.dec_deg == pytest.approx(47.18, abs=0.1)

    # Original source file must be untouched -- solve() mutates its input
    # in place, so the test must never point it at the real Desktop file.
    original_header = fits.getheader(REAL_LIGHT)
    assert "PLTSOLVD" not in original_header


@requires_astap
@requires_real_light
def test_solve_raises_plate_solve_error_when_hint_is_wrong(tmp_path: Path) -> None:
    staged = tmp_path / "test_solve_bad.fit"
    shutil.copy2(REAL_LIGHT, staged)

    # A coordinate hint nowhere near M51, with a tight search radius --
    # must fail, and must be detected via PLTSOLVD, not exit code (which
    # is 0 either way, confirmed empirically).
    with pytest.raises(PlateSolveError):
        solve(staged, ra_hours=0.0, dec_deg=-60.0, search_radius_deg=1.0, timeout=60)
