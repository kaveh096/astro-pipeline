# Calibration frames: when to use bias, darks, and how many is enough

Practical notes for choosing calibration inputs per telescope. Written after
tracing visible vertical banding in an M51/T24 render back to its cause.
Revisit when the multi-instrument path is built.

## Background: why iTelescope data bands

iTelescope's sensors are not regularly replaced, so they accumulate dead
rows/columns of pixels. Dithering (small pointing offsets between subs) is
the standard defence: the defect stays fixed on the sensor while the sky
moves, so after registration the bad pixels land in different places in
each frame and stacking rejection can throw them out.

That only works if there are enough light frames for rejection to have
something to work with. With few subs the defects survive stacking and show
up as vertical banding in the final image. This matches what Kaveh saw
processing the same target manually last year.

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
| T24 | 5 per binning | 5 per binning (300s) | **none** — never downloadable, and the subscription has lapsed |
| T21 | 82 per binning | 25 per binning (900s) | 330 raw sky flats + pre-built masters |

So T24 is the worst case on every axis and T21 should calibrate visibly
cleaner. When the multi-instrument path lands, that difference is worth
measuring rather than assuming.

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
- Whether T21's much larger calibration sets measurably reduce banding, once
  that data is processed.
