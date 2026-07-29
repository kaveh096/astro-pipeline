# astro-pipeline

An agentic solution to my astro photography pipeline using free CLI and scripting tools.

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

Early scaffolding. See `research/2026-07-27-tooling-research.md` for the tool
survey and the design/implementation plan for the full stage-by-stage
architecture.

## Toolchain

- [Siril](https://siril.org) 1.4+ — calibration, registration, stacking,
  plate solving, drizzle, GHT stretch, SPCC, `rgbcomp`
- [GraXpert](https://graxpert.com) — AI background extraction, denoise
- [ASTAP](https://www.hnsky.org/astap.htm) — plate-solve fallback
- [Cosmic Clarity](https://www.setiastro.com/cosmic-clarity) — AI sharpen/denoise/star-removal
- Python: astropy, reproject, photutils, tifffile

## Setup (Windows)

External tools (installed side-by-side with existing software, nothing removed):

- Siril 1.4.3 — `C:\Program Files\SiriL\bin\siril-cli.exe`
- GraXpert 3.0.2 — `%LOCALAPPDATA%\Programs\GraXpert\GraXpert.exe` (has a real `-cli` flag)
- ASTAP + D20 star database — `C:\Program Files\astap\astap_cli.exe`
- Photoshop CS6 64-bit (already installed) — COM ProgID `Photoshop.Application`

Python: this project targets 3.12, installed side-by-side with the system
Python via the official installer (not the system default, no PATH changes):

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[windows,dev]"
```

## Layout

```
src/astro_pipeline/   Pipeline stage modules (Layer 1, standalone CLI)
scripts/              Siril .ssf script templates
skill/                Claude Code skill wrapper (Layer 2)
config/               Defaults, target-alias table
tests/                Tests, run against real sample session fixtures
```
