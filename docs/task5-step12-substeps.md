# Task 5, Step 12: `lrgb_orchestrator.py` extraction — sub-step plan (12a-12d)

Status: planning/research only, 2026-09-17. No source changed to produce this
document. Line numbers below are against the CURRENT `src/astro_pipeline/pipeline.py`
(1201 lines total, after Steps 0-11 shrank it from the original 2459-line
file `docs/task5-oop-refactor-plan.md` was written against) — every line
number in the parent plan document that predates Step 11 (e.g. "line 1966",
"line 2190", "line 2212", "~825 lines moving") is stale and must not be
reused; this document re-derives everything from the file as it exists now.
`run_lrgb` currently spans lines 195-1038 (844 lines including its
docstring).

This document independently re-derives the `self.` state inventory for
`LRGBOrchestrator` per the parent plan's Open Question 3 instruction ("don't
trust any single version of that list without re-deriving it from the real
checkpoint-chain code yourself") and drafts the concrete 12a-12d execution
plan the parent plan recommends but doesn't itself write out.

---

## A. Re-derived `self.` state inventory

Phase boundaries used below match `STAGE_ORDER = ("masters", "reconciled",
"final")` and the real control flow:

- **masters** body: lines 297-818 (`project_dir = Path(project_dir)` through
  `if stop_after == "masters": return result`)
- **reconciled** body: lines 820-977 (`if not is_rgb_only:` L-background
  block through `if stop_after == "reconciled": return result`)
- **final** body: lines 979-1038 (stretch/compose through export)

### A.1 Constructor parameters (14) — trivially `self.*`, since `run()` is called after `__init__` returns

`project_dir, telescope, target, ra_hours, dec_deg, lum_binning, rgb_binning,
stretch_method, lum_source, pedestal, stop_after, force, flat_policy,
calibration_mode` — these are `run_lrgb`'s own 14 parameters. All become
`self.` attributes set in `__init__` as a matter of course. Most (`telescope,
ra_hours, dec_deg, lum_binning, rgb_binning, lum_source, flat_policy,
calibration_mode`) are read ONLY inside the masters body (never again after
line 818) — they don't need to "survive" as state in any interesting sense,
they're just constructor-set like any other `self.` attribute. Four are
genuinely read late and worth calling out specifically:

- **`target`** — read again at line 1029 (`export(..., stem=f"{target}_rgb"
  if is_rgb_only else f"{target}_lrgb")`), inside `_finalize()`. Matches
  round-1's finding.
- **`stretch_method`** — read again at lines 990/991/997/999, inside
  `_finalize()` (also read once at line 727 inside masters, for
  `RunSignature` construction). Matches round-1's finding.
- **`stop_after`** — read at line 812 (masters early-return check) AND line
  971 (reconciled early-return check). **Not explicitly named in either
  round's list** (it's a constructor param so it was presumably assumed
  "obviously self", but it genuinely crosses the masters/reconciled boundary
  and the two read sites must both work against `self.stop_after`, so it
  belongs on the punch list explicitly). See Section E for why the
  early-return checks themselves should live in `run()`, not inside
  `_build_masters()`/`_reconcile()` — which changes how `stop_after` is used
  but not the fact that it must be readable from both places.
- **`force`** — read at lines 292-295 (validation) and 331/758-759 (masters
  body only: `force_masters`, `stages_to_invalidate` cascade). Does **not**
  cross past masters — included here only because it's a constructor
  parameter, not because it needs to survive into `_reconcile()`/`_finalize()`.

### A.2 Computed attributes that actually cross a phase boundary (13)

