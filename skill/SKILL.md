---
name: astro-pipeline-lrgb
description: Use when the user wants to run, drive, resume, or continue the astrophotography pipeline in this repo against an iTelescope session folder -- phrases like "run the pipeline", "process <target>", "pick up where the pipeline left off", "build the masters for <target>", denoise/star-removal/darken a finished TIFF, or boost a broadband image with narrowband data. Covers LRGB/RGB-only/OSC composites, pure narrowband (SHO/HOO) composites, narrowband-boost (HaRGB), and optional post-processing (denoise, star removal, black-point export). Drives astro_pipeline.pipeline stage-by-stage, pausing at each checkpoint for a human Proceed/Adjust/Abort decision rather than running to completion unattended. Does not do framing, cropping, or colour-grading -- those stay the user's manual Photoshop step.
version: 2.0.0
---

# Astro Pipeline: run wrapper

Interviews the user about a project folder, then drives the pipeline
(`src/astro_pipeline/pipeline.py`) one stage at a time via the `skill/run_*.py`
scripts, stopping at real control-flow boundaries to show what happened and
let a human Proceed / Adjust / Abort. This skill adds no pipeline behaviour
of its own -- it calls the pipeline, reads back `result.notes` /
`result.checkpoints`, and presents them. If this file disagrees with the
code, trust the code.

**Scope reminder (do not exceed this)**: no flats (deferred, separate pass),
no automatic override of which telescope's Luminance drives the composite
(surface the numbers, never silently pick against them), no cross-telescope
Luminance blending, no touching Photoshop -- every path below ends the
moment a TIFF path exists. Optional post-processing (denoise/star-removal/
black-point/narrowband-boost) never overwrites or auto-recombines anything;
it only ever adds new, separately-named files next to what's already there.

**Which flow does the user want?**
| Data | Flow |
|---|---|
| Mono L + R/G/B, or R/G/B only, or OSC/Bayer colour | Steps 1-5 below (LRGB/RGB-only/OSC, one skill, auto-detected) |
| Narrowband only (SII/Ha/OIII), false-colour SHO or HOO | "Narrowband (SHO/HOO)" section |
| Broadband LRGB/RGB already finished, want Ha/OIII/SII blended in for colour pop | "Narrowband-boost (HaRGB)" section |
| A finished TIFF, want denoise / star removal / a specific black point before Photoshop | "Optional post-processing" section |

## Repo conventions to reuse, not reinvent

- Python venv: `.venv/Scripts/python` (Windows). Run every command below
  through it, e.g. `.venv/Scripts/python.exe skill/interview.py ...`.
- `scripts/run_m51.py` is a hardcoded reference for the real calling
  convention (`telescope="T24"`, `target="M51"`, explicit RA/Dec in hours/
  degrees -- passed explicitly so plate solving needs no network name
  resolution). Sanity-check argument shapes against it; don't invoke it
  directly for a real run.
