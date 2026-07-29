from pathlib import Path

import pytest

from astro_pipeline import siril_driver
from astro_pipeline.siril_driver import (
    SirilError,
    _build_script_text,
    _parse_log_lines,
    check_version,
    find_siril_cli,
    run_pyscript,
    run_script,
)

try:
    SIRIL_CLI = find_siril_cli()
    SIRIL_AVAILABLE = True
except FileNotFoundError:
    SIRIL_CLI = None
    SIRIL_AVAILABLE = False

requires_siril = pytest.mark.skipif(not SIRIL_AVAILABLE, reason="Siril not installed on this machine")


# --- pure functions: no Siril install required -----------------------------


def test_build_script_text_inserts_requires_if_missing() -> None:
    text = _build_script_text(["stack r_pp_light"])
    assert text.startswith("requires ")
    assert "stack r_pp_light" in text


def test_build_script_text_does_not_duplicate_requires() -> None:
    text = _build_script_text(["requires 1.4.0", "stack r_pp_light"])
    assert text.count("requires") == 1


def test_parse_log_lines_strips_prefix() -> None:
    raw = "log: hello\nprogress: 50%\nlog:no space\nsomething else\n"
    lines = _parse_log_lines(raw)
    assert lines == ["hello", "no space"]


# --- integration tests against the real installed Siril CLI ----------------


@requires_siril
def test_check_version_passes_on_installed_siril() -> None:
    check_version(SIRIL_CLI)  # must not raise


@requires_siril
def test_run_script_success(tmp_path: Path) -> None:
    result = run_script(["requires 1.4.0"], workdir=tmp_path, siril_cli=SIRIL_CLI)
    assert result.ok
    assert result.returncode == 0


@requires_siril
def test_run_script_failure_raises_with_log_context(tmp_path: Path) -> None:
    with pytest.raises(SirilError) as exc_info:
        run_script(["requires 1.4.0", "thiscommanddoesnotexist"], workdir=tmp_path, siril_cli=SIRIL_CLI)
    assert exc_info.value.result.returncode != 0
    assert any("unknown command" in line.lower() for line in exc_info.value.result.tail(20))


@requires_siril
def test_run_pyscript_rejects_script_outside_workdir(tmp_path: Path) -> None:
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    script = other_dir / "probe.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(ValueError):
        run_pyscript(script, workdir=tmp_path, siril_cli=SIRIL_CLI)