| # | Name | First assigned | Read at | Crosses | Status |
|---|---|---|---|---|---|
| 1 | `notes` | 302 (`= result.notes`) | throughout all 3 phases (every `_log` call) | masters→reconciled→final | Confirmed (orig list). Note it's literally `result.notes` (same list object) — could be dropped in favor of always writing `self.result.notes`, but keeping the alias matches the original code's own convention and every `_log(msg, notes)` call site unchanged. |
| 2 | `result` | 301 | throughout all 3 phases | masters→reconciled→final | Confirmed (orig list). |
| 3 | `checkpoint_dir` | 304 | 793, 804 (masters); 834, 963 (reconciled); 1006 (final) | masters→reconciled→final | Confirmed (orig list). |
| 4 | `checkpoints_path` | 305 | 799, 810 (masters); 840, 969 (reconciled); 1013 (final) | masters→reconciled→final | Confirmed (orig list). |
| 5 | `is_rgb_only` | 446 | 447,453,791,812,814-816 (masters); 821,824,855,862,868,974 (reconciled); 984,987,1003,1029 (final) | masters→reconciled→final | Confirmed (orig list). |
| 6 | `final` (Path, the output dir — NOT the "final" stage name) | 299 | 300,571,657,777-785,801,825 (masters); 825,853,883,908,909,933 (reconciled); 984,985,988,989,993,994,1028 (final) | masters→reconciled→final | Confirmed (round-1 addition). Heaviest cross-phase user of all — read in every single phase. **[Round-1 review correction: line 415 dropped]** — 415 recomputes its own path via `pipeline_dir(project_dir)` independently rather than reading `final`. **[Round-3 review correction: 848/850/916 dropped]** — those lines read `lum_for_compose_path` (row 12), not `final` itself. Neither correction changes the cross-phase conclusion, which holds on the remaining citations. |
| 7 | `previous` | 789 (`= None`) | 794,797,804-808 (masters); 834,838,964,967 (reconciled); 1008 (final) | masters→reconciled→final | Confirmed (round-1 addition). **[Round-1 review correction: added 804-808]** — a second real read/write cycle at checkpoint 02, missed in the first pass. **[Round-3 review correction: 793→794]** — off-by-one on the read-site line number. |
| 8 | `previous_linear` | 790 | 793,797,804-808 (masters); 834,838,964,967 (reconciled); 1008,1011 (final) | masters→reconciled→final | Confirmed (round-1 addition). **[Round-1 review correction: added 804-808]**, same as `previous`. |
| 9 | `contributors` | 566 (`= []`, appended through 692) | 606,624,692,694,696-698,706-753 (masters); 849,862,864,865,869,870,876,908,913,917,929,950,952,955 (reconciled) | masters→reconciled (NOT read in final) | Confirmed (orig list); confirmed NOT needed in `_finalize()`. |
| 10 | `reference` | 707 | 710,745 (masters); 937 (reconciled, log line only) | masters→reconciled (NOT read in final) | Confirmed (orig list). |
| 11 | `reference_pos` | 706 | 706-707 (masters, self-reference for `reference`) | 930,934,952 (reconciled) | masters→reconciled (NOT read in final) | Confirmed (round-2 addition). |
| 12 | `lum_for_compose_path` | **848/850 — inside the reconciled body, NOT masters** | 916 (reconciled); **995 (final)** | reconciled→final | Confirmed real, but **boundary corrected**: the orig list implies masters-provenance; it is actually first assigned inside `_reconcile()` itself (guarded by `if not is_rgb_only:`), then read once more later in the same `_reconcile()` call (916) and again in `_finalize()` (995). So this is a reconciled→final crossing, not masters→reconciled. |
| 13 | **`rgb_reconciled`** | 853 | 854,855,862,869,870,872,884,908,909,916(no — via `lum_bg`),952,960 (reconciled); **989, 996 (final — both branches of the composite-name `if`)** | reconciled→final | **NEW — not present in either round's list.** `rgb_reconciled = final / "rgb_reconciled.fit"` is set at the top of the reconciled body and is read again in `_finalize()` at both line 989 (`is_rgb_only` branch: `shutil.copy2(rgb_reconciled, rgb_in)`) and line 996 (non-`is_rgb_only` branch: same copy). This is the exact same shape of gap as `lum_for_compose_path` (#12) and was missed by both adversarial rounds. It is technically re-derivable as `self.final / "rgb_reconciled.fit"` (deterministic, like several masters-internal `Path`s already are) rather than stored separately, but Risk 5's own rule ("every literal filename string... copied verbatim") argues for storing it explicitly as `self.rgb_reconciled` so the string `"rgb_reconciled.fit"` exists in exactly one place in the new file, matching how `lum_for_compose_path` is already handled. |

### A.3 Attributes named in the original Section-1 list that do **not** actually cross a boundary — correction

Re-derivation shows four names from the parent plan's Section 1 "these three
phases share a large amount of mutable state" list are **fully confined to
the masters body** (never read at or after line 820):

- **`report`** — set line 310, last read line 694 (masters only).
- **`run_signature_path`** — set line 306, read lines 307 and 786 (masters only).
- **`old_signature`** — set line 307, read through line 756 (masters only).
- **`new_signature`** — set line 726, read lines 756/786 (masters only).

None of these need `self.` status to satisfy a `_build_masters()` /
`_reconcile()` / `_finalize()` split **as long as `_build_masters()` stays
one method**. They only become genuinely cross-boundary state if Open
Question 3's alternative design (`_build_luminance()` +
`_build_colour_contributors()` + a separate `_build_run_signature()` step)
is adopted instead — flagging this explicitly rather than silently carrying
them forward as "confirmed" cross-phase state the way the original list
implied. **Recommendation:** keep them as plain locals inside
`_build_masters()` for 12b (simpler, and correct for the single-method
design this plan adopts — see Section E); only promote them to `self.` if a
future step actually splits `_build_masters()` further.

