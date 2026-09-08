---
name: astro-pipeline-lrgb
description: Use when Kaveh wants to run, drive, resume, or continue the LRGB astrophotography pipeline in this repo against an iTelescope session folder -- phrases like "run the pipeline", "process <target>", "run the M51 project", "pick up where the pipeline left off", "build the masters for <target>", or any request to turn a folder of raw iTelescope FITS subs into a stretched, colour-calibrated LRGB TIFF. Drives astro_pipeline.pipeline.run_lrgb stage-by-stage (masters -> reconciled -> final), pausing at each checkpoint for a human Proceed/Adjust/Abort decision rather than running to completion unattended. Does not do framing, cropping, or colour-grading -- those stay Kaveh's manual Photoshop step.
version: 1.0.0
---

# Astro Pipeline: LRGB run wrapper

Interviews Kaveh about a project folder, then drives `run_lrgb()`
(`src/astro_pipeline/pipeline.py`) one stage at a time via
`skill/run_stage.py`, stopping at each of the three real control-flow
boundaries the pipeline exposes (`stop_after="masters"` /
`"reconciled"` / `"final"`) to show what actually happened and let a human
decide whether to proceed, adjust a parameter and re-run, or stop.

This wraps an already-complete, already-resumable pipeline (Slices 1-3
built the science; Slice 4.1-4.3 made staged execution and resume safety
real). This skill adds no new pipeline behaviour of its own -- it calls
`run_lrgb`, reads back `result.notes` / `result.checkpoints`, and presents
them. If something here disagrees with what `run_lrgb` actually does,
trust the code in `src/astro_pipeline/pipeline.py`, not this file.

**Scope reminder (do not exceed this)**: no flats (deferred, a separate
pass), no automatic override of which telescope's Luminance drives the
composite (Slice 2's whole point is that this stays a human call -- surface
the numbers, never silently pick against them), no cross-telescope
Luminance blending (Kaveh's colour-only-rule decision is binding), and no
touching Photoshop -- this skill's job ends the moment a TIFF path exists.

## Repo conventions to reuse, not reinvent

- Python venv: `.venv/Scripts/python` (Windows). Run every command below
  through it, e.g. `.venv/Scripts/python.exe skill/interview.py ...`.
- `scripts/run_m51.py` is a hardcoded reference for the real calling
  convention (`telescope="T24"`, `target="M51"`, explicit RA/Dec in hours/
  degrees -- passed explicitly so plate solving needs no network name
  resolution). Use it as a sanity check for argument shapes, not as
  something to invoke directly for a real interactive run.
- `skill/interview.py` and `skill/run_stage.py` (this skill's own
  supporting scripts) do the actual work below; read their docstrings if
  anything here is ambiguous.

## Step 1 -- Interview

1. **Project folder.** Ask which project folder to run against. If Kaveh
   doesn't name one, offer the one real fixture as a default --
   `C:\Users\Kaveh\Desktop\M51 - Whirlpool galaxy - T24 & T21 - Jan 2025`
   -- but make clear it's a default, not the only option: this has to
   generalize to whatever folder he points it at next.

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
     Kaveh the deduplicated list, not the raw one. If you want to show the
     raw-vs-deduplicated contrast for transparency, that's fine too, but
     the deduplicated list is what should drive the conversation.
   - a **dark-scaling preview**: for any group whose exact-exptime dark is
     missing, whether `calibration.select_dark()` will actually resolve
     it by scaling a longer/shorter dark via `-opt=exp` (safe direction),
     or whether it's a genuine `[BLOCKED]` gap `run_lrgb` will raise
     `CalibrationFramesMissingError` on. Surface `[BLOCKED]` entries
     prominently -- those are real stoppers, not cosmetic warnings.

3. **Present the summary** to Kaveh: telescopes/targets/binnings/users
   found, the deduplicated calibration gaps, and the dark-scaling notes.
   If there's a `[BLOCKED]` entry, say so plainly and ask whether to
   proceed anyway (it will fail loudly inside `run_lrgb` when it gets
   there) or stop here.

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
   - `stretch_method` -- ask Kaveh's aggressiveness preference in plain
     language and map it to one of the three real values
     `stretch_compose.stretch_and_compose` supports:
     - `"autostretch"` (default) -- shadow-clipped histogram stretch;
       the safe, always-viewable choice.
     - `"autoghs"` -- lifts more faint signal, background stays grey (no
       black point), noisier.
     - `"autoghs+auto"` -- autoghs then autostretch on top: most faint
       detail recovered, most noise. Only offer this if Kaveh explicitly
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

Relay from `=== NOTES ===`:
- the designated gain/offset reference (`highest STACKCNT wins`);
- each non-reference contributor's fitted gain and background numbers
  (`gain=... on N high-signal px`);
- the combine weights actually used.

Look at the `03_lum_background_extracted` and `04_rgb_reconciled`
checkpoint previews.

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
  Say this out loud to Kaveh before running it (it will rebuild the
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

Relay the `05_lrgb_final` checkpoint (this one renders FAITHFULLY, not
autostretched for display -- if it looks too dark or too flat, that is
real information about the chosen `stretch_method`, not a preview
artifact) and look at its preview PNG.

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
  named `<target>_lrgb.tif`, e.g. `M51_lrgb.tif`, not a hardcoded stem);
- the faithful preview PNG path, for a quick look without opening
  Photoshop;
- clipped-low/clipped-high fractions from the export, if either is
  non-trivial (worth a mention -- it's a real signal about the stretch).

Then stop. **Do not open Photoshop, do not attempt any framing, cropping,
or colour-grading** -- that is deliberately Kaveh's manual creative step,
not something this skill automates. The skill's job ends at "here's your
TIFF."

## Abort, at any checkpoint

Just stop. `run_lrgb`'s own resumability (Slice 4.1-4.3: `usable()` +
run-signature tracking) is exactly what makes this safe -- whatever is on
disk in `_pipeline/` is left as-is, and a later re-run of this same skill
picks up from wherever it actually got to, without redoing completed
work. No separate cleanup step exists or is needed.

## Known gaps, honestly

- `run_stage.py` prints the same log lines twice in a raw terminal capture
  (once live via `run_lrgb`'s internal `print()`, once again under
  `=== NOTES ===` since that's `result.notes` printed back) -- cosmetic,
  not a bug; treat `=== NOTES ===` as the authoritative transcript.
- This skill has only been walked through against the real M51 project
  folder (both telescopes, both binnings, a Luminance override round-trip
  that succeeded, and one that raised `ReprojectionError` -- see above).
  It has not been exercised against a project with zero colour
  contributors, an unknown telescope (`UnknownInstrumentError`), or a
  genuinely `[BLOCKED]` calibration gap -- those paths exist in the
  underlying pipeline (Slice 3.1/3.2, `calibration.select_dark`) but
  weren't hit on real data this session. If one comes up, relay
  `run_lrgb`'s actual exception message rather than guessing what it
  means.
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
