"""Thin driver around the Siril headless CLI.

`.ssf` scripts run via `siril-cli -s` are the primary, stable orchestration
surface (see research/2026-07-27-tooling-research.md and the design plan) --
`sirilpy` is reserved for pixel-access QA stats where there's no CLI
equivalent, since it's explicitly experimental upstream.

Empirically verified against a real Siril 1.4.3 install on this machine
(not assumed from docs): exit code 0 on success, 1 on script failure; every
output line is prefixed "log:" or "progress:"; invoking siril-cli.exe via
Python's subprocess (as opposed to directly from an MSYS/git-bash shell)
does not trigger the "msys2 environment detected" Python-init failure, so
no special environment handling is needed here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SIRIL_CLI_CANDIDATES = [
    Path(r"C:\Program Files\SiriL\bin\siril-cli.exe"),
]

MIN_REQUIRED_VERSION = (1, 4, 0)


class SirilError(RuntimeError):
    """A Siril script failed. Carries the parsed log so callers/checkpoints
    can surface *why*, not just that it failed."""

    def __init__(self, message: str, result: SirilResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass
class SirilResult:
    returncode: int
    log_lines: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, n: int = 10) -> list[str]:
        return self.log_lines[-n:]


def find_siril_cli() -> Path:
    for candidate in DEFAULT_SIRIL_CLI_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "siril-cli.exe not found in known locations: "
        f"{[str(c) for c in DEFAULT_SIRIL_CLI_CANDIDATES]}"
    )


def get_version(siril_cli: Path | None = None) -> tuple[int, int, int]:
    exe = siril_cli or find_siril_cli()
    result = subprocess.run([str(exe), "--version"], capture_output=True, text=True, check=True)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse Siril version from: {result.stdout!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_version(siril_cli: Path | None = None) -> None:
    version = get_version(siril_cli)
    if version < MIN_REQUIRED_VERSION:
        raise RuntimeError(
            f"Siril {'.'.join(map(str, version))} is older than the minimum "
            f"required {'.'.join(map(str, MIN_REQUIRED_VERSION))} "
            "(sirilpy, current drizzle/SPCC behavior all assume 1.4+)."
        )


def _parse_log_lines(raw: str) -> list[str]:
    lines = []
    for line in raw.splitlines():
        if line.startswith("log: "):
            lines.append(line[len("log: ") :])
        elif line.startswith("log:"):
            lines.append(line[len("log:") :].strip())
    return lines


def _build_script_text(commands: list[str]) -> str:
    lines = list(commands)
    if not any(line.strip().startswith("requires") for line in lines):
        lines.insert(0, f"requires {'.'.join(map(str, MIN_REQUIRED_VERSION))}")
    return "\n".join(lines) + "\n"


def run_script(
    commands: list[str],
    workdir: str | Path,
    siril_cli: Path | None = None,
    timeout: float | None = None,
    script_name: str = "run.ssf",
) -> SirilResult:
    """Write `commands` to a .ssf script in `workdir` and run it headlessly.

    Raises SirilError on nonzero exit; the exception carries the parsed
    log so a checkpoint or caller can report *why* it failed.
    """
    exe = siril_cli or find_siril_cli()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    script_path = workdir / script_name
    script_path.write_text(_build_script_text(commands), encoding="utf-8")

    proc = subprocess.run(
        [str(exe), "-d", str(workdir), "-s", str(script_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = SirilResult(
        returncode=proc.returncode,
        log_lines=_parse_log_lines(proc.stdout),
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
    )
    if not result.ok:
        tail = "\n".join(result.tail())
        raise SirilError(
            f"Siril script '{script_name}' failed (exit {result.returncode}). "
            f"Last log lines:\n{tail}",
            result,
        )
    return result


def run_pyscript(
    python_script: str | Path,
    workdir: str | Path,
    siril_cli: Path | None = None,
    timeout: float | None = None,
) -> SirilResult:
    """Run a Python script inside Siril's managed venv via the `pyscript` command.

    `python_script` must already live in `workdir` -- Siril resolves the
    `pyscript` argument relative to its working directory (set via `-d`),
    not relative to any path passed here.
    """
    python_script = Path(python_script)
    workdir = Path(workdir)
    if python_script.parent.resolve() != workdir.resolve():
        raise ValueError(
            f"python_script must live directly in workdir ({workdir}), "
            f"got {python_script}. Siril resolves pyscript arguments "
            "relative to its own working directory."
        )
    return run_script(
        [f"pyscript {python_script.name}"],
        workdir=workdir,
        siril_cli=siril_cli,
        timeout=timeout,
        script_name=f"_pyscript_{python_script.stem}.ssf",
    )