Also confined to a single phase (masters-internal only, never crossing at
all — not on either round's list, not promoted here): `cal_index`,
`lum_candidates`, `rgb_binnings`, `osc_binnings`, `profile_tuple`,
`colour_calibration_mode`, `colour_flat_policy`, `selected_key`,
`selected_path`, `selected_fwhm`, `force_masters`, `lum_stackcnt`,
`lum_frame_hashes`, `lum_flat_frame_hashes`, `lum_calibration_modes`,
`colour_frame_hashes`, `colour_flat_frame_hashes`, `stages_to_invalidate`,
`signature_stages`, `primary_calibrated` (masters); `lum_bg`, `cropped`,
`cropped_rgb`, `gain_matched`, `weights`, `reprojected_paths`, `out_path`,
`matched_path`, `fit_results`, `recon` (reconciled); `composite`,
`composite_name`, `rgb_in`, `lum_in`, `compose` (final). These stay plain
locals inside whichever method they're born in.

### A.4 Net count

- 14 constructor-parameter attributes (Section A.1).
- 13 computed cross-phase attributes (Section A.2): 12 confirmed from the
  2-round list (with `lum_for_compose_path`'s boundary corrected) + 1 new
  (`rgb_reconciled`).
- **Total: 27 `self.` attributes** for `LRGBOrchestrator`, plus explicit
  correction that 4 previously-listed names (`report`, `run_signature_path`,
  `old_signature`, `new_signature`) should NOT be promoted to `self.` under
  the single-method `_build_masters()` design.

---

## B. Literal on-disk strings that must survive byte-for-byte (Risk 5 / 5b)

### B.1 Checkpoint labels (verbatim), in `run_lrgb`

- `f"01_master_{LUMINANCE_FILTER.lower()}"` → literal `"01_master_luminance"` (line 793)
- `"02_primary_rgb_colour_calibrated"` (line 804)
- `"03_lum_background_extracted"` (line 834)
- `"04_rgb_reconciled"` (line 963)
- `"05_rgb_final"` / `"05_lrgb_final"` (line 1007, chosen by `is_rgb_only`)

(`run_narrowband`'s own labels — `"02_narrowband_colour_calibrated"`,
`"03_narrowband_equalized"`, `f"04_{palette}_final"` — are out of scope for
Step 12; they stay in `pipeline.py` until Step 13.)

### B.2 On-disk filenames (verbatim), in `run_lrgb`

`"final"` (output subdir), `"checkpoints"` (subdir), `"checkpoints.json"`,
`"run_signature.json"`, `f"master_{LUMINANCE_FILTER.lower()}.fit"` →
`"master_luminance.fit"` (line 415), `"lum.fit"` (827), `"lum_bg.fits"`
(output_stem `"lum_bg"` at 829, path at 825/777), `"lum_bg_cropped.fits"`
(850), `"rgb_reconciled.fit"` (853/778/782), `f"rgb_reconciled_contrib_{contributor.key}.fit"`
(883), `"combined_crop"` (subdir, 909/933), `f"gain_matched_{contributor.key}.fit"`
(933), `"lrgb_final.fit"` (779/783/785), `"rgb_colour_calibrated.fit"` (801 —
the PRIMARY contributor's top-level copy, distinct from each contributor's
own `contrib_dir/rgb_colour_calibrated.fit`), `f"{composite_name}.fit"` →
`"rgb_final.fit"` / `"lrgb_final.fit"` (985), `"rgb_for_compose.fit"`
(988/994), `"lum_for_compose.fit"` (993), export stem `f"{target}_rgb"` /
`f"{target}_lrgb"` (1029).

Every one of these must grep-match the same count before/after each of
12a-12d, per the parent plan's own Risk 5/5b mitigation.

---

## C. Monkeypatch retargeting inventory (Risk 1)

All sites below do `monkeypatch.setattr(pipeline_module, "<name>", ...)`
against a name that `run_lrgb` (not `run_narrowband`) calls. Every one of
these must retarget to `lrgb_orchestrator_module` at **Step 12a** (the
moment the function's body — and its imports — move to the new file), not
deferred to 12b-12d. 12b-12d themselves require **zero further monkeypatch
changes** (no additional module boundary crossed after 12a).

### C.1 `tests/test_pipeline.py` — 24 sites

| Line | Target name | Test function |
|---|---|---|
| 1482 | `scan_session` | `test_run_lrgb_rgb_only_full_run_no_luminance_no_crash` |
| 1570 | `scan_session` | `test_run_lrgb_rgb_only_multi_contributor_raises_not_implemented` |
| 1632 | `scan_session` | `test_run_lrgb_mixed_osc_and_rgb_same_binning_raises_not_implemented` |
| 1702 | `scan_session` | `test_run_lrgb_stop_after_masters_returns_before_reconciliation_MOCKED` |
| 1715 | `build_group_master` | (same test) |
| 1736 | `run_graxpert_background_extraction` | (same test) |
| 1797 | `scan_session` | `test_run_lrgb_stop_after_reconciled_then_final_does_not_rebuild_masters_MOCKED` |
| 1812 | `build_group_master` | (same test) |
| 1839 | `run_graxpert_background_extraction` | (same test) |
| 1850 | `reproject_to_reference` | (same test) |
| 1860 | `stretch_and_compose` | (same test) |
| 1945 | `scan_session` | `test_run_lrgb_multi_contributor_reconciliation_end_to_end_MOCKED` |
| 1955 | `build_group_master` | (same test) |
| 1987 | `run_graxpert_background_extraction` | (same test) |
| 1999 | `reproject_to_reference` | (same test) |
| 2011 | `crop_to_common_coverage` | (same test) |
| 2020 | `match_gain_offset` | (same test) |
| 2031 | `combine_same_grid` | (same test) |
| 2112 | `scan_session` | `test_run_lrgb_force_cascade_deletes_expected_top_level_files_MOCKED` |
| 2127 | `build_group_master` | (same test) |
| 2153 | `run_graxpert_background_extraction` | (same test) |
| 2163 | `reproject_to_reference` | (same test) |
| 2175 | `stretch_and_compose` | (same test) |
| 2185 | `_delete_if_exists` | (same test) |

(Note: `test_run_lrgb_rgb_only_full_run_no_luminance_no_crash`,
`..._multi_contributor_raises_not_implemented`, and
`..._mixed_osc_and_rgb_same_binning_raises_not_implemented` also do
`monkeypatch.setattr(ColourContributorBuilder, "build_rgb", fake_build_rgb)`
— that target is the **class object** in `colour_contributor.py`, not an
attribute of `pipeline_module`, so it needs no retargeting regardless of
where `run_lrgb` lives.)

### C.2 `tests/test_run_signature.py` — 4 sites

| Line | Target name | Test |
|---|---|---|
| 436 | `scan_session` | the Luminance-loop `flat_frame_hash` regression test (asserts `contributor_stale`'s 5th arg is a real hash, not `""`) |
| 443 | `build_group_master` | (same test) |
| 650 | `scan_session` | the colour-loop `flat_frame_hash` regression test |
| 658 | `build_group_master` | (same test) |

Both of these call `pipeline_module.run_lrgb(...)` directly (not via a
fixture) — this is exactly the Risk-3 safety net the parent plan calls out
by name (`test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale`-class
tests); confirm they still pass, and still fail if a `flat_frame_hash` kwarg
is dropped, immediately after 12a.

### C.3 The sharp edge: two names that stay "valid but silently wrong"

`scan_session` and `_delete_if_exists` remain genuine attributes of
`pipeline_module` even after `run_lrgb` moves out, because `run_narrowband`
(which stays in `pipeline.py` until Step 13) also imports and calls both
directly (lines 1116 and 1129 respectively — see Section D). This means
`monkeypatch.setattr(pipeline_module, "scan_session", ...)` or
`"_delete_if_exists"` will **not** raise `AttributeError` after 12a — it
will silently patch a name pipeline.py still legitimately owns, while
`lrgb_orchestrator.run_lrgb`'s own `from .ingest import scan_session` /
`from .contributor_staleness import _delete_if_exists` resolve independently
and are untouched by the patch. Every one of the 8 `scan_session` sites and
the 1 `_delete_if_exists` site above (2185) is this exact trap — the real
Siril/filesystem code would run inside what's meant to be a fast, isolated
unit test, exactly the "fails loudly only by accident" failure Risk 1
describes. Grep for these two names specifically after 12a and confirm
`lrgb_orchestrator_module` (not `pipeline_module`) is the patch target.

**Total: 28 monkeypatch call sites requiring retargeting** (24 +
4), plus the two silent-danger names above needing extra attention during
verification since they won't fail loudly if missed.

---

## D. Import migration for `pipeline.py` -> `lrgb_orchestrator.py` (Step 12a)

**[Round-2 review correction, 2026-09-17]** `PipelineResult` itself was never
classified below, and it's not a type-hint-only concern:
`result = PipelineResult()` is a real runtime instantiation inside
`run_lrgb` (line 301) and inside `run_narrowband` (line 1109) alike, so
`from __future__ import annotations` does not make this safe to leave
implicit. Once `run_lrgb` moves to `lrgb_orchestrator.py`, that file needs
`PipelineResult` at runtime, while `pipeline.py` still needs it too (for
`run_narrowband`, which stays there until Step 13) AND needs to import
`run_lrgb` back from `lrgb_orchestrator.py` for its re-export
(Section E, 12a). Defining `PipelineResult` in either file and importing it
into the other creates a genuine two-way import cycle — the first one this
refactor has produced (verified: no earlier extraction, e.g.
`master_builder.py`/`colour_contributor.py`, ever imports back from
`pipeline.py`). Traced by hand: whichever module loads first hits
`ImportError: cannot import name 'PipelineResult' from partially
initialized module` on the refactor's own first sanity check
(`python -c "import astro_pipeline.pipeline"`), before pytest even runs.
**Fix, applied to this plan**: extract `PipelineResult` into its own new
module, `pipeline_result.py` (mirrors `filter_constants.py`'s precedent
from Step 2 — a small, zero-dependency module that exists specifically so
two orchestration-layer modules can both depend on it without depending on
each other). `pipeline_result.py` imports `Checkpoint`/`ExportResult` from
their existing homes (`checkpoints.py`/`export_image.py`) and defines only
the dataclass; both `pipeline.py` and `lrgb_orchestrator.py` import
`PipelineResult` from it. This also pre-empts an identical cycle at
**Step 13** (`run_narrowband` constructs `PipelineResult` too) — do this
extraction once, now, rather than again at Step 13. `Checkpoint`'s import
moves out of `pipeline.py` entirely as part of this (it was only ever
needed for `PipelineResult`'s own `list[Checkpoint]` field — confirmed by
grep, no other reference in the file); `pipeline.py` no longer needs to
import `Checkpoint` directly at all once this lands.

This is a small, separate, zero-risk mechanical step — do it as its own
first commit, **12a-0**, before 12a itself (see Section E).

---

**[Round-1 review correction, 2026-09-17]** The original version of this
section wrongly framed most of the "stays in `pipeline.py`" list as
narrowband-only, based on checking only `run_narrowband`'s own body against
`pipeline.py`'s import block — it never checked whether `run_lrgb` ALSO
calls those same names. Grep-verified against the real code: `run_lrgb`
itself directly calls/references `pipeline_dir` (298, 415), `checkpoint`
(792, 803, 833, 962, 1006), `save_checkpoints` (799, 810, 840, 969, 1013),
`usable` (826, 854, 986), `ColourContributorBuilder` (614, 686),
`CalibrationMode`/`FlatPolicy` at runtime (384, 386, 561, 562 — enum
comparisons, not just type hints), `DEFAULT_PEDESTAL` (line 205, a parameter
**default value**, evaluated at `def`-time — a name resolution failure here
would break `import astro_pipeline.lrgb_orchestrator` itself, not just a
later call), `infer_calibration_mode`/`infer_flat_policy` (343/558, 392/563),
and `stretch_rgb` (line 991 — `run_lrgb`'s own RGB-only-mode stretch call,
a *separate* call site from `run_narrowband`'s use at 1177). If 12a had
followed the original Section D literally, `lrgb_orchestrator.py` would be
missing all of these imports and would fail immediately (`DEFAULT_PEDESTAL`
at import time) or NameError on the first test that calls `run_lrgb`. This
would have been a loud, self-diagnosing failure (not a silent behavior
change), but "full suite green on the first pass" as originally claimed for
12a was wrong. Corrected three-way split below.

Cross-checked against `run_narrowband`'s own body (lines 1041-1200) AND
`run_lrgb`'s own body (195-1038) for every name, not just the former.

**Moves fully to `lrgb_orchestrator.py`, removed from `pipeline.py`
entirely (used by `run_lrgb` only — not `run_narrowband`, not
`PipelineResult`):**
`resolve_instrument_profile`, `resolve_osc_instrument_profile`,
`UnknownInstrumentError`, `run_graxpert_background_extraction`,
`LUMINANCE_FILTER`, `RGB_FILTERS`, `OSC_FILTER`, `discover_luminance_contributors`,
`discover_osc_contributors`, `select_luminance_source`, `LumCandidate`,
`resolve_lights`, `build_group_master`, `contributor_fwhm_arcsec`,
`contributor_dir`, `MIN_SEQUENCE_FRAMES`, `STAGE_ORDER`, `ContributorSignature`,
`RunSignature`, `cascade_from`, `diff_invalidation`, `frame_identity_hash`,
`load_run_signature`, `save_run_signature`, `reproject_to_reference`,
`crop_to_common_coverage`, `match_gain_offset`, `combine_same_grid`,
`stretch_and_compose`, `shutil`, `astropy.io.fits` (not referenced anywhere
else in `pipeline.py`).

**Needed by BOTH files — import independently into each, no conflict
(used by `run_lrgb` AND by `run_narrowband` and/or `PipelineResult`):**
`scan_session` (also 1116 — see C.3 above), `_delete_if_exists` (also
1129 — see C.3 above), `stretch_rgb` (991 in `run_lrgb`, 1177 in
`run_narrowband` — two independent call sites, not shared code), `usable`
(826/854/986 in `run_lrgb`, 1158/1175 in `run_narrowband`), `pipeline_dir`
(298/415 in `run_lrgb`, 1106 in `run_narrowband`), `infer_calibration_mode`/
`infer_flat_policy` (343/558 and 392/563 in `run_lrgb`, 1119/1120 in
`run_narrowband`), `ColourContributorBuilder` (614/686 in `run_lrgb`, 1131
in `run_narrowband`), `CalibrationMode`/`FlatPolicy` (runtime enum
comparisons in both, not just type hints/params), `DEFAULT_PEDESTAL`
(default value for both functions' own `pedestal` parameter — each file
needs its own import, since each defines its own function signature),
`checkpoint`/`save_checkpoints` (both call these directly at their own
checkpoint-emission sites).

**Stay ONLY in `pipeline.py` (not called by `run_lrgb` at all):**
`build_single_filter_master` (re-exported for `skill/run_narrowband_boost.py`,
not called by either orchestrator directly — leave the re-export in place,
per Risk 4). Also pre-existing, confirmed unused by anything in the file
today (unrelated to this refactor, not moved): `INSTRUMENT_PROFILES`,
`OSC_INSTRUMENT_PROFILES`, `OSCInstrumentProfile`. `ColourContributor` (the
dataclass) is referenced only as a local-variable type annotation inside
`run_lrgb` (line 566) — under `from __future__ import annotations` this is
never evaluated at runtime, so it needs no import in `lrgb_orchestrator.py`
for correctness, only for a type checker; import it there anyway for
mypy/IDE support since the annotation is moving with the function.

After 12a, `python -c "import astro_pipeline.pipeline"` and `python -c
"import astro_pipeline.lrgb_orchestrator"` must both succeed with no cycle,
per Risk 2's mitigation — check both directions since `pipeline.py` will now
import `run_lrgb` back from `lrgb_orchestrator.py` for its re-export.

---

## E. Sub-step execution plan

Each sub-step: make the change -> run the Risk-2 import check -> run the
full suite -> confirm the pass/skip count matches the baseline recorded
after the parent plan's Step 0 -> commit. Never batch two of 12a-12d into
one commit (matches the project's own single-purpose-commit convention).

### 12a-0 — extract `pipeline_result.py` (new, [Round-2 review addition])

- Create `src/astro_pipeline/pipeline_result.py`: move the `PipelineResult`
  dataclass (currently `pipeline.py` lines ~174-181) there verbatim, with
  its own `from .checkpoints import Checkpoint` / `from .export_image import
  ExportResult` imports.
- In `pipeline.py`: replace the dataclass definition with
  `from .pipeline_result import PipelineResult`; drop the now-unused
  `Checkpoint` import (confirmed nothing else in `pipeline.py` references it
  — see Section D). **[Round-3 review addition]** `ExportResult`
  (`pipeline.py`'s own import from `.export_image`) becomes unused for the
  same reason at the same moment — it was only ever needed for
  `PipelineResult`'s own `export_result: ExportResult | None` field — so
  drop it too in this same commit, not as a separate cleanup later.
- No behavior change, no test change expected (nothing imports `Checkpoint`
  or the dataclass definition location directly — `test_pipeline.py` and
  others import `PipelineResult`, if at all, from `astro_pipeline.pipeline`,
  which keeps working via the new import). `python -c "import
  astro_pipeline.pipeline"` green, full suite green at baseline. Commit
  BEFORE 12a — this removes the circular-import trap Section D identifies
  before it can ever be hit.

### 12a — move `run_lrgb` verbatim, re-export, retarget monkeypatches

- Create `src/astro_pipeline/lrgb_orchestrator.py`. Move lines 195-1038 of
  `pipeline.py` (the entire `run_lrgb` function, docstring included) into it
  verbatim — **zero internal restructuring**, not even renaming a local
  variable.
- Move the imports identified in Section D's "moves fully" list into the new
  file's own import block; remove them from `pipeline.py`'s import block
  (Section D's "stays" list remains in `pipeline.py`, needed by
  `run_narrowband`).
- In `pipeline.py`, add `from .lrgb_orchestrator import run_lrgb` so every
  existing external caller (`skill/run_stage.py`, `tests/test_pipeline.py`,
  `tests/test_run_signature.py`) keeps working unchanged via
  `astro_pipeline.pipeline.run_lrgb`.
- **[Round-2 review addition]** `tests/test_pipeline.py` lines 11-27 (plus a
  local import at line 585) import 6 names directly from
  `astro_pipeline.pipeline` that this step's "moves fully" list removes
  from `pipeline.py` entirely: `build_group_master`, `contributor_dir`,
  `contributor_fwhm_arcsec`, `discover_luminance_contributors`,
  `resolve_lights`, `select_luminance_source`. These were correctly
  classified as run_lrgb-only in Section D, but nobody had checked whether
  anything besides `run_lrgb`/`run_narrowband` imports them from
  `pipeline`'s namespace until round 2. Left unfixed, this breaks
  `test_pipeline.py`'s collection entirely (`ImportError`), failing all
  ~68 tests in that file, not just the `run_lrgb`-related ones — this is
  the step's real "does the full suite even collect" risk, not just the
  `run_lrgb` tests. Retarget these imports to their real current homes
  (already-extracted modules from earlier steps, not `lrgb_orchestrator.py`):
  `build_group_master`/`contributor_fwhm_arcsec`/`resolve_lights` from
  `astro_pipeline.master_builder` (Step 7), `contributor_dir` from
  `astro_pipeline.workspace` (Step 8), `discover_luminance_contributors`/
  `select_luminance_source` from `astro_pipeline.luminance_selection`
  (Step 11) — matching the precedent already used when
  `resolve_instrument_profile`'s single import was retargeted at Step 6.
  Grep `tests/*.py` for all 6 names to confirm no other file imports them
  from `pipeline` too before considering this done.
- Retarget all 28 monkeypatch sites from Section C: `import
  astro_pipeline.lrgb_orchestrator as lrgb_orchestrator_module` (or
  equivalent) in place of / alongside `import astro_pipeline.pipeline as
  pipeline_module`, and change every `monkeypatch.setattr(pipeline_module,
  "<name>", ...)` in the 28 sites to `monkeypatch.setattr(lrgb_orchestrator_module,
  "<name>", ...)`. Pay special attention to the `scan_session`/`_delete_if_exists`
  sites (Section C.3) — these will NOT raise if missed, only silently run
  real code.
- Grep every literal string in Section B before/after to confirm the count
  is unchanged.
- `python -c "import astro_pipeline.pipeline"` and `python -c "import
  astro_pipeline.lrgb_orchestrator"` — both green, no cycle.
- Full suite green at the recorded baseline count. Time the suite — a
  missed monkeypatch retarget on `scan_session`/`_delete_if_exists` would
  likely show up as new wall-clock slowness or new subprocess activity in a
  test that used to be instant (Risk 1's own stated tell), not necessarily a
  failure.
- Commit.

### 12b — introduce `LRGBOrchestrator`, extract `_build_masters()`

- In `lrgb_orchestrator.py`, add `class LRGBOrchestrator:` with `__init__`
  taking the same 14 parameters as `run_lrgb` (Section A.1), in the same
  order, with the same defaults. `__init__` body:
  - Lines 290-295 (validation) run FIRST, unchanged, raising `ValueError`
    before any other state is built — matches `run_lrgb`'s current ordering
    exactly (a bad `stop_after`/`force` must fail before `final.mkdir()` or
    anything else happens).
  - Lines 297-307 become `self.project_dir`, `self.out`, `self.final`,
    `self.result = PipelineResult()`, `self.notes = self.result.notes`,
    `self.checkpoint_dir`, `self.checkpoints_path`, `self.run_signature_path`,
    `self.old_signature = load_run_signature(...)`.
  - Also set `self.previous = None` and `self.previous_linear: bool | None
    = None` here (currently assigned at lines 789-790, right before
    checkpoint 01 emission) — moving them to `__init__` is a harmless,
    behavior-preserving reordering since nothing reads them before line 789
    anyway, and it means `_build_masters()` doesn't need its own
    "initialize state" preamble.
  - Store all 14 constructor params as `self.<name>` (Section A.1).
- `_build_masters(self) -> None`: move lines 309-810 (from the `_log("===
  scanning...")` call through checkpoint 02's `save_checkpoints` call),
  **excluding** lines 789-790 (`previous`/`previous_linear` initialization,
  already relocated into `__init__` above — **[Round-3 review: clarified]**
  this is a sub-range carve-out of 309-810, not an overlap or double-move)
  and **excluding** the `if stop_after == "masters": return result` block
  (812-818). Every bare local reference to a constructor param or
  cross-phase variable becomes `self.<name>`; `report`, `run_signature_path`,
  `old_signature`, `new_signature`, and everything in Section A.3's
  "masters-internal only" list stay as plain locals inside this one method
  (per Section A.3's recommendation — do not promote them to `self.` in this
  step).
- `run_lrgb()` becomes: construct `LRGBOrchestrator(...)`, call
  `orchestrator._build_masters()`, then reproduce the `if stop_after ==
  "masters": return orchestrator.result` check (lines 812-818) **in
  `run_lrgb()` itself, not inside `_build_masters()`** — this early-return
  decision is a `run()`-level concern (see 12d's note on introducing `run()`
  once all three methods exist), not part of "building masters" itself.
  Until 12d, `run_lrgb()` can call the phases directly in sequence with the
  same three `stop_after` checks inline, exactly mirroring today's
  structure but through method calls instead of inline code.
- No test/import changes expected beyond what 12a already did — 12b is a
  pure in-file restructuring, no monkeypatch target changes (nothing new
  crosses a module boundary).
- Full suite green. Commit.

### 12c — extract `_reconcile()`

- `_reconcile(self) -> None`: move lines 820-969 (the `if not is_rgb_only:`
  L-background block through checkpoint 04's `save_checkpoints` call) —
  **NOT** including the `if stop_after == "reconciled": return result`
  block (971-977), same reasoning as 12b.
- Reads `self.is_rgb_only`, `self.final`, `self.checkpoint_dir`,
  `self.checkpoints_path`, `self.previous`/`self.previous_linear`,
  `self.contributors`, `self.reference`, `self.reference_pos`, `self.result`,
  `self.notes` — all already `self.` from 12b's `__init__`/`_build_masters()`.
- Sets `self.lum_for_compose_path` (lines 848/850 — now genuinely a `self.`
  write inside `_reconcile()`, matching Section A.2 #12's corrected
  boundary) and `self.rgb_reconciled` (line 853 — Section A.2 #13, the new
  finding).
- `run_lrgb()`'s inline sequencing updates to call `orchestrator._reconcile()`
  then check `stop_after == "reconciled"`.
- Full suite green. Commit.

### 12d — extract `_finalize()`, introduce `run()`

- `_finalize(self) -> None`: move lines 979-1036 (composite naming through
  the export log line). Reads `self.is_rgb_only`, `self.final`,
  `self.rgb_reconciled`, `self.lum_for_compose_path`, `self.stretch_method`,
  `self.target`, `self.checkpoint_dir`, `self.checkpoints_path`,
  `self.previous`/`self.previous_linear`, `self.result`, `self.notes`.
  Mutates `self.result.composite_path`/`self.result.export_result` and
  appends to `self.result.checkpoints`, exactly as today.
- Introduce `run(self) -> PipelineResult`:
  ```python
  def run(self) -> PipelineResult:
      self._build_masters()
      if self.stop_after == "masters":
          return self.result
      self._reconcile()
      if self.stop_after == "reconciled":
          return self.result
      self._finalize()
      return self.result
  ```
  (This is the point where `self.stop_after` — Section A.1's fourth called-out
  constructor param — is actually read from two different methods' calling
  context, satisfying the cross-boundary requirement identified there.)
- `run_lrgb()` collapses to:
  ```python
  def run_lrgb(
      project_dir: str | Path,
      telescope: str,
      target: str,
      ra_hours: float,
      dec_deg: float,
      lum_binning: int = 1,
      rgb_binning: int = 2,
      stretch_method: str = "autostretch",
      lum_source: tuple[str, int] | None = None,
      pedestal: float = DEFAULT_PEDESTAL,
      stop_after: str | None = None,
      force: set[str] | None = None,
      flat_policy: FlatPolicy | None = None,
      calibration_mode: dict[str, CalibrationMode] | None = None,
  ) -> PipelineResult:
      return LRGBOrchestrator(
          project_dir, telescope, target, ra_hours, dec_deg,
          lum_binning=lum_binning, rgb_binning=rgb_binning,
          stretch_method=stretch_method, lum_source=lum_source,
          pedestal=pedestal, stop_after=stop_after, force=force,
          flat_policy=flat_policy, calibration_mode=calibration_mode,
      ).run()
  ```
  **[Round-2 review correction]** the originally-drafted `**kwargs` version
  is NOT actually byte-for-byte identical to the real signature — it loses
  the explicit parameter names/defaults from introspection (`inspect.signature`,
  IDE autocomplete, anything relying on keyword-only enforcement or the real
  defaults being visible without following through to `LRGBOrchestrator`).
  Every real call site (`scripts/`, `skill/`, all tests) happens to pass
  trailing params as keywords, so nothing currently breaks either way — but
  the plan's own explicit "byte-for-byte identical" commitment (Section 1)
  means write out the full signature, not a `**kwargs` shortcut.
- Full suite green, including a manual check that
  `test_run_lrgb_rejects_unknown_stop_after`/`..._rejects_unknown_force_stage`
  (lines 1313/1318) still fail fast, before any `LRGBOrchestrator` state is
  built — confirms `__init__`'s validation-first ordering from 12b survived
  three refactoring passes intact.
- Commit.

---

## Open items for a follow-up adversarial pass

1. This document's own re-derivation found one variable (`rgb_reconciled`,
   Section A.2 #13) neither prior adversarial round caught, and corrected
   four names (`report`, `run_signature_path`, `old_signature`,
   `new_signature`) that were listed as cross-phase state but are actually
   masters-internal. Given the pattern (each of three independent passes so
   far has found something the previous two missed), a third adversarial
   review of this specific document — before 12a starts — is warranted
   rather than assumed complete.
2. Section E's `_build_masters()` design keeps `report`/`old_signature`/
   `new_signature`/`run_signature_path` as plain locals (Section A.3). If a
   future reviewer prefers Open Question 3's alternative split
   (`_build_luminance()` + `_build_colour_contributors()` +
   `_build_run_signature()`), all four of those names WOULD need promotion
   to `self.` at that point — not a correction to this document, but a
   consequence of a different method boundary this document explicitly
   didn't adopt.

## Round-1 adversarial review (2026-09-17) — outcome

A fresh, independent reviewer (no shared context with this document's
author, per this project's established review convention) verified every
claim in this document against the real code. Full review:
`docs/task5-step12-review-round1.md`. Outcome, applied inline above:

- **1 high-severity correction** (Section D's import-migration split was
  materially wrong — ~11 names `run_lrgb` itself needs were mis-framed as
  narrowband-only; corrected into a proper three-way split above). This
  would have caused 12a to fail on its own `import`/test-suite check
  immediately (loud, self-diagnosing — not a silent behavior change — but
  the plan's "full suite green on first pass" claim for 12a was wrong as
  originally written).
- **2 cosmetic citation slips** in Section A.2 (a stray `final` line
  citation, a missed second `previous`/`previous_linear` cycle at
  checkpoint 02) — corrected inline, no conclusion changed.
- **Everything else independently confirmed correct**: Section B's full
  checkpoint-label/filename inventory, Section C's full 28-site monkeypatch
  inventory (exact line numbers and test-function attribution), the new
  `rgb_reconciled` finding, the four-name masters-internal demotion, and
  the `__init__` validation-before-`final.mkdir()` ordering claim.

Given a real (if loud, not silent) defect surfaced on this first review
pass, a second independent round is warranted before treating this document
as execution-ready, per this project's standing convention for
plans/steps of this size and risk.

## Round-2 adversarial review (2026-09-17) — outcome

A second fresh, independent reviewer (no shared context with either this
document's author or the round-1 reviewer) reviewed the POST-round-1
version of this document against the real code. Full review:
`docs/task5-step12-review-round2.md`. Outcome, applied inline above:

- **1 critical, previously-uncaught finding**: `PipelineResult` is
  instantiated at runtime by both `run_lrgb` and `run_narrowband`, was
  never classified in Section D at all, and creates a genuine two-way
  import cycle once `run_lrgb` moves out — the first cycle this refactor
  has produced. Fixed by extracting `PipelineResult` into its own new
  `pipeline_result.py` module (mirroring `filter_constants.py`'s
  precedent), as a new first sub-step **12a-0**, done before 12a. This also
  pre-empts the identical problem recurring at Step 13.
- **1 high, previously-uncaught finding**: `tests/test_pipeline.py` imports
  6 names directly from `astro_pipeline.pipeline`
  (`build_group_master`, `contributor_dir`, `contributor_fwhm_arcsec`,
  `discover_luminance_contributors`, `resolve_lights`,
  `select_luminance_source`) that 12a's original "moves fully" list would
  have removed from `pipeline.py` without retargeting — breaking
  `test_pipeline.py`'s collection entirely (all ~68 tests in the file, not
  just `run_lrgb`'s). Fixed by adding an explicit retargeting bullet to
  12a.
- **1 low/medium finding**: 12d's original `**kwargs`-based `run_lrgb()`
  wrapper wasn't literally byte-for-byte identical to the real signature
  (loses explicit param names/defaults from introspection), contradicting
  the parent plan's own stated commitment even though no real call site
  currently breaks either way. Fixed by writing out the full explicit
  signature.
- **Everything else independently re-confirmed correct**: the 27-attribute
  `self.` state inventory has no further missed cross-phase variable
  (re-checked specifically for this, given the established pattern of each
  round finding something the others missed); Section C's monkeypatch
  targets spot-checked and exact; Section E's phase-boundary line ranges
  byte-exact against the real file; Section D's "needed by both" list
  (post round-1 correction) verified correct.

**Net effect across both rounds**: 12a as originally drafted would NOT have
reached a green full-suite run, for three independent reasons found across
two rounds (Section D's import split, the `PipelineResult` cycle, and the
test-import retargeting gap) — all loud/self-diagnosing failures, not
silent behavior changes, but real defects nonetheless. Given round 2 also
surfaced a critical, previously-unseen finding (not just cosmetic cleanup
of round 1's corrections), this matches the project's established
"keep reviewing until a round finds only small/cosmetic issues" convergence
criterion — round 2 did not converge. A third round is warranted before
treating 12a-0/12a as execution-ready.