- `skill/interview.py`, `skill/run_stage.py`, `skill/run_narrowband.py`,
  `skill/run_narrowband_boost.py`, `skill/run_post_process.py` do the actual
  work below; read their docstrings if anything here is ambiguous.

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
     repeats one warning per user per group (see `light_groups()`'s own
     docstring: that's deliberate there, collaborators' subs must not be
     silently merged at that layer, but it makes the raw output
     unreadable for a human standing at an interview checkpoint). Show
     the user the deduplicated list, not the raw one. If you want to show the
     raw-vs-deduplicated contrast for transparency, that's fine too, but
     the deduplicated list is what should drive the conversation.
   - a **dark-scaling preview**: for any group whose exact-exptime dark is
     missing, whether `calibration.select_dark()` will actually resolve
     it by scaling a longer/shorter dark via `-opt=exp` (safe direction),
     or whether it's a genuine `[BLOCKED]` gap `run_lrgb` will raise
     `CalibrationFramesMissingError` on. Surface `[BLOCKED]` entries
     prominently -- those are real stoppers, not cosmetic warnings.
   - a **`Precalibrated: <telescopes>` line** (only printed when at least
     one telescope qualifies): these telescopes have no local Bias/Dark
     frames at all but DO have iTelescope-side-calibrated (`calibrated-`
     provenance) lights, so `run_lrgb` will use them directly
     (`CalibrationMode.PRECALIBRATED`) instead of locally recalibrating --
     real case: T73 (NGC 3628) and T02 (Abell 6 and HFG1). Detected
     automatically; nothing to ask about or pass as a parameter. Its
     calibration-gap warnings for that telescope are already filtered out
     of the deduplicated list below it (they'd otherwise read as a
     blocking problem when they're actually expected).

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
     Luminance data for `target` gets its own master built regardless
     (Slice 1), and Step 2 below surfaces all of them.
   - `ra_hours` / `dec_deg` -- ask directly; `run_lrgb` needs these for
     plate solving and does no name resolution of its own. If the project
     folder is the M51 fixture, the known values are
     `ra_hours=13.4980, dec_deg=47.1953` (from `scripts/run_m51.py`) --
     confirm these rather than silently assuming them for a different
     target.
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
sharpest by measured FWHM (Slice 2), and builds every colour contributor
with a full R/G/B set for the primary telescope.

**Read the output, don't just glance at it:**
- The `=== NOTES ===` section contains the real Luminance-selection
  reasoning, verbatim from `select_luminance_source()` -- a line like
  `Luminance source: T24-bin1 selected as sharpest (FWHM 3.44")` for the
  winner, and `Luminance-T21-bin1: master built but NOT selected for the
  composite (FWHM 3.88" vs selected 3.44")` for every contributor that
  lost. Relay both -- the point of building every contributor is that
  it's inspectable, not just the winner.
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
  contributor -- nothing to ask about there either. Two real cases: T02
  (Abell 6 and HFG1, PRECALIBRATED, no local frames at all) and T68
  (IC 1396, RAW_LOCAL -- local bias but NO local dark at all; calibrates
  bias-only + debayer automatically rather than raising, since darks may
  legitimately not exist for an OSC delivery -- a real dark at that
  binning, if one DOES exist, is still used normally). A target with BOTH
  a full mono R/G/B set AND `Color` data
  at the same (telescope, binning) raises `NotImplementedError` instead
  of silently combining them (channel-order parity between Siril's
  debayer output and the mono path's `rgbcomp` has never been verified) --
  a real, deliberate limitation, not a bug; relay the exception message
  verbatim if it comes up.
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
changed `luminance_selected` as a "masters"-tier change and cascades
automatically -- verified directly on real M51 data (this slice's own
validation): overriding to `T21:1` with no `--force` at all produced
`lum_bg.fits: run signature changed (Luminance-affecting) -- deleting to
force regeneration` (and the same for `rgb_reconciled.fit`/
`lrgb_final.fit`) in the notes, unprompted. Only reach for `--force
masters` if you need to force a rebuild WITHOUT a signature change (e.g.
re-selecting the same contributor to rule out on-disk corruption) --
using it for an ordinary override is harmless but redundant.

**A real failure mode to know about, hit during this slice's own
validation**: overriding Luminance to a telescope whose field doesn't
overlap the RGB contributors well enough raises
`reconciliation.ReprojectionError: Channels overlap too poorly to crop to
common coverage (would need to discard more than 25% of the frame)` once
the run reaches reconciliation -- confirmed real on M51 (T21's practice
2-sub Luminance, `0.96"/px`, vs T24's RGB, `0.47"/px` on a different
field position). `run_lrgb` raises this loudly rather than silently
cropping to a sliver or producing garbage, which is the correct behaviour
-- if it happens, relay the exception message verbatim, don't guess at a
fix, and ask whether to pick a different Luminance source (ideally one
from the SAME telescope as the RGB data, since colour-only-rule
contributors are typically field-matched) or Abort.

## Step 3 -- Reconciled (`stop_after="reconciled"`)

On Proceed from Step 2, re-run the same command with
`--stop-after reconciled` (drop `--force`/`--lum-source` unless
deliberately still overriding). This adds L background extraction and,
when there's more than one colour contributor, the reprojection + gain/
offset match + STACKCNT-weighted combine (Slice 3.4/3.5).

**RGB-only mode**: no L background extraction happens (there is no L),
and with the single supported RGB-only shape (exactly one colour
contributor, see Step 2's note) there is nothing to reconcile against
either -- `rgb_reconciled.fit` is just the one contributor's output,
copied through. Checkpoint `03_lum_background_extracted` never appears;
only `04_rgb_reconciled` does. Skip straight to that checkpoint below.

Relay from `=== NOTES ===` (Luminance-driven runs only -- RGB-only has
no gain/offset fit to relay, since there is nothing to reconcile):
- the designated gain/offset reference (`highest STACKCNT wins`);
- each non-reference contributor's fitted gain and background numbers
  (`gain=... on N high-signal px`);
- the combine weights actually used.

Look at the `03_lum_background_extracted` and `04_rgb_reconciled`
checkpoint previews (RGB-only: `04_rgb_reconciled` only).

**Menu:**
```
Proceed -- advance to the final stretch + export
Adjust  -- re-run with a different `pedestal` (Slice 4.1), OR force a
           bare recompute of reconciliation
Abort   -- stop here
```

Be honest about what "Adjust" means here, because the two options are not
symmetric:
- **Different `pedestal`**: `pedestal` is baked into every calibrated
  light BEFORE registration/stacking (see `calibration.py`), so changing
  it invalidates the raw masters themselves, not just reconciliation.
  Re-run with `--pedestal <value> --force masters --stop-after
  reconciled` -- yes, `--force masters`, even though you're adjusting at
  the "reconciled" checkpoint, because that's genuinely what's stale.
  Say this out loud to the user before running it (it will rebuild the
  masters, not just the reconciliation step) rather than silently doing a
  slower thing than "Adjust" sounds like it should be.
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
  RGB-only mode: `<target>_rgb.tif`, e.g. `Abell 6 and HFG1_rgb.tif`);
- the faithful preview PNG path, for a quick look without opening
  Photoshop;
- clipped-low/clipped-high fractions from the export, if either is
  non-trivial (worth a mention -- it's a real signal about the stretch).

Then stop. **Do not open Photoshop, do not attempt any framing, cropping,
or colour-grading** -- that is deliberately the user's manual creative step,
not something this skill automates. The skill's job ends at "here's your
TIFF."

## Abort, at any checkpoint

Just stop. `run_lrgb`'s own resumability (Slice 4.1-4.3: `usable()` +
run-signature tracking) is exactly what makes this safe -- whatever is on
disk in `_pipeline/` is left as-is, and a later re-run of this same skill
picks up from wherever it actually got to, without redoing completed
work. No separate cleanup step exists or is needed.

## Narrowband (SHO/HOO)

For a target shot ONLY in narrowband (SII/Ha/OIII, no L/R/G/B/OSC), skip
Steps 1-5 and run `skill/run_narrowband.py` instead -- a single call, no
staged checkpoints (there is exactly one colour contributor, no
reconciliation tier to pause at):
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
plus a faithful preview PNG, same non-destructive/no-Photoshop scope as
every other output this skill produces.

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
default), not this skill's own invention. Run AFTER Step 4/5 has already
produced `rgb_reconciled.fit` (and `lum_bg.fits`, if not RGB-only) under
`final/` or `final/_intermediate/`:
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

**Real finding, don't skip this step if reimplementing**: raw Siril
`stack` output has no background subtraction or cross-filter
normalization -- a real Red master and a real Ha master can land on
near-identical absolute pixel scales, so a naive `max(Red, Ha*k)` boosts
ZERO pixels at any realistic `k` (confirmed: 0% at k=0.4 on real M42
data). The fix actually shipped: the narrowband layer is re-expressed in
the TARGET channel's own real units first (`rescale_narrowband_to_
reference()`), and the blend itself is background-preserving
(`max(channel, channel_median*(1-k) + narrowband*k)` via
`boost_channel_with_narrowband(..., channel_median=...)`) -- the target
channel itself is never rescaled, so its real relationship to the other
two RGB channels stays correct for the downstream `rgbcomp`.

Output: `<target>_lrgb_haboost.tif` (or `_rgb_haboost.tif`), plus the raw
registered narrowband layer exported standalone
(`<target>_<filter>_layer.tif`) as a real ingredient for manual Photoshop
tuning if the automated blend ratio isn't to taste. Non-destructive: never
touches the original LRGB/RGB TIFF.

## Optional post-processing (denoise / star removal / black point)

After a TIFF already exists (from Step 5, narrowband, or narrowband-boost),
offer this as a distinct, optional final step before Photoshop -- ask the
user whether they want it; never run it unprompted. Every output is a NEW
file in `final/`; the original TIFF/preview is never touched.
```
.venv/Scripts/python.exe skill/run_post_process.py "<project_dir>/_pipeline/final" \
  --target-name "<TARGET>" --black-point <0.0-1.0> \
  [--nebula]   # star removal first; omit for galaxy targets
```
Ask which chain applies:
- **Galaxy** (default, no `--nebula`): denoise the full composite (stars
  included -- a galaxy's own star field is not a thing to remove) ->
  `<target>_denoised_darkened.tif`.
- **Nebula** (`--nebula`): star removal FIRST on the original composite
  (not the denoised one -- denoising blurs faint stars and degrades
  detection, confirmed during this capability's development), then
  denoise the STARLESS result. Star layer exported standalone, faithful,
  un-denoised, for optional manual recombination in Photoshop (not
  auto-recombined -- same scope boundary as everything else this skill
  declines to automate) -> `<target>_starless.tif`, `<target>_stars.tif`,
  `<target>_starless_denoised_darkened.tif`.

`--black-point` (0.0-1.0, the low end of the export's linear stretch) has
no baked-in default on purpose -- a genuine aesthetic choice, ask the
user rather than picking one. `--denoise-gpu` exists but is known
unreliable on low-VRAM/older-GPU machines (see "Known gaps" below) --
default to CPU denoise unless the user specifically wants to try GPU.
Resumable: if `<target>_starless.fit`/`_stars.fit` already exist under
`final/_intermediate/`, star removal is skipped and reused.

## Known gaps, honestly

- `run_stage.py` prints the same log lines twice in a raw terminal capture
  (once live via `run_lrgb`'s internal `print()`, once again under
  `=== NOTES ===` since that's `result.notes` printed back) -- cosmetic,
  not a bug; treat `=== NOTES ===` as the authoritative transcript.
- This skill has been walked through against three real project folders:
  M51 (both telescopes, both binnings, a Luminance override round-trip
  that succeeded, and one that raised `ReprojectionError` -- see above),
  NGC 3628 (T73, `CalibrationMode.PRECALIBRATED` -- confirmed the
  "Precalibrated" interview line and its calibration-gap filtering both
  work end to end on a real no-local-dark delivery), and Abell 6 and HFG1
  (T02, RGB-only + OSC -- confirmed checkpoints `02`/`04`/`05_rgb_final`
  all fire correctly with no `01`/`03`, and the `_rgb.tif` export naming).
  It has not been exercised against a project with zero colour
  contributors at all, an unknown telescope (`UnknownInstrumentError`), a
  genuinely `[BLOCKED]` calibration gap, or the mixed-OSC-and-mono-RGB
  `NotImplementedError` case -- those paths exist in the underlying
  pipeline but weren't hit on real data yet. If one comes up, relay
  `run_lrgb`'s actual exception message rather than guessing what it
  means.
- **This machine's ~8GB RAM can OOM-kill a real Siril `register`/`stack`
  call outright** on a handful of large (6000x4000+) frames -- confirmed
  real on the Abell 6/HFG1 run (7 frames, killed mid-`stack`, no partial/
  corrupt output). `run_lrgb`'s own resumability already covers this:
  the staged/debayered lights survive the kill, so simply re-running the
  identical `run_stage.py` command resumes from `register+stack` rather
  than restaging from scratch. If a real run dies this way, say so
  plainly and just re-run the same command -- don't treat it as a code
  bug to fix first.
- **Forcing a Luminance-tier rebuild is not byte-reproducible**, even
  reverting to the exact same parameters afterward: reverting the M51
  override back to `T24-bin1` during this slice's validation re-ran
  GraXpert's AI background extraction on L from scratch, and the
  resulting `lrgb_final.fit`/TIFF were NOT byte-identical to the
  pre-override files (different crop shape, different pixel content) --
  GraXpert's own run-to-run variance, not something this skill or
  `run_lrgb` controls. This matches plan-rev4.md's own stated position
  that byte-identical output is "provably unachievable" as a
  verification criterion for anything that forces a real Siril/GraXpert
  re-run; it is NOT a sign that an Adjust round-trip corrupted anything --
  the rebuilt output was independently checked (checkpoint stats,
  `usable()`'s own NaN/read-back gate) and is a legitimate, correctly
  re-derived result, just not byte-for-byte the same as before. Only a
  RESUMED call (nothing forced, nothing changed) is byte-identical --
  that path is what `tests/test_pipeline.py`'s
  `test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`
  actually verifies.
- **GPU denoise (`--denoise-gpu`) is unreliable on Intel-integrated-GPU
  laptops**: confirmed real crash investigated end-to-end (event logs,
  driver dumps, retried after a full reboot) -- root cause is a
  Haswell-era Intel GPU's DirectML incompatibility (the INTEL-SA-00315
  security fix disabled DX12 on this hardware class), not an OOM and not
  fixable by a driver upgrade. Default to CPU denoise; only try
  `--denoise-gpu` if the user has a genuinely modern discrete GPU.
- **CPU denoise at full resolution (4096x4096) needs real time and real
  RAM headroom** -- confirmed to complete successfully but takes 1-2+
  hours per image on an 8GB machine. Run it as a detached background
  process, not a foreground blocking call, and monitor by polling the
  PID rather than assuming a long silence means it died.
- `run_narrowband.py`/`run_narrowband_boost.py` have real-data-tested unit
  coverage and a manual, hand-run validation against independently-built
  masters (M42/T20), but `run_narrowband_boost.py` has NOT yet been run
  end-to-end as a script against one complete real LRGB project start to
  finish -- if it's used that way for the first time, treat the run as a
  fresh validation, not a known-good path.
- **A real bad-data case, not a code bug**: M42's Green-bin2 registration
  can fail with Siril's "Found 0 stars in reference" if the frame set
  contains near-zero-signal frames (cloud/focus/tracking dropouts) --
  diagnose by checking each frame's pixel std via astropy directly, not by
  assuming the code is at fault; the fix is excluding the bad frame(s),
  not touching the pipeline.
