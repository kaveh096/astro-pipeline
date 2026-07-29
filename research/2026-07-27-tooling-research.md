# Astro Pipeline — Tooling Research & Proposed Architecture
Date: 2026-07-27
Scope: free/open-source replacement for DeepSkyStacker + manual Photoshop stretch/LRGB combine, for iTelescope-sourced mono LRGB(+NB) data with mixed binning (L=1x1, RGB=2x2, NB sometimes higher). Constraint: pipeline must be checkpointed, not single-button — Kaveh keeps final creative control in Photoshop.

## 1. Calibration / registration / stacking (replaces DSS)

**Siril 1.4.3** (stable, actively released — monthly cadence, 1.5 dev already underway) is the only serious free/OSS candidate; DSS-class alternatives (Astro Pixel Processor) are paid-only, and PixInsight is paid and out of budget.

- CLI/headless: `siril -s script.ssf` (batch script) or `-p` (named-pipe command stream with structured log/status/progress — good for orchestration).
- Official Python API: **`sirilpy`** (`SirilInterface`, `.cmd()` to invoke any Siril command, pixel data as numpy, FITS header/statistics access). Ships with Siril, auto-managed venv. Marked *experimental*, expected to stabilize in 1.5 — pin versions.
- `.ssf` scripting covers calibration, registration (now WCS/astrometric with polynomial distortion correction — more rigorous than DSS's star-triangle matching), stacking (rejection, normalization), and `rgbcomp` composition.
- **Binning mismatch (the actual DSS pain point)** — two scriptable options:
  1. Drizzle at stack time: stack L normally, stack RGB with `-drizzle -pixfrac= -kernel=` at scale 2 to upsample onto L's pixel grid.
  2. Explicit `resample`/`binxy` commands, or Python `reproject` (WCS-based, needs plate-solved frames) for a deterministic, inspectable resample step.
  3. `rgbcomp -lum=` has an implicit auto-upscale fallback — useful as a safety net, not as the primary mechanism (you want the explicit checkpoint instead).
- **ASTAP** (free, real CLI `astap_cli`): supplementary plate-solve/verification pass per master, not a required dependency (Siril has its own solver).

## 2. Stretch / color calibration / denoise / sharpen / stars

- **GraXpert** (GPL-3, `github.com/Steffenhir/GraXpert`, stable 3.0.2 / 3.1 RC): genuine documented CLI — `graxpert -cli -cmd background-extraction|denoising [-gpu] [-ai_version]`. AI background/gradient extraction, AI denoise, (3.1+) AI deconvolution. Siril has a first-party `GraXpert-AI.py` pyscript integration so this can run inline in a Siril script.
- **Siril GHT** (Generalized Hyperbolic Transform stretch, tunable strength) and **SPCC** (Spectrophotometric Color Calibration, Gaia DR3-based, new in 1.5 — directly targets your "eyeballing color balance" complaint) are both scriptable commands, not GUI-only presets.
- **Cosmic Clarity Suite** (Seti Astro, MIT license, confirmed free/OSS): AI sharpen, AI denoise, star removal ("Dark Star"), satellite-trail removal — the free alternative to paid RC-Astro PixInsight plugins (BlurX/NoiseX). Weaker automation story than GraXpert: folder-drop input/output convention rather than clean CLI flags — still scriptable via folder-watching, just less clean.
- **StarNet2**: license/CLI details not confidently verified in this pass — treat Cosmic Clarity's Dark Star as the safer, confirmed-free star-removal path; revisit StarNet2 only if Dark Star proves insufficient.

## 3. Python glue and the Photoshop handoff

- **astropy + ccdproc** remains the standard if any calibration step needs to happen in pure Python rather than Siril — not required if Siril handles calibration end to end.
- **reproject** (astropy-affiliated, `reproject_interp/exact/adaptive`) is the correct tool if resampling is done outside Siril's drizzle — needs WCS (plate-solved) frames.
- **Layered 16-bit file handoff — this was the open question, and the answer is a deliberate no:** neither `psd-tools` nor `pytoshop` can reliably *write* a full multi-layer 16-bit PSD from scratch (both are read-oriented/unmaintained for that purpose). `psdtags` + `tifffile` can technically build a Photoshop-readable layered TIFF, but the author's own docs call it unstable and untested for anything but simple cases. **Don't build the pipeline's reliability on an experimental file-format writer.**
  - **Recommended path instead:** export clean, pixel-aligned 16-bit TIFFs per component (L, R, G, B, starless-L, stars-only) using `tifffile` (rock-solid), then assemble them into a real .psd via **Windows COM automation** (`win32com.client.Dispatch("Photoshop.Application")`) or a small ExtendScript `.jsx` — old Photoshop versions have mature, stable scripting support for "open these N TIFFs, stack as named layers, set blend modes, save as .psd." This is far more reliable than depending on an immature layered-file writer, and it's a small, well-trodden scripting surface.
- **Prior art**: thin, hobby-scale ecosystem, no dominant framework (small repos wrapping Siril, e.g. `async-siril`, `poto-siril`, plus generic ccdproc calibration scripts). One notable analog: `aescaffre/pixinsight-mcp` — an MCP server wrapping PixInsight for AI-assistant control, i.e. someone already built the "let an AI agent drive an astro tool" pattern for PixInsight. Worth a look as a reference for the skill-wrapper design, not as a dependency.

## 4. Proposed architecture

**Layer 1 — scriptable core (standalone, runnable without Claude):**
A Python CLI orchestrating discrete, resumable stages, each writing to a known location and recording state so any stage can be re-run independently:

1. Ingest — scan iTelescope download folder(s), group subs by target/filter/binning/date from FITS headers (astropy.io.fits).
2. Calibration — Siril `calibrate` (skip if iTelescope already calibrated; make this a per-run choice).
3. Registration + stacking per filter (Siril via `sirilpy`) → one master FITS per filter. **Checkpoint: inspect each master.**
4. Pixel-scale reconciliation — drizzle at stack time or explicit resample, so L/R/G/B/NB masters share a pixel grid. **Checkpoint: alignment overlay.**
5. SPCC color calibration on RGB. **Checkpoint: color sanity check.**
6. GraXpert background extraction + denoise per master (linear stage).
7. Siril GHT first-pass stretch, tunable — script surfaces the parameter, doesn't just pick one. **Checkpoint: preview.**
8. Optional Cosmic Clarity sharpen/denoise on L, Dark Star starless/stars-only split.
9. Export pixel-aligned 16-bit TIFFs per component.
10. Photoshop assembly (separate script, Windows COM/ExtendScript) — opens the TIFFs as named layers with appropriate blend modes, saves .psd, hands off to you for final creative work — exactly where you are today, minus the hours of manual channel wrangling.

**Layer 2 — Claude Code skill wrapper:**
A `SKILL.md` in this repo that interviews you at the start of a session (target, session folder(s), filters/binning present, whether iTelescope pre-calibrated, narrowband present, stretch aggressiveness preference), then drives Layer 1 stage-by-stage, pausing at the defined checkpoints to show intermediate results and ask whether to proceed or re-run a stage with adjusted parameters — matching what you asked for: agentic orchestration with your judgment gating each phase, not a black-box button.

## Confidence notes
- Siril, GraXpert, ASTAP, astropy/ccdproc/reproject: well-documented, high confidence.
- `sirilpy`: real and functional but explicitly experimental upstream — pin Siril version, expect API drift until 1.5.
- Cosmic Clarity CLI automation: confirmed free/MIT, but automation relies on folder conventions rather than clean flags — validate before relying on it unattended.
- StarNet2 licensing/CLI: unconfirmed, not load-bearing in this design (Dark Star covers the same need).
- Layered PSD/TIFF writers: deliberately excluded from the design as unreliable; TIFF-export + Photoshop-COM-assembly chosen instead.
