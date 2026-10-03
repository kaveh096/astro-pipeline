---
name: astro-pipeline-lrgb
description: Use when the user wants to run, drive, resume, or continue the astrophotography pipeline in this repo against an iTelescope session folder -- phrases like "run the pipeline", "process <target>", "pick up where the pipeline left off", "build the masters for <target>", denoise/star-removal/darken a finished TIFF, or boost a broadband image with narrowband data. Covers LRGB/RGB-only/OSC composites, pure narrowband (SHO/HOO) composites, narrowband-boost (HaRGB), and optional post-processing (denoise, star removal, black-point export). Drives astro_pipeline's lrgb_orchestrator/narrowband_orchestrator stage-by-stage, pausing at each checkpoint for a human Proceed/Adjust/Abort decision rather than running to completion unattended. Does not do framing, cropping, or colour-grading -- those stay a manual step in your own image editor.
version: 0.1.0
---

# Astro Pipeline: run wrapper

Interviews the user about a project folder, then drives the pipeline
(`src/astro_pipeline/lrgb_orchestrator.py` / `narrowband_orchestrator.py`)
one stage at a time via the `skill/run_*.py` scripts, stopping at real
control-flow boundaries to show what happened and let a human Proceed /
Adjust / Abort. This skill adds no pipeline behaviour of its own -- it
calls the pipeline, reads back `result.notes` / `result.checkpoints`, and
presents them. If this file disagrees with the code, trust the code.

**Scope reminder (do not exceed this)**: flats are supported for mono
Luminance/R/G/B/narrowband groups calibrated locally (RAW_LOCAL) -- see
the "Flats" section below for what that covers and what it deliberately
does not (no automatic override of which telescope's Luminance drives the
composite -- surface the numbers, never silently pick against them; no
cross-telescope Luminance blending; no touching your image editor) --
every path below ends the moment a TIFF exists. Optional post-processing
(denoise/star-removal/black-point/narrowband-boost) never overwrites or
auto-recombines anything; it only adds new, separately-named files next
to what's already there.

## Before you start

- **iTelescope.net data only.** Light frames must match iTelescope's
  filename convention; anything else is reported as an unrecognized frame,
  not processed. See README's "Project folder layout" for the expected
  directory shape (raw delivery folders + a generated `_pipeline/` that
  the pipeline owns and you should never hand-edit).
- **Every `run_*.py` script checks its own prerequisites** (Siril's
  version, ASTAP/GraXpert/StarNet2 presence, and an SPCC colour profile)
  before writing anything, and exits 2 with a clear message if something's
  missing -- relay that message verbatim rather than guessing at a fix.
  `--skip-preflight` bypasses the check if the user says the tool
  situation is fine despite what it reports. `interview.py` reports the
  same prerequisites informationally, without blocking, since it never
  writes anything itself.
- **SPCC colour calibration only ever runs against the run's PRIMARY
  telescope** (the `--telescope` you pass) -- a telescope with
  Luminance-only data and no SPCC profile is fine and expected, since it's
  never the one SPCC needs a profile for. If the primary telescope has no
  known instrument profile, preflight reports it before anything is built.
- **Outputs**: a 16-bit TIFF + faithful preview PNG per composite, under
  the project's own `_pipeline/final/`. Intermediate/resumability state
  lives under `_pipeline/checkpoints/`. The pipeline never writes outside
  `_pipeline/`.
- **Some stages take a long time** -- a full master build with GraXpert
  background extraction, Siril `register`/`stack` on a large frame set, or
  a CPU-only denoise pass can run from several minutes to a couple of
  hours. Run a long stage as a background/detached process rather than a
  blocking foreground call, and poll for completion rather than assuming a
  long silence means it died.
- **Every script's own `--help` is the authoritative argument reference**
  -- read a script's docstring/`--help` output if anything below is
  ambiguous; this file explains the workflow, not every flag.
- Python venv: `.venv/Scripts/python` (Windows). Run every command below
  through it, e.g. `.venv/Scripts/python.exe skill/interview.py ...`.
