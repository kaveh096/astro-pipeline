# astro-pipeline

An agentic solution to an astrophotography processing pipeline using free CLI and scripting tools.

Automated, checkpointed processing pipeline for LRGB/narrowband astrophotography
data rented from iTelescope.net: calibration through stacking, pixel-grid
reconciliation across mixed binning/instruments, background extraction, color
calibration, stretch, LRGB/narrowband composition, and a 16-bit TIFF export
for final creative work in the image editor of your choice.

Design goal: replace DeepSkyStacker + all-manual stretching with a
scriptable, resumable pipeline that pauses at real checkpoints instead of
running as a single black box. A Claude Code skill (`skill/`) wraps the core
CLI to interview the user and drive it stage-by-stage.

## Status

Feature-complete for LRGB/RGB-only/OSC composites, pure narrowband (SHO/HOO),
narrowband-boost (HaRGB), and optional post-processing (denoise, star
removal, black-point export). See "Scope and known limitations" below for
what this does and doesn't cover.
`research/2026-07-27-tooling-research.md` has the original tool survey and
design plan (superseded in places — see the note at its top), kept for
historical context.

## Toolchain

- [Siril](https://siril.org) 1.4.4+ — calibration, registration, stacking,
  GHT stretch, SPCC, `rgbcomp`, `pm` (pixelmath)
- [ASTAP](https://www.hnsky.org/astap.htm) — plate solving (required; the
  only solver this pipeline calls)
- [GraXpert](https://graxpert.com) — AI background extraction, AI denoise
- [StarNet2](https://www.starnetastro.com) — AI star removal / star-layer
  separation (optional post-processing step)
- Python 3.11/3.12: astropy, reproject, photutils, tifffile, pillow
- Any image editor that opens a 16-bit TIFF (optional) — the pipeline's own
  job ends at the TIFF export; there is no automation of the creative pass

## Setup (Windows)

This pipeline is Windows-only today (Siril CLI paths, `.venv\Scripts\`
convention). External tools install side-by-side with existing software,
nothing removed or overwritten:

```
git clone https://github.com/kaveh096/astro-pipeline.git
cd astro-pipeline
```

- **Siril 1.4.4+** — installs to `C:\Program Files\SiriL\bin\siril-cli.exe`.
  **1.4.4 or newer is required**: SPCC crashes the process outright on 1.4.3.
- **ASTAP + a star database** (D20 is enough for most fields) — installs to
  `C:\Program Files\astap\astap_cli.exe`. **Required**: this is the only
  plate solver the pipeline calls (Siril's own solver isn't used).
- **GraXpert 3.0.2+** — installs to
  `%LOCALAPPDATA%\Programs\GraXpert\GraXpert.exe`. Has a real `-cli` flag;
  GPU acceleration needs a modern DirectML-capable GPU — older/integrated
  GPUs (e.g. pre-2020 Intel iGPUs) may crash or hang under GraXpert's GPU
  mode. CPU-only is slower but works everywhere; see "Configuring tool
  locations" below for how to force CPU.
- **StarNet2 CLI 2.6.0+** (optional, only if you want star removal) —
  any installed version is found automatically; download from
  [starnetastro.com](https://www.starnetastro.com), extract so
  `starnet2.exe` ends up at
  `%LOCALAPPDATA%\Programs\StarNet2\starnet2_win_2.6.0-0231_ORT_x64_cli\starnet2.exe`.
  Its license prohibits sharing full-scale raw input images that demonstrate
  the software's performance — doesn't affect normal use, just don't publish
  StarNet2 test/benchmark inputs.
- An **image editor that opens 16-bit TIFF** (optional, e.g. Photoshop, GIMP,
  Affinity Photo) — only for the manual creative pass after the pipeline
  hands off a TIFF; nothing in this repo automates or talks to it.
- A **Gaia DR3 catalogue** for SPCC color calibration — see
  [Colour calibration: catalogues, and the per-project sky
  region](docs/colour-calibration-catalogues.md) for which one to download
  and how to install it into Siril.

Install **Python 3.12** specifically (3.11 also works; 3.13+ is not
supported yet — `py -3.12` below assumes the official installer registered
it with the `py` launcher, not that it's your system default):

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`[dev]` pulls in `pytest`; skip it if you don't plan to run the tests.

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
`SKILL.md` description. In `cmd.exe`:

```
mkdir .claude\skills
mklink /J .claude\skills\astro-pipeline-lrgb skill
```

(`mklink` is a `cmd.exe` builtin, not a PowerShell one. In PowerShell, use
`New-Item -ItemType Junction -Path .claude\skills\astro-pipeline-lrgb -Target skill`
instead.) Either makes a directory junction, not a copy — no admin rights
needed on Windows, and it stays in sync with `skill/` automatically. A plain
`xcopy skill .claude\skills\astro-pipeline-lrgb /E /I` also works if you'd
rather have an independent copy.

## Configuring tool locations

Siril, ASTAP, GraXpert and StarNet2 are found in this order: an
`ASTRO_PIPELINE_*` environment variable naming the exact executable (an
error if set but the file doesn't exist -- a wrong override should say so,
not silently fall through), then a list of known default install
locations, then your `PATH`. Set one of these if a tool is installed
somewhere other than its default location:

| Tool | Env var |
|---|---|
| Siril | `ASTRO_PIPELINE_SIRIL_CLI` |
| ASTAP | `ASTRO_PIPELINE_ASTAP_CLI` |
| GraXpert | `ASTRO_PIPELINE_GRAXPERT_EXE` |
| StarNet2 | `ASTRO_PIPELINE_STARNET_EXE` |

Each should point at the executable itself, e.g. in PowerShell:
`$env:ASTRO_PIPELINE_SIRIL_CLI = "D:\Tools\Siril\bin\siril-cli.exe"`.

GraXpert's background-extraction step (used by the main LRGB/narrowband
pipeline) defaults to GPU acceleration. Set `ASTRO_PIPELINE_GRAXPERT_GPU=0`
to force CPU -- useful on older/integrated GPUs that crash or hang under
GraXpert's GPU mode.

## Scope and known limitations

- **iTelescope.net data only.** Light frames must match iTelescope's own
  filename convention (`raw-T24-<user>-<target>-YYYYMMDD-HHMMSS-<filter>-
  BIN<n>-<E|W>-<exptime>-<seq>.fits`, case-insensitive; `raw`/`calibrated`/
  `jpeg` provenance). Anything else is reported as an unrecognized frame,
  not processed. `.zip` deliveries are auto-extracted and scanned the same
  way.
- **Colour calibration (SPCC) only has instrument profiles for a few
  telescopes** — mono: T24, T73, T59; one-shot-colour: T02. Any other
  telescope's LRGB/RGB/OSC run will fail at the SPCC step with
  `UnknownInstrumentError` (after the masters are already built). To add
  your own telescope, add an `InstrumentProfile`/`OSCInstrumentProfile` to
  `src/astro_pipeline/color_calibration.py`. Narrowband composites don't use
  SPCC and aren't affected.
- **RGB/colour discovery is scoped to one telescope per run** — the
  `--telescope` you pass. Combining RGB data captured across multiple
  telescopes for one target isn't supported.
- **OSC (one-shot-colour) cameras with local raw calibration
  (bias/dark/flat, not iTelescope-precalibrated) aren't supported yet.**
  Precalibrated OSC lights work fine.
- **Windows only** — Siril/ASTAP/GraXpert/StarNet2 are all located via
  fixed Windows install paths, and the skill scripts assume
  `.venv\Scripts\python.exe`.
- **Some stages take a long time.** A full master build with GraXpert
  background extraction, or a CPU-only denoise pass, can run well past the
  couple of minutes a typical terminal command implies — plan to let a
  stage run to completion rather than assuming it's hung. ~8 GB of RAM is
  workable but tight for full-resolution images.
- Input: FITS (`.fit`/`.fits`/`.fts`), zip-aware. Output: 16-bit TIFF +
  PNG preview per composite, written under the project's own `_pipeline/`
  (see "Project folder layout" below).

## Quick start (without Claude Code)

The pipeline is a standalone CLI; the Claude Code skill is a convenience
wrapper around it, not a requirement. From a project folder laid out per
"Project folder layout" below:

```
.venv\Scripts\python.exe skill\interview.py <project_dir>
.venv\Scripts\python.exe skill\run_stage.py <project_dir> --telescope T24 --target M51 --ra-hours 13.498 --dec-deg 47.195
```

`interview.py` scans the folder and reports what it found (telescopes,
filters, calibration gaps) without writing anything. `run_stage.py` runs
the LRGB/RGB-only/OSC pipeline (auto-detected from what's present) and
pauses at each checkpoint. RA/Dec are the target's coordinates in decimal
hours and decimal degrees — look them up on Simbad or a planetarium app.
Outputs land in `<project_dir>/_pipeline/final/` (TIFF + PNG preview);
intermediate checkpoints are in `<project_dir>/_pipeline/checkpoints/`.
See `skill/SKILL.md` for narrowband, narrowband-boost and post-processing
(denoise/star-removal/black-point) entry points.

## Project folder layout

Everything for one target lives under a single project folder, alongside
the raw delivery folders iTelescope gives you:

```
<Target> - <telescopes> - <date>/
    Calibrations/              <- raw, from iTelescope (or your own bias/dark/flat)
    <Lights folder(s)>/        <- raw, from iTelescope (any name; scanned recursively)
    _pipeline/                 <- generated by this pipeline; never edit by hand
        final/                 <- TIFF + PNG outputs, per-composite intermediates
        checkpoints/           <- resumability state
```

The pipeline only ever writes inside `_pipeline/`; your raw delivery
folders are never modified. Camera-named bias/dark frames and
`scope_<filter>_<binning>_skyflat<n>` flats need to sit under a `T<digits>`
telescope folder for recognition; lights can be anywhere in the tree.

## Design boundaries

- **The pipeline's job ends the moment a 16-bit TIFF exists** — no framing,
  cropping, or colour-grading of any kind. That stays a manual step in
  whatever editor you use.
- **The sharpest telescope (by measured FWHM) owns Luminance**; Luminance
  is never blended across telescopes.
- **Colour calibration is SPCC-only** (Siril's Spectrophotometric Colour
  Calibration, requires Siril 1.4.4+). No alternative colour calibration
  method is implemented.

## Contributing

Issues and PRs are welcome. Most of the test suite runs against mocked
data with no external dependencies; a smaller set of real-data tests needs
your own iTelescope project data — point `tests/local_paths.py` (copy from
`tests/local_paths.py.example`, gitignored) or the matching
`ASTRO_PIPELINE_*` environment variables at it, or those tests just skip.
Code comments occasionally cite an internal planning label (e.g. "Step 3b",
"Decision Q2", "plan-flats-v4") — these refer to design discussions that
aren't published; the comment's own text is the part that matters.

## Notes

- [Colour calibration: catalogues, and the per-project sky
  region](docs/colour-calibration-catalogues.md) — which Gaia catalogue to
  download for a target, how to install it, and why SPCC needs Siril 1.4.4+.
- [Calibration frames: when to use bias, darks, and how many is
  enough](docs/calibration-frame-counts.md) — why lights are calibrated
  against darks only, and what to expect per telescope.
- External tools (Siril, GraXpert, ASTAP, StarNet2) are separately
  licensed and not bundled with this repo; see each project's own site for
  its license.

## Layout

```
src/astro_pipeline/   Pipeline stage modules (Layer 1, standalone CLI)
examples/             Worked example: calling run_lrgb() directly
skill/                Claude Code skill wrapper (Layer 2)
docs/                 Calibration reference notes
tests/                Tests, run against real sample session fixtures
```
