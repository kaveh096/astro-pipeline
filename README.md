# astro-pipeline

An agentic solution to an astrophotography processing pipeline using free CLI and scripting tools.

Automated, checkpointed processing pipeline for LRGB/narrowband astrophotography
data rented from iTelescope.net: calibration through stacking, pixel-grid
reconciliation across mixed binning/instruments, background extraction, color
calibration, stretch, LRGB/narrowband composition, and a 16-bit handoff to
Photoshop for final creative work.

Design goal: replace DeepSkyStacker + all-manual Photoshop stretching with a
scriptable, resumable pipeline that pauses at real checkpoints instead of
running as a single black box. A Claude Code skill (`skill/`) wraps the core
CLI to interview the user and drive it stage-by-stage.

## Status

Feature-complete for LRGB/RGB-only/OSC composites, pure narrowband (SHO/HOO),
narrowband-boost (HaRGB), and optional post-processing (denoise, star
removal, black-point export) — see `docs/ROADMAP.md` for what has shipped,
what's deliberately deferred, and the project's binding design decisions.
`research/2026-07-27-tooling-research.md` has the original tool survey and
design plan, kept for historical context.

## Toolchain

- [Siril](https://siril.org) 1.4.4+ — calibration, registration, stacking,
  plate solving, drizzle, GHT stretch, SPCC, `rgbcomp`, `pm` (pixelmath)
- [GraXpert](https://graxpert.com) — AI background extraction, AI denoise
- [ASTAP](https://www.hnsky.org/astap.htm) — plate-solve fallback
- [StarNet2](https://www.starnetastro.com) — AI star removal / star-layer
  separation (optional post-processing step)
- Python 3.11/3.12: astropy, reproject, photutils, tifffile
- Adobe Photoshop (optional) — the pipeline's own job ends at a 16-bit TIFF;
  Photoshop is only needed for the final manual creative pass

## Setup (Windows)

This pipeline is Windows-only today (Siril CLI paths, Photoshop COM
automation). External tools install side-by-side with existing software,
nothing removed or overwritten:

- **Siril 1.4.4+** — installs to `C:\Program Files\SiriL\bin\siril-cli.exe`.
  **1.4.4 or newer is required**: SPCC crashes the process outright on 1.4.3.
- **GraXpert 3.0.2+** — installs to
  `%LOCALAPPDATA%\Programs\GraXpert\GraXpert.exe`. Has a real `-cli` flag;
  GPU acceleration (`-gpu true`) needs a modern DirectML-capable GPU — CPU-only
  works everywhere, just slower (see `docs/ROADMAP.md` §2.5 if denoise hangs
  or crashes on an older/integrated GPU).
- **ASTAP + a star database** (D20 is enough for most fields) — installs to
  `C:\Program Files\astap\astap_cli.exe`. Only needed as a plate-solve
  fallback; Siril's own solver is tried first.
- **StarNet2 CLI 2.6.0** (optional, only if you want star removal) — download
  from [starnetastro.com](https://www.starnetastro.com), extract so
  `starnet2.exe` ends up at
  `%LOCALAPPDATA%\Programs\StarNet2\starnet2_win_2.6.0-0231_ORT_x64_cli\starnet2.exe`.
  Its license prohibits sharing full-scale raw input images that demonstrate
  the software's performance — doesn't affect normal use, just don't publish
  StarNet2 test/benchmark inputs.
- **Adobe Photoshop** (optional, already installed if you have it) — any
  version with COM automation (`Photoshop.Application`); only used for the
  manual creative pass after the pipeline hands off a TIFF.
- A **Gaia DR3 catalogue** for SPCC color calibration — see
  [Colour calibration: catalogues, and the per-project sky
  region](docs/colour-calibration-catalogues.md) for which one to download
  and how to install it into Siril.

Python: this project targets 3.11–3.12, installed side-by-side with the
system Python via the official installer (not the system default, no PATH
changes):

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[windows,dev]"
```

`[windows]` pulls in `pywin32`, needed for the Photoshop COM handoff.
`[dev]` pulls in `pytest`. Skip either extra if you don't need it.

## Using the skill in Claude Code

The pipeline is a standalone CLI (`src/astro_pipeline/`, `skill/run_*.py`) —
you don't need Claude Code to run it. But `skill/SKILL.md` wraps it as a
[Claude Code](https://claude.com/claude-code) skill that interviews you about
a project folder and drives the pipeline stage-by-stage, pausing at each
checkpoint for a Proceed/Adjust/Abort decision instead of running unattended.

**Simplest way to use it**: open this repo as your working directory in
Claude Code and just ask, e.g.:

```
Read skill/SKILL.md and run the pipeline for <your project folder>.
```

Claude Code can read and follow any file in the repo, so this works with no
extra setup.

**To have Claude Code recognize requests like "process \<target\>" or "denoise
this TIFF" automatically**, without naming the file, register it as a
project-scoped skill so Claude Code's own skill-discovery picks it up by its
`SKILL.md` description:

```
mkdir .claude\skills
mklink /J .claude\skills\astro-pipeline-lrgb skill
```

(`mklink /J` makes a directory junction, not a copy — no admin rights needed
on Windows, and it stays in sync with `skill/` automatically. A plain
`xcopy skill .claude\skills\astro-pipeline-lrgb /E /I` also works if you'd
rather have an independent copy.)

## Notes

- [Colour calibration: catalogues, and the per-project sky
  region](docs/colour-calibration-catalogues.md) — which Gaia catalogue to
  download for a target, how to install it, and why SPCC needs Siril 1.4.4+.
- [Calibration frames: when to use bias, darks, and how many is
  enough](docs/calibration-frame-counts.md) — why lights are calibrated
  against darks only, and what to expect per telescope.

## Layout

```
src/astro_pipeline/   Pipeline stage modules (Layer 1, standalone CLI)
scripts/              Siril .ssf script templates
skill/                Claude Code skill wrapper (Layer 2)
docs/                 Design decisions, roadmap, calibration reference notes
tests/                Tests, run against real sample session fixtures
```