- `examples/run_lrgb_example.py` is a worked reference for the real
  `run_lrgb` calling convention (telescope/target strings, explicit RA/Dec
  in hours/degrees -- passed explicitly so plate solving needs no network
  name resolution). Sanity-check argument shapes against it; don't invoke
  it directly for a real run.

**Which flow does the user want?**
| Data | Flow |
|---|---|
| Mono L + R/G/B, or R/G/B only, or OSC/Bayer colour | Steps 1-5 below (LRGB/RGB-only/OSC, one skill, auto-detected) |
| Narrowband only (SII/Ha/OIII), false-colour SHO or HOO | "Narrowband (SHO/HOO)" section |
| Broadband LRGB/RGB already finished, want Ha/OIII/SII blended in for colour pop | "Narrowband-boost (HaRGB)" section |
| A finished TIFF, want denoise / star removal / a specific black point before your image editor | "Optional post-processing" section |

## Step 1 -- Interview

1. **Project folder.** Ask which project folder to run against -- there
   is no default; every real run needs an explicit path to a real
   iTelescope session folder (raw FITS lights, optionally local
   bias/dark/flat calibration frames).

2. **Scan it.**
   ```
   .venv/Scripts/python.exe skill/interview.py "<project_dir>"
   ```
   This prints, from a real `scan_session()` call against that folder (no
   templated numbers):
   - which telescopes/targets/binnings/users were actually found;
   - `IngestReport.missing_calibration_warnings()`'s gaps, DEDUPLICATED to
     one line per `(telescope, frame_type, binning)` -- the raw function
     repeats one warning per user per group, which is deliberate there
     (collaborators' subs must not be silently merged at that layer), but
     unreadable for a human standing at an interview checkpoint. Show the
     deduplicated list, not the raw one; showing both for transparency is
     also fine, but the deduplicated list is what should drive the
     conversation.
   - a **dark-scaling preview**: for any group whose exact-exptime dark is
     missing, whether `calibration.select_dark()` will resolve it by
     scaling a longer/shorter dark via `-opt=exp` (safe direction), or
     whether it's a genuine `[BLOCKED]` gap `run_lrgb` will raise
     `CalibrationFramesMissingError` on. Surface `[BLOCKED]` entries
     prominently -- those are real stoppers, not cosmetic warnings.
   - a **`Precalibrated: <telescopes>` line** (only printed when at least
     one telescope qualifies): these telescopes are missing local Bias
     and/or Dark frames but DO have iTelescope-side-calibrated
     (`calibrated-` provenance) lights, so `run_lrgb` will use them
     directly (`CalibrationMode.PRECALIBRATED`) instead of locally
     recalibrating. Detected automatically; nothing to ask about or pass
     as a parameter. Its calibration-gap warnings for that telescope are
     already filtered out of the deduplicated list below it (they'd
     otherwise read as a blocking problem when they're actually
     expected).

3. **Present the summary** to the user: telescopes/targets/binnings/users
   found, which telescopes are precalibrated (if any), the deduplicated
   calibration gaps, and the dark-scaling notes. If there's a `[BLOCKED]`
   entry, say so plainly and ask whether to proceed anyway (it will fail
   loudly inside `run_lrgb` when it gets there) or stop here.

4. **Confirm the run parameters**, asking for whatever isn't already
   obvious from the scan:
   - `target` (must match one of the targets the scan found).
   - `telescope` -- the PRIMARY telescope: scopes RGB discovery and is the
     fallback Luminance source if nothing can be measured. Luminance
     discovery itself is NOT scoped to this -- every telescope with
     Luminance data for `target` gets its own master built regardless,
     and Step 2 below surfaces all of them.
   - `ra_hours` / `dec_deg` -- ask directly; `run_lrgb` needs these for
     plate solving and does no name resolution of its own. Look these up
     on Simbad or a planetarium app rather than guessing.
   - `lum_binning` (default 1) / `rgb_binning` (default 2) -- only ask if
     the scan shows more than one binning in play and it's not obvious
     which is primary.
   - `stretch_method` -- ask the user's aggressiveness preference in plain
     language and map it to one of the three real values
     `stretch_compose.stretch_and_compose` supports:
     - `"autostretch"` (default) -- shadow-clipped histogram stretch;
       the safe, always-viewable choice.
     - `"autoghs"` -- lifts more faint signal, background stays grey (no
       black point), noisier.
     - `"autoghs+auto"` -- autoghs then autostretch on top: most faint
       detail recovered, most noise. Only offer this if the user explicitly
       wants an aggressive stretch.

Do not invent a fourth option or silently default without asking -- this
interview step exists specifically so the run doesn't start on assumed
parameters.

## Step 2 -- Masters (`stop_after="masters"`)

Run:
```
.venv/Scripts/python.exe skill/run_stage.py "<project_dir>" \
  --telescope <TELESCOPE> --target <TARGET> \
  --ra-hours <RA> --dec-deg <DEC> \
  --lum-binning <LUM_BIN> --rgb-binning <RGB_BIN> \
  --stretch-method <STRETCH> \
  --stop-after masters
```
This builds (or resumes/skips, per `run_lrgb`'s own `usable()` gating)
every discovered Luminance contributor across every telescope, selects the
sharpest by measured FWHM, and builds every colour contributor with a
full R/G/B set for the primary telescope.

**Read the output, don't just glance at it:**
- The `=== NOTES ===` section contains the real Luminance-selection
  reasoning, verbatim from `select_luminance_source()` -- a line like
  `Luminance source: T24-bin1 selected as sharpest (FWHM 3.44")` for the
  winner, and `Luminance-T21-bin1: master built but NOT selected for the
  composite (FWHM 3.88" vs selected 3.44")` for every contributor that
  lost. Relay both -- the point of building every contributor is that
  it's inspectable, not just the winner.
- Also in `=== NOTES ===`: a `[run] gain/offset reference: <key>
  (STACKCNT <n>, highest)` line whenever more than one colour contributor
  was built -- this designates which contributor's numbers the
  reconciliation step (Step 3) will match every other contributor onto.
  Relay it here; it's logged at this stage, not at Step 3.
- The `=== CHECKPOINTS ===` section has one entry per checkpoint reached
  (`01_master_luminance`, and `02_primary_rgb_colour_calibrated` once
  every colour contributor for the primary telescope is done) -- each
  with background/noise/SNR/star-count/warnings.
  **RGB-only mode**: if the scan found no Luminance data for this target
  on any telescope, checkpoint `01_master_luminance` never appears -- only
  `02_primary_rgb_colour_calibrated`. This is expected, not a partial/
  failed run. `run_lrgb` detects this automatically, no parameter to set.
  The real trigger case so far is a one-shot-colour (OSC) delivery -- a
  telescope with a `Color` filter group instead of separate Luminance/
  Red/Green/Blue ones. OSC lights are genuine undemosaiced Bayer-mosaic
  sensor data, debayered automatically as part of building that
  contributor -- nothing to ask about there either.
  **If checkpoint `02_primary_rgb_colour_calibrated` never appears at
  all** and the run otherwise completed the masters stage, check the
  notes for a `[note] no colour contributor at BIN<rgb_binning> ...`
  line -- it means real colour data exists at a different binning than
  the one passed via `--rgb-binning`, and names which one(s) to retry
  with.
  RAW_LOCAL OSC (local bias/dark calibration for an OSC camera) is not
  supported yet -- `build_osc` raises `NotImplementedError` if asked for
  it; relay that message verbatim rather than trying to work around it. A
  target with BOTH a full mono R/G/B set AND `Color` data at the same
  (telescope, binning) raises `NotImplementedError` instead of silently
  combining them (channel-order parity between Siril's debayer output
  and the mono path's `rgbcomp` has never been verified) -- a real,
  deliberate limitation, not a bug; relay the exception message verbatim
  if it comes up.
- The `=== PREVIEWS ===` section lists each checkpoint's preview PNG path.
  **Actually look at them** — use the Read tool on each preview path
  before presenting the checkpoint, the same way a human would look at a
  stretched preview before deciding to proceed. Don't just relay numbers.

**Present the menu:**
```
[Masters built. Luminance: T24-bin1 selected (sharpest, 3.44" vs T21's 3.88").
 Colour contributors: T24_bin1 (27 stacked subs), T24_bin2 (29 stacked subs).]

Proceed   -- advance to reconciliation with T24-bin1 as Luminance
Adjust    -- override the Luminance source (name a different
             (telescope, binning) from the ones discovered above)
Abort     -- stop here; nothing further runs, whatever's on disk stays
             exactly as-is (run_lrgb's own resumability covers this --
             no extra cleanup needed)
```
For an RGB-only run (no Luminance data found for this target on any
telescope -- `run_lrgb` detects this automatically, nothing to set), drop
the "Luminance:" line and the "Adjust" option entirely (there is no
Luminance source to override): `[Masters built. RGB-only: no Luminance data
found for this target. Colour contributor: T02_bin1 (6 stacked subs).]` --
"Proceed" reads "advance to the final stretch + export" (RGB-only has no
reconciliation-against-Luminance step, see Step 3 below).

**Adjust**, if chosen: ask which `(telescope, binning)` should drive the
composite instead (must be one of the Luminance contributors actually
discovered and logged above -- naming anything else raises inside
`run_lrgb`). Re-run:
```
... --lum-source <TELESCOPE>:<BINNING> --stop-after masters
```
No `--force` needed: `run_lrgb`'s own run-signature diff already treats a
changed `luminance_selected` as a masters-tier change and cascades
automatically, deleting `lum_bg.fits`/`rgb_reconciled.fit`/`lrgb_final.fit`
and logging why. Only reach for `--force masters` if you need to force a
rebuild WITHOUT a signature change (e.g. re-selecting the same contributor
to rule out on-disk corruption) -- using it for an ordinary override is
harmless but redundant.

**A real failure mode to know about**: overriding Luminance to a telescope
whose field doesn't overlap the RGB contributors well enough raises
`reconciliation.ReprojectionError: Channels overlap too poorly to crop to
common coverage (would need to discard more than 25% of the frame)` once
the run reaches reconciliation. `run_lrgb` raises this loudly rather than
silently cropping to a sliver or producing garbage, which is the correct
behaviour -- if it happens, relay the exception message verbatim, don't
guess at a fix, and ask whether to pick a different Luminance source
(ideally one from the SAME telescope as the RGB data, since field
coverage is then far more likely to match) or Abort.

## Step 3 -- Reconciled (`stop_after="reconciled"`)

On Proceed from Step 2, re-run the same command with
`--stop-after reconciled` (drop `--force`/`--lum-source` unless
deliberately still overriding). This adds L background extraction and,
whenever there IS a Luminance to reconcile against, reprojects every
colour contributor onto L's pixel grid; with more than one colour
contributor it additionally does the gain/offset match + STACKCNT-weighted
combine against the reference designated in Step 2. (A single-contributor,
Luminance-driven run still reprojects -- there's just nothing to gain-match
against, so that part is skipped.)

**RGB-only mode**: no L background extraction happens (there is no L),
and with the single supported RGB-only shape (exactly one colour
contributor, see Step 2's note) there is nothing to reconcile against
either -- `rgb_reconciled.fit` is just the one contributor's output,
copied through. Checkpoint `03_lum_background_extracted` never appears;
only `04_rgb_reconciled` does. Skip straight to that checkpoint below.

Relay from `=== NOTES ===` (Luminance-driven, multi-contributor runs
only -- a single-contributor run has no gain/offset fit to relay, since
there is nothing to match against):
- each non-reference contributor's fitted gain and background numbers
  (`gain=... on N high-signal px`);
- the combine weights actually used.

Look at the `03_lum_background_extracted` and `04_rgb_reconciled`
checkpoint previews (RGB-only: `04_rgb_reconciled` only).

**Menu:**
```
Proceed -- advance to the final stretch + export
Adjust  -- re-run with a different `pedestal`, OR force a bare
           recompute of reconciliation
Abort   -- stop here
```

Be honest about what "Adjust" means here, because the two options are not
symmetric:
- **Different `pedestal`**: `pedestal` is baked into every calibrated
  light BEFORE registration/stacking (see `calibration.py`), so changing
  it invalidates the raw masters themselves, not just reconciliation. It
  is tracked by `run_lrgb`'s own run-signature diff exactly like the
  Luminance override in Step 2 -- re-run with `--pedestal <value>
  --stop-after reconciled` and the masters rebuild automatically, no
  `--force` needed. Say out loud that this will rebuild the masters, not
  just the reconciliation step, before running it.
- **Bare recompute, no parameter change**: `--force reconciled
  --stop-after reconciled` -- only useful if you suspect the on-disk
  reconciliation product is corrupted or stale in a way `run_lrgb`'s own
  signature check didn't catch; there is no other adjustable reconciliation
  parameter exposed today. Don't invent one.

## Step 4 -- Final (full run)

On Proceed from Step 3, re-run with `--stop-after final` (or omit
`--stop-after` entirely -- functionally identical). This stretches L and
the reconciled RGB (per `stretch_method`), composes via `rgbcomp -lum=`,
and exports the 16-bit TIFF + faithful preview PNG.

**RGB-only mode**: no L to compose with -- the reconciled RGB alone gets
stretched and exported directly, no `rgbcomp -lum=` call. The checkpoint
label is `05_rgb_final` (not `05_lrgb_final`), and the output file is
named `<target>_rgb.fit`/`.tif` (not `<target>_lrgb...`) -- an RGB-only
run producing a file literally named "lrgb" would be misleading on disk.

Relay the `05_lrgb_final` checkpoint (RGB-only: `05_rgb_final` -- this
one renders FAITHFULLY, not autostretched for display -- if it looks too
dark or too flat, that is real information about the chosen
`stretch_method`, not a preview artifact) and look at its preview PNG.

**Menu:**
```
Proceed / Done -- hand off (see below)
Adjust         -- a different `stretch_method`; re-run with
                  `--stretch-method <value> --force final --stop-after final`
                  (only the final stage is affected -- masters and
                  reconciliation are untouched by a stretch change)
Abort          -- stop here
```

## Step 5 -- Handoff

Report, plainly:
- the exported TIFF path (`TIFF` line from `run_stage.py`'s output --
  named `<target>_lrgb.tif`, e.g. `M51_lrgb.tif`, not a hardcoded stem;
  RGB-only mode: `<target>_rgb.tif`);
- the faithful preview PNG path, for a quick look without opening your
  image editor;
- clipped-low/clipped-high fractions from the export, if either is
  non-trivial (worth a mention -- it's a real signal about the stretch).

Then stop. **Do not open an image editor, do not attempt any framing,
cropping, or colour-grading** -- that is deliberately the user's manual
creative step, not something this skill automates. The skill's job ends
at "here's your TIFF."

## Abort, at any checkpoint

Just stop. `run_lrgb`'s own resumability (`usable()` + run-signature
tracking) is exactly what makes this safe -- whatever is on disk in
`_pipeline/` is left as-is, and a later re-run of this same skill picks
up from wherever it actually got to, without redoing completed work. No
separate cleanup step exists or is needed.

## Narrowband (SHO/HOO)

For a target shot ONLY in narrowband (SII/Ha/OIII, no L/R/G/B/OSC), skip
Steps 1-5 and run `skill/run_narrowband.py` instead -- a single call
(there is exactly one colour contributor, no reconciliation tier to pause
at). It runs straight through all three of its checkpoints
(`02_narrowband_colour_calibrated`, `03_narrowband_equalized`,
`04_<palette>_final`) without pausing between them for a Proceed/Adjust/
Abort decision -- read them all from the printed `=== CHECKPOINTS ===`/
`=== PREVIEWS ===` output the same way as Steps 2-4 above, just all at
once:
```
.venv/Scripts/python.exe skill/run_narrowband.py "<project_dir>" \
  --telescope <TELESCOPE> --target <TARGET> \
  --ra-hours <RA> --dec-deg <DEC> \
  --palette sho   # or hoo
```
Ask the user which palette:
- `sho` (Hubble palette): maps SII->Red, Ha->Green, OIII->Blue. Needs all
  three filters.
- `hoo`: maps Ha->Red, OIII->Green, OIII->Blue (bicolour, no SII needed) --
  offer this if the scan shows no SII data.

Filter-name spelling is normalized automatically (`Ha`/`H-Alpha`/`Halpha`
-> `Ha`, `OIII`/`O3` -> `OIII`, `SII`/`S2` -> `SII` -- see
`normalize_narrowband_filter_name()`); nothing to ask the user about
spelling. There is no SPCC step (no stars carry meaningful colour
information in narrowband) -- each channel is independently
background-subtracted and percentile-rescaled instead
(`equalize_narrowband_channels()`), which is the real substitute for
colour calibration here. Output: `<target>_sho.tif` / `<target>_hoo.tif`
plus a faithful preview PNG, same non-destructive scope as every other
output this skill produces.

`--force` rebuilds everything; there is no per-stage force vocabulary like
`run_stage.py`'s (only one contributor, no reconciliation boundary to
target). **Known gap**: unlike `run_lrgb`, this path has no
RunSignature-based staleness tracking yet -- re-running after changing an
upstream input does not auto-detect and cascade the way `run_lrgb` does;
if in doubt, pass `--force`.

## Narrowband-boost (HaRGB)

For a target with BOTH a finished LRGB/RGB run AND narrowband data (e.g.
Ha), to blend the narrowband layer into a broadband channel for extra
colour pop -- a real, sourced technique (lighten-style blend into Red by
default), not this skill's own invention. Only needs Step 3's output
(`rgb_reconciled.fit`, and the matching Luminance -- `lum_bg.fits`, or
`lum_bg_cropped.fits` for a multi-contributor run, picked automatically by
shape match) under `final/` or `final/_intermediate/`; it does not need
Step 4/5 to have run first:
```
.venv/Scripts/python.exe skill/run_narrowband_boost.py "<project_dir>" \
  --telescope <TELESCOPE> --target <TARGET> \
  --ra-hours <RA> --dec-deg <DEC> \
  --boost-filter Ha --boost-channel red --boost-factor 0.4 \
  [--no-luminance]   # only for an RGB-only target (nothing to recompose with)
```
What it does, in order: builds a master for `--boost-filter` at
`--binning` (default 2, matching the RGB masters), reprojects it onto the
reconciled RGB's own grid, blends it into `--boost-channel` (default red),
recombines via `rgbcomp`, and re-composes with the existing Luminance (or
stretches the boosted RGB alone, `--no-luminance`).

**Why a naive blend doesn't work, if reimplementing**: raw Siril `stack`
output has no background subtraction or cross-filter normalization -- a
real broadband and a real narrowband master can land on near-identical
absolute pixel scales, so a naive `max(Red, Ha*k)` boosts effectively
zero pixels at any realistic `k`. The narrowband layer is instead
re-expressed in the target channel's own real units first
(`rescale_narrowband_to_reference()`), and the blend itself is
background-preserving (`max(channel, channel_median*(1-k) +
narrowband*k)` via `boost_channel_with_narrowband(...,
channel_median=...)`) -- the target channel itself is never rescaled, so
its real relationship to the other two RGB channels stays correct for the
downstream `rgbcomp`.

Output: `<target>_lrgb_haboost.tif` (or `_rgb_haboost.tif`), plus the raw
registered narrowband layer exported standalone
(`<target>_<filter>_layer.tif`) as a real ingredient for manual tuning in
your image editor if the automated blend ratio isn't to taste.
Non-destructive: never touches the original LRGB/RGB TIFF.

## Optional post-processing (denoise / star removal / black point)

After a TIFF already exists (from Step 5, narrowband, or narrowband-boost),
offer this as a distinct, optional final step before your image editor --
ask the user whether they want it; never run it unprompted. Every output
is a NEW file in `final/`; the original TIFF/preview is never touched.
```
.venv/Scripts/python.exe skill/run_post_process.py "<project_dir>/_pipeline/final" \
  --target-name "<TARGET>" --black-point <value in [0, 1)> \
  [--nebula]   # star removal first; omit for galaxy targets
```
**If more than one final composite exists** under `final/` (or its
`_intermediate/`) -- e.g. a target with both a plain LRGB run and a later
narrowband-boost run -- auto-detection can't pick one and the script
raises rather than guessing; pass `--input <path>` to name the composite
to post-process explicitly.

Ask which chain applies:
- **Galaxy** (default, no `--nebula`): denoise the full composite (stars
  included -- a galaxy's own star field is not a thing to remove) ->
  `<target>_denoised_darkened.tif`.
- **Nebula** (`--nebula`): star removal FIRST on the original composite
  (not the denoised one -- denoising blurs faint stars and degrades
  detection), then denoise the STARLESS result. Star layer exported
  standalone, faithful, un-denoised, for optional manual recombination in
  your image editor (not auto-recombined -- same scope boundary as
  everything else this skill declines to automate) ->
  `<target>_starless.tif`, `<target>_stars.tif`,
  `<target>_starless_denoised_darkened.tif`.

The defaults are aesthetic starting points, not truths: the 0.10 sky
target is deliberately not darker so faint outer structure (cloudy outer
arms, IFN) survives, and the auto black point uses the whole-frame sky
median, so a channel that sits lower than the others (e.g. red near the
frame edge) can clip slightly -- check `clipped low` in the output.
`--denoise-gpu` exists but
can crash/hang on older or integrated GPUs (see README's "Configuring
tool locations") -- default to CPU denoise unless the user specifically
wants to try GPU. Resumable: if `<target>_starless.fit`/`_stars.fit`
already exist under `final/_intermediate/`, star removal is skipped and
reused.

## Flats

**Discovery**: flats are matched on `(telescope, binning, filter_name)`,
exact string, for mono Luminance/R/G/B and narrowband groups calibrated
locally (`CalibrationMode.RAW_LOCAL`) -- nothing to ask the user about,
it's automatic once a matching flat is found. Real names/folders vary by
telescope: some telescopes' flats live under a `.../T<n>/Flats/...` path
with the telescope inferred from the directory rather than the filename;
others carry the telescope directly in the filename.

**`REQUIRE` vs `SKIP_IF_MISSING`**: a telescope with ANY flat at all
defaults to `REQUIRE` (missing a flat for one of its filters is then a
real, blocking gap -- see the interview's `[BLOCKED]` annotation below); a
telescope with zero flats of any kind defaults to `SKIP_IF_MISSING`.
Override uniformly for a whole run via `--flat-policy require|
skip_if_missing` on `run_stage.py`/`run_narrowband.py`/
`run_narrowband_boost.py` if you ever need to (also available on
`interview.py`, to preview the consequence annotations as if that
override were in effect).

**Interview visibility** (`skill/interview.py`): the interview shows three
things that would otherwise be invisible --
- **Consequences**: each calibration-gap line is annotated `[BLOCKED]`
  (Bias/Dark, or a REQUIRE'd Flat -- these actually stop a real run) or
  "no flat applied, proceeding" (a SKIP'd Flat -- informational, the run
  continues without correction for that filter).
- **Matched flats**: `N frames (K copies, C collisions)` per matched
  (telescope, binning, filter) group, using a cheap header-identity
  signal -- surfaces a colliding-basename problem before a real run ever
  touches Siril.
- **Unrecognized frames**: grouped by reason, so a telescope's calibration
  library that no filename pattern (and, if enabled, no header fallback)
  could place anywhere is visible, instead of silently vanishing.

**Header-based recognition, opt-in, OFF by default**
(`--calibration-header-fallback` on `run_stage.py`/`run_narrowband.py`/
`run_narrowband_boost.py`/`interview.py`): lets `IMAGETYP`-based header
recognition find a bias/dark/flat library that no filename pattern can
place -- but recognition ALONE never changes any telescope's
`CalibrationMode` and never changes any other telescope's behaviour. To
actually exercise a header-recognised RAW_LOCAL flat/bias/dark set, pair
the fallback flag with an explicit `--calibration-mode` override:
- `run_stage.py`: `--calibration-mode TEL=raw_local` (repeatable, one
  `TEL=value` per occurrence -- a genuine multi-telescope override).
- `run_narrowband.py` / `run_narrowband_boost.py`: a plain
  `--calibration-mode raw_local` (no `TEL=` prefix -- these entry points
  already pin exactly one `--telescope`).

**What this does NOT do (stated plainly, so a reader doesn't assume more
happened than did)**:
- **No OSC flats, or any RAW_LOCAL OSC calibration at all.** `build_osc`
  raises `NotImplementedError` regardless of any flag above -- deferred,
  not committed to.
- **No narrowband master invalidation on a flat/recipe change.**
  Narrowband (`run_narrowband.py`) has no `RunSignature`-based staleness
  tracking at all (a pre-existing, non-flat-specific gap) -- pass
  `--force` explicitly after changing anything upstream.
- Header-recognition being ON never reprocesses existing data with local
  flats by itself -- it only makes the calibration library visible and
  available; an explicit `--calibration-mode ...=raw_local` override is
  still required to actually use it, and doing so is worth it only when a
  target's own before/after measurement shows a genuine improvement,
  never blind.

## Known gaps, honestly

- `run_stage.py` prints the same log lines twice in a raw terminal capture
  (once live via `run_lrgb`'s internal `print()`, once again under
  `=== NOTES ===` since that's `result.notes` printed back) -- cosmetic,
  not a bug; treat `=== NOTES ===` as the authoritative transcript.
- This skill has not been exercised against every real-world edge case
  the underlying pipeline supports -- a project with zero colour
  contributors at all, an unknown telescope (`UnknownInstrumentError`), a
  genuinely `[BLOCKED]` calibration gap, or the mixed-OSC-and-mono-RGB
  `NotImplementedError` case are all real, reachable paths that may not
  have been hit yet on a given machine's data. If one comes up, relay
  `run_lrgb`'s actual exception message rather than guessing what it
  means.
- **Limited RAM (roughly 8GB) can OOM-kill a real Siril `register`/`stack`
  call outright** on a handful of large (6000x4000+) frames. `run_lrgb`'s
  own resumability already covers this: the staged/debayered lights
  survive the kill, so simply re-running the identical `run_stage.py`
  command resumes from `register+stack` rather than restaging from
  scratch. If a real run dies this way, say so plainly and just re-run
  the same command -- don't treat it as a code bug to fix first.
- **Forcing a Luminance-tier rebuild is not byte-reproducible**, even
  reverting to the exact same parameters afterward -- GraXpert's AI
  background extraction has real run-to-run variance, so a reverted
  override can produce a differently-cropped, non-byte-identical result
  even though it is independently correct (checked via checkpoint stats
  and `usable()`'s own NaN/read-back gate). This is not a sign that an
  Adjust round-trip corrupted anything. Only a RESUMED call (nothing
  forced, nothing changed) is byte-identical.
- **CPU denoise at full resolution needs real time and RAM headroom** --
  a 4096x4096 image can take 1-2+ hours on an 8GB machine. Run it as a
  detached background process, not a foreground blocking call, and
  monitor by polling rather than assuming a long silence means it died.
- `run_narrowband.py`/`run_narrowband_boost.py` have real-data-tested unit
  coverage and a manual validation against independently-built masters,
  but `run_narrowband_boost.py` has not yet been run end-to-end as a
  script against one complete real LRGB project start to finish -- if
  it's used that way for the first time, treat the run as a fresh
  validation, not a known-good path.
- **A real bad-data case, not a code bug**: registration can fail with
  Siril's "Found 0 stars in reference" if the frame set contains
  near-zero-signal frames (cloud/focus/tracking dropouts) -- diagnose by
  checking each frame's pixel std via astropy directly, not by assuming
  the code is at fault; the fix is excluding the bad frame(s), not
  touching the pipeline.
