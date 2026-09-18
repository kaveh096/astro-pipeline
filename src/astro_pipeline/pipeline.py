"""End-to-end LRGB orchestration for a single telescope+target, combining
every user who contributed data on that telescope.

Deliberately resumable at every step: each stage checks whether its output
already exists and skips if so. This machine has ~8GB RAM and long runs
have been killed mid-flight more than once, so a re-run must continue
rather than start over. It also means a stage can be deleted from
`_pipeline/` to force just that stage to recompute.

Stage order here reflects what was established empirically (see the
individual modules' docstrings), not the original design sketch:

    per (filter, binning): calibrate -> register+stack -> plate solve
    per colour contributor: align R/G/B -> crop -> rgbcomp -> GraXpert -> SPCC
    luminance:              GraXpert
    combine:                reproject each contributor onto L's grid,
                             average them together -> GHT both ->
                             rgbcomp -lum
    export:                 16-bit TIFF + faithful PNG preview

Colour processing happens at native (binned) resolution per contributor
and is reprojected up to L's grid only at the end -- reprojection
introduces NaN edges and interpolation artifacts that break Siril's star
photometry, so anything photometric (SPCC) must run before it.

Colour calibration is SPCC only. An earlier version supported PCC as well
(Siril's broadband method), selectable via a `colour_calibration`
parameter -- removed once SPCC was confirmed working (needs Siril >=
1.4.4) and preferred in every respect: no runtime dependency on VizieR,
and better colour, modelled from the actual sensor/filter spectral
response rather than inferred from broadband photometry. See
docs/colour-calibration-catalogues.md for the PCC-vs-SPCC comparison run
on real M51 data before PCC was dropped.

MULTI-USER / MULTI-CONTRIBUTOR COMBINING, added once real data showed two
users (collaborators sharing an iTelescope account arrangement) had
imaged the same target on the same telescope:

  Luminance combines at the RAW-SUB level within one (telescope, binning).
  Two users sharing a telescope and binning for the same filter (e.g. both
  shooting Luminance/BIN1) get their raw lights merged into ONE
  calibrate+register+stack call, via IngestReport.instrument_groups().
  This gives Siril's sigma-rejection visibility into every individual
  frame -- better outlier/satellite-trail rejection than averaging two
  independently-stacked masters would be. calibration_index() has no user
  dimension, so the shared bias/dark for that telescope+binning already
  applies correctly to a merged light list. A merge across DIFFERING
  exposure times (T21's real case: 300s/600s Luminance lights, single
  user) calibrates fine too -- calibration.select_dark() picks one dark
  master at the group's own binning and calibrate_lights() passes
  `-opt=exp`, which Siril applies as a PER-IMAGE scaling coefficient
  (verified against the real installed 1.4.4 binary), so a mixed-exptime
  merge needs no per-exptime bucketing. What still never happens is
  merging Luminance ACROSS telescopes -- T21 has a genuinely different
  pixel scale from T24, so its Luminance can never raw-sub-combine with
  T24's regardless of exptime; each telescope's Luminance gets its own
  master-level contributor instead (see the discovery loop in run_lrgb --
  every discovered telescope's worth of Luminance masters get built there).
  Which one actually drives the rendered composite is a MEASURED
  RECOMMENDATION, not an automatic rule (Slice 2): each contributor's
  median stellar FWHM is read off Siril's own per-frame registration data
  (`r_pp_lights_.seq`'s `R0` lines, see contributor_fwhm_arcsec()) and
  converted to arcsec against its own plate-solved master's pixel scale,
  the sharpest one wins by default, and every other Luminance master still
  gets built (so it's on disk and inspectable) but excluded from the
  composite -- see `lum_source` on run_lrgb for the explicit override.

  RGB combines at the MASTER level, across "contributors" -- one
  contributor per (telescope, binning) that has all three R/G/B present.
  Two different binnings (the user's own BIN2 RGB vs a collaborator's BIN1
  RGB, both on T24) cannot be combined at the raw-sub level at all
  (different pixel scale/dimensions -- Siril's registration requires
  matching frames). Each contributor is independently aligned across its
  own R/G/B, background-extracted, and colour-calibrated (mirroring the
  single-contributor pipeline exactly), THEN reprojected onto the
  Luminance grid, THEN gain/offset-matched onto a reference contributor's
  flux scale (see reconciliation.match_gain_offset/fit_gain -- necessary
  because different-binning contributors sit at genuinely different flux
  scales, measured ~5x between BIN1/BIN2 on real M51 data), THEN averaged
  together (reconciliation.combine_same_grid, weighted by STACKCNT -- subs
  that actually survived quality filtering/rejection into each
  contributor's stacked masters, not raw sub count) into one final RGB.
  This is exactly the pattern reconciliation.py's own module docstring
  anticipated: "each instrument/user's data is independently calibrated,
  registered, and stacked first, and only the resulting masters get
  reprojected together."

  `rgb_binning` names the PRIMARY contributor (kept backward compatible
  with existing fixtures/tests: its files stay at the legacy top-level
  paths, e.g. final/rgb_native.fit). Any OTHER binning discovered for the
  same telescope+target+RGB-filters becomes an additional contributor
  under final/contrib_<telescope>_bin<n>/ (Slice 3: telescope-explicit,
  not just contrib_bin<n> -- a second telescope shooting the same
  non-primary binning would otherwise collide), and only actually runs if
  all three R/G/B are present for it -- a partial contributor (missing one
  channel) is logged and skipped (Slice 3: was a RuntimeError that
  aborted the whole run; see _build_colour_contributor), not silently
  built into an incomplete composite.
"""

from __future__ import annotations

from .calibration_policy import infer_calibration_mode, infer_flat_policy
from .lrgb_orchestrator import run_lrgb
from .master_builder import build_single_filter_master
from .narrowband_filters import (
    NARROWBAND_PALETTES,
    _NarrowbandNormalizingReport,
    equalize_narrowband_channels,
    normalize_narrowband_filter_name,
)
from .narrowband_orchestrator import run_narrowband
