# Calibration frames: when to use bias, darks, and how many is enough

Practical notes for choosing calibration inputs per telescope. Written after
tracing visible vertical banding in an M51/T24 render back to its cause.

## Background: why iTelescope data bands

iTelescope's sensors are not regularly replaced, so they accumulate dead
rows/columns of pixels. Dithering (small pointing offsets between subs) is
the standard defence: the defect stays fixed on the sensor while the sky
moves, so after registration the bad pixels land in different places in
each frame and stacking rejection can throw them out.

That only works if there are enough light frames for rejection to have
something to work with. With few subs the defects survive stacking and show
up as vertical banding in the final image. This matches what was seen
processing the same target manually with other tools previously.

## Rule 1: don't subtract bias from lights when you have matching darks

A dark of matching exposure and temperature **already contains the bias
signal**. Subtracting a separately-stacked master bias as well is redundant,
and it actively injects that master's own read noise and fixed-column
pattern into every light.

Siril's own bundled scripts follow this: `-bias` is passed when calibrating
**flats** (which are too short to use a dark), never when calibrating lights
against a dark.

Measured on the real T24 M51 data — column-to-column scatter normalised by
each frame's own pixel noise, where ~1.0 would mean no fixed-column
structure at all:

| frame | ratio |
|---|---|
| master bias | **48.0** |
| master dark | 1.5 |
| raw light | 2.0 |
| light calibrated with bias + dark | **3.8** |
| light calibrated with dark only | **3.3** |

Calibration nearly doubled the banding, and dropping the redundant bias
subtraction recovered part of it.

This is now the default: `calibrate_lights(..., subtract_bias=False)`. The
master bias is still built, because it is the correct reference for
calibrating flats.

## Rule 2: few calibration frames inject more noise than they remove

A master built from N frames retains roughly `1/sqrt(N)` of a single
frame's noise, and that noise is stamped identically onto every light:

| frames | residual noise in master |
|---|---|
| 5 | 45% |
| 10 | 32% |
| 20 | 22% |
| 50 | 14% |

At 5 frames the master is doing nearly as much harm as good. Note that even
dark-only calibration (3.3) measured *worse* than the raw frame (2.0) on the
T24 data — with 5 frames there is no way to win, it is a data limitation
rather than something the pipeline can fix.

Rough guidance:

- **20+ frames** — use normally.
- **10-20** — usable; expect some injected noise.
- **< 10** — the dark is still worth using for hot pixels and dark current,
  but expect residual fixed-pattern noise. Do not also subtract bias.
- **Flats** are a separate case: always worth using if available, since they
  correct a multiplicative error nothing else addresses.

## What we actually have, per telescope

| telescope | bias | darks | flats |
|---|---|---|---|
| T24 | 5 per binning | 5 per binning (300s) | **none** — not offered for this telescope/delivery |
| T21 | 82 per binning | 25 per binning (900s) | 330 raw sky flats + pre-built masters |
| T20 (M42) | 50 (BIN1) / 49 (BIN2, +1 truncated) | 10 (BIN1, 180s) + 10 (BIN2, 180s) + 10 (BIN2, 300s) | 70 raw (10 each L/R/G/B/Ha/SII, 9+1 truncated OIII) — **~21 months older than the lights** (real dust/vignetting-drift risk) |
| T68 (IC 1396) | 48 (BIN1) | 50 (BIN1, 240s) | 88 raw, all Color/BIN1 — real `DATE-OBS=1970` (camera clock unset) on the calibration frames |
| T05 (M31) | 15 (BIN1) | 16 (BIN1, 180s, **−15°C** — real, unfixed temperature mismatch vs the lights' −10°C) | 120 raw (40 each R/G/B) — the freshest of the three, 19 days old |

None of T20/T68/T05's calibration frames are recognised by FILENAME —
no telescope token anywhere in their path. Opt-in, header-based recognition
(`--calibration-header-fallback`) makes these real counts visible in
`calibration_index()`/`flat_index()`, but deliberately does NOT flip any of
these three telescopes off `CalibrationMode.PRECALIBRATED` by default, and
does NOT reprocess any of their real delivered images — recognition and
reprocessing are two separate decisions, and only the first has shipped.
The per-target local-flats-vs-iTelescope measurement (whether reprocessing
any of these three with local flats would actually help) is a deliberate
next step, not yet done: build RAW_LOCAL and PRECALIBRATED masters for the
same data and compare masked background uniformity / dust-residual
metrics, per target rather than one shared verdict, since the age/quality
risk above differs enough between T20/T68/T05 that a blind "reprocess all
three" call isn't warranted.

So T24 is the worst case on every axis and T21 calibrates visibly cleaner.
That gap is worth measuring on any newly-added multi-telescope target
rather than assumed.

## What was tried and rejected

- **Siril `fixbanding`** (`fixbanding 1 3 -vertical`): moved the metric from
  3.24 to 3.21, i.e. no meaningful effect. Not used.
- **Blaming the missing flats**: the original hypothesis, and wrong. Flats
  correct multiplicative sensitivity variation; measuring showed the banding
  was *added* at the calibration step, not left uncorrected by it.

## Worth revisiting

- More aggressive stacking rejection for datasets with known column defects
  — with dithering, defects should be rejectable, and 13 subs with
  winsorized sigma 3/3 evidently is not enough to fully remove them.
- Whether T21's much larger calibration sets measurably reduce banding
  compared to T24's, on a real side-by-side render.
