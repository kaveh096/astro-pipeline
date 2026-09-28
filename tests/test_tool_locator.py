"""Tests for tool_locator.py's shared resolve_tool() logic, used by every
find_siril_cli/find_astap_cli/find_graxpert/find_starnet wrapper.

All synthetic -- these exercise the resolution order itself (env var ->
candidates -> PATH), not any real installed tool.
"""

from pathlib import Path

import pytest

from astro_pipeline.tool_locator import ToolNotFoundError, resolve_tool


def test_env_var_wins_over_candidates_and_path(tmp_path, monkeypatch):
    real_exe = tmp_path / "custom-siril.exe"
    real_exe.write_text("fake exe")
    monkeypatch.setenv("ASTRO_PIPELINE_SIRIL_CLI", str(real_exe))

    result = resolve_tool(
        display_name="siril-cli.exe",
        env_var="ASTRO_PIPELINE_SIRIL_CLI",
        candidates=[Path("Z:/wrong/siril-cli.exe")],
        which_names=["siril-cli.exe"],
        install_hint="install it",
    )
    assert result == real_exe


def test_env_var_pointing_at_missing_file_raises_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_PIPELINE_SIRIL_CLI", str(tmp_path / "does-not-exist.exe"))

    with pytest.raises(ToolNotFoundError, match="ASTRO_PIPELINE_SIRIL_CLI"):
        resolve_tool(
            display_name="siril-cli.exe",
            env_var="ASTRO_PIPELINE_SIRIL_CLI",
            candidates=[],
            which_names=[],
            install_hint="install it",
        )


def test_falls_through_to_first_existing_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRO_PIPELINE_SIRIL_CLI", raising=False)
    missing = tmp_path / "missing.exe"
    present = tmp_path / "present.exe"
    present.write_text("fake exe")

    result = resolve_tool(
        display_name="siril-cli.exe",
        env_var="ASTRO_PIPELINE_SIRIL_CLI",
        candidates=[missing, present],
        which_names=[],
        install_hint="install it",
    )
    assert result == present


def test_falls_through_to_path_when_no_candidate_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRO_PIPELINE_SIRIL_CLI", raising=False)
    on_path = tmp_path / "on-path.exe"

    import astro_pipeline.tool_locator as tool_locator_module

    monkeypatch.setattr(tool_locator_module.shutil, "which", lambda name: str(on_path))

    result = resolve_tool(
        display_name="siril-cli.exe",
        env_var="ASTRO_PIPELINE_SIRIL_CLI",
        candidates=[tmp_path / "missing.exe"],
        which_names=["siril-cli.exe"],
        install_hint="install it",
    )
    assert result == on_path


def test_raises_tool_not_found_error_naming_the_tool_and_env_var(monkeypatch):
    monkeypatch.delenv("ASTRO_PIPELINE_SIRIL_CLI", raising=False)
    import astro_pipeline.tool_locator as tool_locator_module

    monkeypatch.setattr(tool_locator_module.shutil, "which", lambda name: None)

    with pytest.raises(ToolNotFoundError, match="siril-cli.exe") as exc_info:
        resolve_tool(
            display_name="siril-cli.exe",
            env_var="ASTRO_PIPELINE_SIRIL_CLI",
            candidates=[Path("Z:/nope.exe")],
            which_names=["siril-cli.exe"],
            install_hint="Install Siril from https://siril.org.",
        )
    assert "ASTRO_PIPELINE_SIRIL_CLI" in str(exc_info.value)
    assert isinstance(exc_info.value, FileNotFoundError)


def test_starnet_candidates_sorted_newest_version_first(tmp_path, monkeypatch):
    import astro_pipeline.star_removal as star_removal_module

    starnet_dir = tmp_path / "Programs" / "StarNet2"
    for version in ["starnet2_win_2.6.0-0231_ORT_x64_cli", "starnet2_win_2.10.0-0500_ORT_x64_cli", "starnet2_win_2.9.1-0400_ORT_x64_cli"]:
        d = starnet_dir / version
        d.mkdir(parents=True)
        (d / "starnet2.exe").write_text("fake exe")

    monkeypatch.setattr(star_removal_module, "_LOCALAPPDATA", tmp_path)
    candidates = star_removal_module._starnet_candidates()

    # Natural version sort (2.10.0 > 2.9.1 > 2.6.0), not lexicographic
    # (which would put "2.10.0" before "2.6.0" or "2.9.1" incorrectly).
    assert [c.parent.name for c in candidates] == [
        "starnet2_win_2.10.0-0500_ORT_x64_cli",
        "starnet2_win_2.9.1-0400_ORT_x64_cli",
        "starnet2_win_2.6.0-0231_ORT_x64_cli",
    ]


def test_graxpert_gpu_env_var_forces_cpu(monkeypatch, tmp_path):
    import astro_pipeline.background_extraction as bg

    calls = {}

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return FakeCompletedProcess()

    monkeypatch.setattr(bg.subprocess, "run", fake_run)
    monkeypatch.setattr(bg, "_nan_fill_for_graxpert", lambda fits_path, output_dir: (fits_path, None))
    monkeypatch.setenv("ASTRO_PIPELINE_GRAXPERT_GPU", "0")

    src = tmp_path / "input.fits"
    src.write_text("fake")
    output_path = tmp_path / "output.fits"
    output_path.write_text("fake")

    try:
        bg.run_graxpert_background_extraction(src, "output", graxpert_exe=Path("fake.exe"), gpu=True)
    except Exception:
        pass  # only the -gpu flag passed to subprocess matters for this test

    assert "-gpu" in calls["cmd"]
    gpu_index = calls["cmd"].index("-gpu")
    assert calls["cmd"][gpu_index + 1] == "false"


def test_graxpert_gpu_env_var_never_overrides_explicit_false(monkeypatch, tmp_path):
    import astro_pipeline.background_extraction as bg

    calls = {}

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return FakeCompletedProcess()

    monkeypatch.setattr(bg.subprocess, "run", fake_run)
    monkeypatch.setattr(bg, "_nan_fill_for_graxpert", lambda fits_path, output_dir: (fits_path, None))
    monkeypatch.delenv("ASTRO_PIPELINE_GRAXPERT_GPU", raising=False)

    src = tmp_path / "input.fits"
    src.write_text("fake")
    output_path = tmp_path / "output.fits"
    output_path.write_text("fake")

    try:
        bg.run_graxpert_background_extraction(src, "output", graxpert_exe=Path("fake.exe"), gpu=False)
    except Exception:
        pass

    gpu_index = calls["cmd"].index("-gpu")
    assert calls["cmd"][gpu_index + 1] == "false"
