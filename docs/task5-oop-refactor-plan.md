# Task 5: OOP refactor plan for `astro_pipeline` (behavior-preserving)

Status: 2 rounds of fresh adversarial review complete (2026-09-16), findings
merged inline as `**[Round-1/Round-2 review ...]**` annotations. Not
committed — planning artifact only, no source was changed to produce this
document. Ready for a final human go-ahead before Step 0 starts.

Scope confirmed by direct reading of every file named below (not inferred
from the roadmap summary): `pipeline.py` (full, 2459 lines), `run_signature.py`
(full), `calibration.py` (full), `reconciliation.py` (full),
`background_color.py` (full), `checkpoints.py` (full), `__init__.py`, all
`skill/*.py` import statements, `tests/conftest.py`, `tests/test_pipeline.py`
import list and full `def test_` inventory, `tests/test_run_signature.py`'s
monkeypatch call sites, and a full local test run (`284 passed, 35 skipped,
0 failed`, ~9.6 minutes). Module signatures for every other `src/astro_pipeline/*.py`
file were grepped to confirm they are already single-responsibility and out
of scope (see "Out of scope").

---

## 0. Headline findings that shape everything below

0a. **[Round-1 review correction]** Step 1 (dedup `_log`) is not actually
    zero-risk — see the corrected `logging_utils.py` entry in Section 1.

1. **`pipeline.py`'s "private" underscore functions are not private.**
   `tests/test_pipeline.py` imports `_build_colour_contributor` and
   `_NarrowbandNormalizingReport` directly from `astro_pipeline.pipeline`.
   `skill/run_narrowband_boost.py` imports `_NarrowbandNormalizingReport` and
   `build_single_filter_master` directly from `astro_pipeline.pipeline` too.
   34 separate `monkeypatch.setattr(pipeline_module, "<name>", ...)` call
   sites across `test_pipeline.py`/`test_run_signature.py` patch functions
   that live in `pipeline.py`'s namespace only because they were imported
   there from `calibration.py`/`reconciliation.py`/`ingest.py`/etc
   (`build_master`, `select_dark`, `run_calibration`, `solve`,
   `register_and_stack`, `reproject_to_reference`, `crop_to_common_coverage`,
   `run_script`, `scan_session`, `run_graxpert_background_extraction`,
   `stretch_rgb`, `_build_colour_contributor`). **This is the dominant
   constraint on the whole refactor** — see Risk 1 below.

2. **`run_lrgb`'s own multi-stage orchestration (the code most worth
   refactoring) currently has almost no test coverage that runs without
   Kaveh's personal M51 project folder on his own machine.** All 4 tests of
   `stop_after`/`force` staged-execution semantics (`test_run_lrgb_stop_after_masters_*`,
   `test_run_lrgb_stop_after_reconciled_*`, `test_run_lrgb_full_run_after_staged_calls_*`,
   `test_run_lrgb_force_final_only_touches_*`) are gated on
   `@requires(FINAL_DIR / "lrgb_final.fit")`. I ran the full suite in this
   session's environment: **all 35 skips are real-fixture-gated, zero real
   M51/NGC3628/Abell6/IC1396 data is present even in this dev environment**,
   so right now the orchestration-level behavior-preservation contract is
   verified by *nobody*, on *no* machine, in the common case. Multi-contributor
   reconciliation (2+ RGB binnings combined via `match_gain_offset` +
   `combine_same_grid` through `run_lrgb` itself, not the underlying
   functions in isolation) and the RunSignature-driven stale-file-deletion
   cascade for non-flat cases are in the same position. This is the
   project's single biggest risk to the "100% behavior-preserving" mandate,
   independent of how the module split is drawn. See Section 3.

3a. **[Round-1 review correction]** Section 0's claim that "zero real
    M51/NGC3628/Abell6/IC1396 data is present even in this dev environment"
    is FALSE for 3 of the 4 fixtures. Verified directly: `NGC3628_PROJECT_DIR`,
    `ABELL6_PROJECT_DIR`, `IC1396_PROJECT_DIR` all exist and are actively
    exercised by real-Siril-gated tests today (confirmed by a real
    AstropyUserWarning about a truncated FITS file firing during
    `test_stage_precalibrated_lights_real_t73_produces_pp_lights_sequence`,
    which cannot happen if the test's body never runs). Only the M51
    "Desktop" fixture (`PROJECT_DIR`/`FINAL_DIR`) is genuinely absent — and
    that's specifically the fixture the 4 `stop_after`/`force` staged-
    execution tests are gated on, so the *narrower* claim ("those 4 tests
    skip, run_lrgb's staged-orchestration control flow has no coverage
    without M51 specifically") still holds and is why Section 4b's new
    mocked tests are still needed. But Section 4c's table description of
    NGC3628/Abell6/IC1396 tests as "currently-skipped-in-this-session" is
    wrong — they run for real in this environment — and "the real-data
    check can only run on one machine" overstates it: 3 of 4 real-data
    checks already run in ordinary dev runs today, only M51's does not.
3. The known pre-existing failure named in the task brief
   (`test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale`)
   **did not reproduce** in this session — it passed both in isolation and
   as part of the full 284-passed run. Either it was already fixed by an
   intervening commit, or it's order/environment-sensitive in a way this
   run didn't trigger. Re-verify before assuming it still needs fixing;
   don't spend a refactor step "fixing" a test that's currently green.

---

## 1. Proposed module structure for `pipeline.py`'s contents

`pipeline.py` today mixes four genuinely different responsibilities:
narrowband filter-name/channel bookkeeping, single-group master-building,
colour-contributor construction (RGB and OSC), and top-level orchestration
(`run_lrgb`/`run_narrowband`). The split below follows those seams, not the
literal function list.

New flat modules under `src/astro_pipeline/` (matching this project's
existing convention — no subpackages exist today, and introducing one only
for `pipeline.py` while everything else stays flat would itself be an
inconsistency):

### `filter_constants.py` (new, ~15 lines)
`RGB_FILTERS`, `LUMINANCE_FILTER`, `OSC_FILTER`. Zero-dependency. Exists
specifically so lower-level new modules (below) don't have to import
*from* `pipeline.py` to get these — importing "up" into the orchestration
layer to get a constant would recreate the import-cycle risk this whole
split is supposed to remove (see Risk 2).

### `logging_utils.py` (new, ~10 lines)
One `log(message, notes)` function. **[Round-1 review correction: this is
NOT a zero-risk dedup as originally drafted.]** The two existing copies are
NOT identical: `pipeline._log(message, notes: list[str])` crashes on
`notes=None`; `calibration._log(message, notes: list[str] | None)` guards
with `if notes is not None`. This is load-bearing: `calibration.py`'s
`stage_precalibrated_lights()` defaults `notes=None` and is called with no
`notes` arg by two real, currently-passing tests
(`test_stage_precalibrated_lights_real_t73_produces_pp_lights_sequence`,
`test_stage_precalibrated_lights_debayer_real_t02_produces_3channel`, both
real-Siril+real-fixture-gated and confirmed to actually execute in this
dev environment). The merged function MUST keep the `list[str] | None` +
guard version (calibration.py's), not pipeline.py's stricter one, or Step
1 silently breaks both tests on exactly the machine that has the real
fixtures configured.

### `resume_guard.py` (new, ~40 lines)
`usable()`. Single responsibility ("is an existing stage output actually
fit to resume from"), already independently reasoned about in its own
docstring, no reason to keep it bolted to `pipeline.py`.

### `narrowband_filters.py` (new, ~140 lines)
`NARROWBAND_PALETTES`, `NARROWBAND_FILTER_ALIASES`,
`normalize_narrowband_filter_name()`, `_NarrowbandNormalizingReport`,
`equalize_narrowband_channels()`. All four exist purely to support
`run_narrowband`'s own needs (filter-name spelling variance and the
per-channel SPCC substitute) and are unrelated to `narrowband_boost.py`'s
separate HaRGB-blend-into-an-existing-RGB-composite feature — don't merge
the two despite the shared "narrowband" word.

### `master_builder.py` (new, ~230 lines)
`resolve_lights()`, `build_master()` (**[Round-1 review: rename to
`build_group_master()` on extraction]** — `calibration.py:456` already
defines its own, unrelated `build_master(frame_paths, basename, work_dir,
...)`, a low-level stage/convert/stack-with-rejection primitive behind
`build_master_bias/dark/flat`. Both are already imported bare as
`build_master` into different test files today (`test_calibration.py` vs
`test_pipeline.py`); once `master_builder.py` sits flat next to
`calibration.py` both exporting an identically-named symbol, this is a
real, ongoing human/grep mistargeting risk, not just a cosmetic clash —
rename the extracted one rather than carry the collision forward),
`contributor_fwhm_arcsec()`, `build_single_filter_master()`. These four
form one real chain: resolve which lights feed a group, calibrate→
register→stack→solve them, and (optionally) measure the result's
sharpness. `resolve_lights` is used by the Luminance loop, the RGB loop,
and the OSC loop alike — it belongs at this shared, low level, not inside
a "colour contributor" module. Note: `build_single_filter_master()` calls
`infer_calibration_mode()`/`infer_flat_policy()` directly (pipeline.py
:957, :983) — a real, direct dependency on `calibration_policy.py`, not
merely "same layer, no edge" (see Risk 2 correction below); this is why
Step 5 (calibration_policy) must precede Step 7 (master_builder).

### `contributor_staleness.py` (new, ~140 lines)
`_delete_if_exists()`, `_colour_contributor_frame_hash()`,
`_osc_contributor_frame_hash()`, `_flat_frame_hash()`,
`_colour_contributor_flat_frame_hash()`, `_clear_colour_contributor_products()`,
`_clear_osc_contributor_products()`. Deliberately separated from the
builder: this is "what does staleness mean for a contributor, and how do
we clear it," which conceptually belongs with `run_signature.py`'s domain,
not with construction. Practical bonus: this module has **zero dependency
on Siril/GraXpert/SPCC** (it only hashes filenames and deletes files), so
every function in it is trivially fast-unit-testable without any
monkeypatching — a concrete, checkable improvement the split buys for free.

### `colour_contributor.py` (new, ~280 lines)
`ColourContributor` (dataclass, moved as-is) and a `ColourContributorBuilder`
class wrapping today's `_build_colour_contributor`/`_build_osc_colour_contributor`.
This is the strongest concrete OOP win available in the file. **[Round-1
review correction: the original param counts here were wrong — corrected
below.]** `_build_colour_contributor` takes **13** parameters,
`_build_osc_colour_contributor` takes **11**, and **9** are identical
(`project_dir, contrib_dir, report, telescope, target, binning, ra_hours,
dec_deg, notes`) with a 10th (`calibration_mode`) shared in name/type but
differing default. Verified against real call sites (`run_lrgb`'s two
separate loops over `rgb_binnings`/`osc_binnings`, one binning never needs
both shapes per the existing `_mixed_shape_binnings` guard) — one builder
instance per (telescope, binning) calling exactly one of
build_rgb()/build_osc() is a faithful match to real control flow; the
design holds even though the original numbers citing it were off. Bundle that shared context into the builder's
`__init__`/state; keep two methods, `build_rgb(flat_policy, calibration_mode,
filters=RGB_FILTERS, run_colour_calibration=True)` and
`build_osc(calibration_mode, bayer_pattern=0)`, each taking only the
parameters that actually differ between the two real callers.
**[Round-2 review: the design as drafted only accounts for shared
*parameters*, not shared *body* logic]** — reading both function bodies in
full, not just signatures: the GraXpert background-extraction block is
near-identical between the two (pipeline.py:1177-1182 vs :1289-1294 — same
`rgb_native_bg.fits` filename, same `usable()` gate, same
`run_graxpert_background_extraction` call shape), and the SPCC
colour-calibration block is near-identical too (:1184-1218 vs :1296-1310 —
same staging-file/`os.replace` pattern, differing only in which
`resolve_*_instrument_profile` function is called and logged; the OSC
version also lacks the RGB version's `run_colour_calibration=False`
narrowband branch). This is a real ~30-40 line factoring opportunity
(shared `_extract_background()`/`_calibrate_colour()` builder methods)
that should be part of the `ColourContributorBuilder` design, not just the
two thin `build_rgb`/`build_osc` entry points originally planned. This is not
"turn every function into a class" — it's specifically justified because
the two functions already share almost their entire parameter list and
because `run_lrgb` constructs one builder per (telescope, binning) inside a
loop, which is exactly the shape a small stateful object fits.
`contributor_dir()` moves to `workspace.py` instead (see below) — it's a
pure path-naming function and `workspace.py` already owns
`group_dir`/`group_name_for`/`pipeline_dir`, i.e. exactly this
responsibility.

### `workspace.py` (existing, +1 function)
Add `contributor_dir()` here, unchanged. No new module needed — this one
already exists for exactly this purpose.

### `background_color.py` / new split (see Section 2) supplies
`resolve_instrument_profile()`/`resolve_osc_instrument_profile()` — moved
there, not kept in the orchestration layer, since they only resolve
against `background_color.py`'s own `INSTRUMENT_PROFILES`/
`OSC_INSTRUMENT_PROFILES` registries and raise its own `UnknownInstrumentError`.
Only `tests/test_pipeline.py` imports `resolve_instrument_profile` from
`pipeline` today (a single import line) — update that one import rather
than adding a permanent re-export shim for it.

### `calibration_policy.py` (new, ~70 lines)
`infer_flat_policy()`, `infer_calibration_mode()`. Same shape ("given a
report and a telescope, infer a policy enum from what the data actually
has, never hardcode by name"), same dependency footprint
(`ingest.IngestReport`-shaped `report` + `calibration.py`'s enums). Natural
pairing, nothing to do with contributor construction or orchestration.

### `luminance_selection.py` (new, ~150 lines)
`LumCandidate` (type alias), `discover_luminance_contributors()`,
`discover_osc_contributors()`, `select_luminance_source()`. Kept as plain
functions, **not** a class — each is already independently unit-tested
today via direct import. **[Round-1 review correction: this module has NO
real dependency on `master_builder.py`/`colour_contributor.py`]** —
`select_luminance_source()` takes pre-measured FWHM values as plain tuples
(the actual `contributor_fwhm_arcsec()` call happens inside `run_lrgb`
itself, not inside `select_luminance_source`), and the two `discover_*`
functions only touch `report.instrument_groups()` plus
`filter_constants.py`. It could be extracted as early as immediately after Step 2 [round-2 review: not
concurrently with Step 2 — `discover_luminance_contributors`/
`discover_osc_contributors` reference `LUMINANCE_FILTER`/`OSC_FILTER`,
which only exist once `filter_constants.py` is created]; Section
5's ordering (Step 11) is not load-bearing, just where it happened to land
in the write-up. The logic is genuinely stateless (discover → [caller
measures] → pick), and wrapping
them in a `LuminanceSelector` class would only relocate the same code under
`self.` with no reduction in parameter count or duplication. (Contrast with
`ColourContributorBuilder` above, where the class earns its place by
actually cutting a shared 8-parameter prefix.) Use OOP where it reduces
real coupling; don't use it as a uniform style mandate.

### `lrgb_orchestrator.py` (new, ~700 lines including docstrings)
`run_lrgb()` and a new `LRGBOrchestrator` class it delegates to internally.
`run_lrgb()`'s public signature and behavior stay byte-for-byte identical —
callers (skill/tests) never see the class. Internally, `run_lrgb()`
constructs an `LRGBOrchestrator(project_dir, telescope, target, ra_hours,
dec_deg, ...)` and calls `.run(stop_after, force)`, which threads through
three methods that match the *existing, already-documented*
`STAGE_ORDER = ("masters", "reconciled", "final")` vocabulary exactly:

- `_build_masters()` — Luminance discovery/build/select, RGB+OSC contributor
  discovery/build (via `ColourContributorBuilder`), reference-contributor
  selection, `RunSignature` construction + `diff_invalidation` + the
  `force`-cascade delete, masters-stage checkpoints. (~350 of the current
  825 lines — the largest single chunk.)
- `_reconcile()` — L background extraction, per-contributor reprojection,
  crop-to-common-coverage, gain/offset match, weighted combine, checkpoint.
- `_finalize()` — stretch + compose + export, checkpoint.

The class exists because these three phases share a large amount of mutable
state (`report`, `notes`, `result`, `checkpoint_dir`, `checkpoints_path`,
`run_signature_path`, `is_rgb_only`, `contributors`, `reference`,
`lum_for_compose_path`, `old_signature`/`new_signature`) that would
otherwise have to be threaded as an ever-growing parameter list or returned
as an ever-growing tuple between three free functions — the class turns
that into `self.` attributes set by an earlier phase and read by a later
one, which is a real simplification, not decoration. This mirrors the
function's own docstring, which already describes exactly these three
phases and their dependency order.

### `narrowband_orchestrator.py` (new, ~180 lines)
`run_narrowband()` as a **plain function**, not a class. It has none of
`run_lrgb`'s multi-stage/force/stop_after/multi-contributor complexity by
its own explicit design ("structurally simpler than run_lrgb on purpose" —
its own docstring). Forcing a shared base class with `LRGBOrchestrator` for
symmetry would be over-engineering with no behavior or coupling benefit;
call this out explicitly as a deliberate non-uniformity.

### `pipeline.py` (kept, shrinks to ~120 lines)
Keeps only: the excellent module docstring (unchanged — it's real
institutional knowledge about *why* the pipeline is shaped this way, not
implementation detail), `PipelineResult` (dataclass — small enough to leave
here rather than spin out a one-class module), and re-export lines for
every symbol external code currently imports from `astro_pipeline.pipeline`.

**Recommendation on re-exports — prefer updating call sites over a
permanent facade.** The external surface is small and fully enumerable by
grep: 5 files under `skill/` and 2 files under `tests/` (`test_pipeline.py`,
`test_run_signature.py`). Updating ~50 import/monkeypatch lines across 7
files to point at the new module names is mechanical, grep-verifiable, and
more honest than perpetuating `pipeline.py` as a disguised god-module of
re-exports — which would undercut the stated goal of "small,
single-responsibility modules." Treat a temporary re-export shim as a
same-day scaffolding aid during an individual extraction step (see Section
4), removed once that step's callers are updated and the suite is green —
not as the end state.

---

## 2. `calibration.py` / `reconciliation.py` / `background_color.py`

**`calibration.py` (834 lines): leave as one module.** It is large because
its docstrings are unusually thorough (measured, real-hardware findings),
not because it mixes unrelated concerns — every function in it sits on one
real dependency chain (`select_dark` → `calibrate_lights` →
`run_calibration`, plus the three `build_master_*` siblings feeding into
that same chain). Splitting it would move code around without reducing
coupling, since `run_calibration` would still need to import from whatever
pieces got separated. Optional, low-priority, NOT required for Task 5:
`DarkSelection`/`select_dark`/`_calibrate_command` (already a pure,
independently-documented command-string builder) could move to a
`calibration_commands.py` if they ever need to be reused or tested
independent of the Siril-calling functions — defer this, it buys nothing
today.

**`reconciliation.py` (641 lines): split into three files, medium
priority, do after `pipeline.py`'s extraction is stable.** Three genuinely
independent algorithms are bundled under one "Stage 5" theme:
reprojection (`pixel_scale_deg`, `pick_finest_reference`,
`reproject_to_reference`, `reconcile_masters`), coverage cropping
(`crop_to_common_coverage`), and gain/offset matching + weighted combine
(`GainOffsetFit`, `fit_gain`, `match_gain_offset`, `combine_same_grid`).
None of the three calls into either of the others. Convert to a package:

```
reconciliation/
  __init__.py       # re-exports everything, so `from astro_pipeline.reconciliation
                     # import reproject_to_reference, crop_to_common_coverage,
                     # match_gain_offset, combine_same_grid` (pipeline.py's own
                     # import, test_reconciliation.py's, and
                     # skill/run_narrowband_boost.py's) needs zero changes.
  reproject.py
  coverage.py
  gain_offset.py
```
This is a real, low-risk win (no external caller needs to change), but it's
not the file the task brief names as the primary target, so sequence it
third, after `pipeline.py` is fully split and green.

**`background_color.py` (638 lines): split into two, medium priority,
same sequencing as reconciliation.py.** Two operationally unrelated
concerns share the file: SPCC colour calibration (`InstrumentProfile`/
`OSCInstrumentProfile` registries, `run_spcc`, `_parse_spcc_result`,
`_run_spcc_command`, `ColorCalibrationError`/`CatalogueUnavailableError`)
and GraXpert background extraction/denoising (`find_graxpert`,
`_nan_fill_for_graxpert`, `_check_graxpert_output_not_corrupt`,
`run_graxpert_background_extraction`, `run_graxpert_denoise`,
`BackgroundExtractionError`/`DenoiseError`) — different binaries, different
failure modes, already largely independent test classes in
`test_background_color.py`. Proposed:

```
color_calibration.py       # SPCC: InstrumentProfile, OSCInstrumentProfile,
                            # registries, run_spcc, resolve_instrument_profile,
                            # resolve_osc_instrument_profile (see Section 1)
background_extraction.py   # GraXpert: run_graxpert_background_extraction,
                            # run_graxpert_denoise, the shared NaN-fill/
                            # corruption-check helpers
background_color.py        # kept, shrinks to ~20 lines: calibrate_color_and_background()
                            # plus re-exports of everything pipeline.py/
                            # skill scripts/tests currently import from it
                            # (INSTRUMENT_PROFILES, OSC_INSTRUMENT_PROFILES,
                            # OSCInstrumentProfile, UnknownInstrumentError,
                            # run_graxpert_background_extraction, run_spcc)
```
Here a thin facade in `background_color.py` genuinely earns its keep
(unlike `pipeline.py`'s case) because the module *name* itself is not the
thing being deprecated. **[Round-1 review correction: the original
justification here was factually wrong and should not be cited.]** It is
NOT because `calibrate_color_and_background()` is "a real, meaningful
orchestration function" in active use — verified it is called nowhere
(not `pipeline.py`, not any skill script), only by its own direct unit
test, and it defaults to a hardcoded single-telescope
`profile=T24_PROFILE` predating the multi-instrument work (i.e. it may
itself be dead/stale code, not a reason to shape the module around it).
The real justification for keeping a facade here is narrower: several
real external call sites (`pipeline.py`, `skill/run_post_process.py`)
import `run_graxpert_background_extraction`/`run_graxpert_denoise` and the
instrument-profile registries from `background_color` by that module name
today, so a facade avoids touching those call sites — the same "update
callers vs. keep a facade" tradeoff as Section 1's Open Question 1, not a
special exemption. This split is optional/deferred either way; re-justify
before executing on it rather than reusing the dead-code claim above.

**Confirmed already cohesive, no action:** `run_signature.py`,
`checkpoints.py`, `ingest.py`, `staging.py`, `workspace.py`, `siril_driver.py`,
`solving.py`, `registration_stacking.py`, `star_removal.py`,
`stretch_compose.py`, `export_image.py`, `narrowband_boost.py`, `index.py`,
`manifest.py` — each greps out to one clear responsibility matching its own
module name; none mixes unrelated concerns the way `pipeline.py` does.

---

## 3. Behavior-preservation risk points and mitigations

**Risk 1 — monkeypatch target drift (silent, not loud).** 34 call sites do
`monkeypatch.setattr(pipeline_module, "<name>", fake)` for a name that will
no longer be defined-or-imported in `pipeline.py` once its owning function
moves (e.g. `build_master`, `_build_colour_contributor`, `scan_session`,
`reproject_to_reference`, `run_script`, `stretch_rgb`, `select_dark`,
`run_calibration`, `register_and_stack`, `solve`,
`run_graxpert_background_extraction`, `crop_to_common_coverage`). Setting an
attribute on a module object that nothing reads back is **not an error** —
the test still runs, and the *real* function executes instead of the fake
one (real Siril subprocess calls, real filesystem writes) inside what was
meant to be a fast unit test. This fails loudly only by accident (e.g. a
real Siril call erroring because a fixture path doesn't exist) — it can
also just silently produce a different, wrong-but-plausible result, which
is exactly the failure class this whole codebase's own `usable()`/
`checkpoint()` design otherwise goes out of its way to guard against.
*Mitigation:* whichever module a function moves to becomes the new
monkeypatch target (`monkeypatch.setattr(lrgb_orchestrator_module, "build_master", ...)`,
etc.) — this is mechanical, not a judgment call, once the destination
module is fixed. After each extraction step: `grep -n
"monkeypatch.setattr(pipeline_module, \"<moved_name>\"" tests/*.py` for
every name moved in that step, update every hit, then run the full suite
and confirm the count of `passed`/`skipped` is unchanged (a silently-passing
test that's actually exercising the real function instead of the mock
would likely still show as "passed" — the real signal to watch is new
wall-clock slowness or new Siril-process activity in a test that used to be
instant; time the suite before/after each step, not just pass/fail count).

**Risk 2 — import cycles.** `pipeline.py` currently imports from
`background_color`, `calibration`, `export_image`, `ingest`, `checkpoints`,
`reconciliation`, `registration_stacking`, `run_signature`, `siril_driver`,
`solving`, `stretch_compose`, `workspace` — i.e. it already sits at the top
of the dependency graph. Splitting it into `master_builder.py`,
`colour_contributor.py`, `lrgb_orchestrator.py`, etc. is safe as long as
those new modules import from the *existing* leaf modules
(`calibration.py`, `background_color.py`, ...), never from each other in a
loop, and never from `pipeline.py` itself (since `pipeline.py` will import
*from* them, for its re-exports). `filter_constants.py`'s entire reason for
existing (Section 1) is to give lower modules a way to get `RGB_FILTERS`/
`LUMINANCE_FILTER`/`OSC_FILTER` without reaching back into `pipeline.py`.
*Mitigation:* draw the dependency graph on paper before writing the new
`import` lines for each extracted module. **[Round-1 review correction:
the original graph below had two wrong edges — corrected here.]** It's a
strict DAG, but `calibration_policy` is a real, direct dependency of
`master_builder` (not a same-layer/no-edge relationship as originally
drawn — `build_single_filter_master()` calls `infer_calibration_mode()`/
`infer_flat_policy()` directly), and `luminance_selection` has NO real
dependency on `master_builder`/`colour_contributor` at all (see its
corrected Section 1 entry) so it isn't actually downstream of them despite
where Section 5 sequences its extraction. Corrected shape: `filter_constants`
→ `calibration_policy`/`narrowband_filters` → `master_builder` →
`contributor_staleness`/`colour_contributor`; `luminance_selection` sits
independently off `filter_constants` alone and could be extracted anywhere
in the sequence; both merge into `lrgb_orchestrator`/`narrowband_orchestrator`
→ `pipeline` facade. Re-verify this graph against the real `import`
statements you're about to write for each module, not by re-trusting this
diagram — it was wrong once already. **[Round-2 review: it still is, on a
different axis]** — missing two real edges to existing leaf modules:
`master_builder.py`'s `contributor_fwhm_arcsec()` calls
`checkpoints._pixel_scale_arcsec()` (pipeline.py:129 imports it directly;
used at line 610 — a private, underscore-prefixed cross-module import, the
same "not actually private" naming-leak class Section 0 finding 1 already
flags for skill/tests, just internal to `src/` this time), and
`contributor_staleness.py`'s four hash helpers all call
`run_signature.frame_identity_hash()` (public, no naming-leak, but still
an undrawn edge). Neither is a new cycle risk (both are pre-existing,
stable leaf modules), but add both edges to whatever diagram is actually
drawn at execution time — don't treat either version of this graph as
authoritative without re-deriving it from the real imports yourself. Then
let
`python -c "import astro_pipeline.pipeline"` (which fails loudly and
immediately on any real cycle) be the first check after every step, before
even running pytest.

**Risk 3 — `RunSignature`/`ContributorSignature` serialization contract.**
Confirmed by full reading of `run_signature.py`: `to_dict()`/`from_dict()`
round-trip through plain dicts/lists/strings only, with no reference to
`pipeline.py` types at all — `RunSignature`/`ContributorSignature` don't
change regardless of where `run_lrgb` or `ColourContributor` end up living.
The actual risk is narrower than "the format changes" — it's that
`_build_masters()`/`_reconcile()` (the extracted phase methods) must keep
calling `old_signature.contributor_stale(section, key, frame_hash, pedestal,
flat_frame_hash)` with **all five arguments positional-or-explicit, in this
exact order**, because `flat_frame_hash` has a `""`-default specifically to
stay backward-compatible with pre-Slice-2.3 persisted files (see its own
docstring: a caller that omits it "defeats the entire point of tracking it
at all"). This is exactly the bug class `test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale`
exists to catch, and it is precisely the kind of regression a refactor that
moves this call site to a different file, by a different author, months
later, could reintroduce without anyone noticing (dropping a kwarg is not a
type error). *Mitigation:* when extracting `_build_masters()`, diff the
moved code against the original line-for-line for every
`contributor_stale(...)` and `RunSignature(...)`/`ContributorSignature(...)`
construction call; keep the existing flat_frame_hash regression test
passing (see Risk 1 for why "passing" isn't sufficient on its own — also
re-run it with a deliberately-reverted 4-arg call once, by hand, to confirm
it still goes red, exactly as its own docstring claims, before trusting it
as the safety net for this specific risk).

**Risk 4 — skill script breakage.** Exact import surface, confirmed by
`grep`:
- `skill/run_stage.py` → `astro_pipeline.pipeline.run_lrgb`,
  `astro_pipeline.calibration.DEFAULT_PEDESTAL`
- `skill/run_narrowband.py` → `astro_pipeline.pipeline.{NARROWBAND_PALETTES,
  run_narrowband}`, `astro_pipeline.calibration.DEFAULT_PEDESTAL`
- `skill/run_narrowband_boost.py` → `astro_pipeline.pipeline.{_NarrowbandNormalizingReport,
  build_single_filter_master}`, `astro_pipeline.reconciliation.reproject_to_reference`,
  `astro_pipeline.narrowband_boost.*`, `astro_pipeline.export_image.export`,
  `astro_pipeline.ingest.scan_session`, `astro_pipeline.siril_driver.run_script`,
  `astro_pipeline.stretch_compose.{stretch_and_compose, stretch_rgb}`,
  `astro_pipeline.workspace.pipeline_dir`
- `skill/run_post_process.py` → `astro_pipeline.background_color.run_graxpert_denoise`,
  `astro_pipeline.export_image.{export, export_with_black_point}`,
  `astro_pipeline.star_removal.run_star_removal`
- `skill/interview.py` → `astro_pipeline.calibration.{CalibrationFramesMissingError,
  CalibrationMode, select_dark}`, `astro_pipeline.ingest.*`,
  `astro_pipeline.pipeline.infer_calibration_mode`

Only `pipeline.py` and (after Section 2's optional split)
`background_color.py`/`reconciliation.py` names are affected by this plan;
none of the other imports above move. *Mitigation:* these 5 files are
short, non-test, directly runnable — after each step that touches a name
one of them imports, actually run that skill script's own `--help` (or
whatever no-op invocation it supports) as a smoke check, not just `import`
it, since a skill script is exactly the kind of file this project's own
test suite does *not* cover end-to-end today (confirmed: no test imports
`skill.run_stage` or `skill.run_narrowband_boost` as a module — only
`test_skill_interview.py` exercises `skill/interview.py`).

**Risk 5 — resumability path changes (files-on-disk contract).** None of
the proposed moves touch *what path any stage output is written to* — only
which `.py` file contains the code that writes it. `usable()`'s
file-existence/NaN checks, `_delete_if_exists()`'s deletion targets, and
every hardcoded filename (`master_{filter}.fit`, `rgb_native.fit`,
`lum_bg.fits`, `rgb_reconciled.fit`, `lrgb_final.fit`, etc.) must be copied
verbatim, not "cleaned up" as a drive-by change — a renamed on-disk file
would silently orphan a real user's existing `_pipeline/` directory the
next time they resume a long-running job (the exact failure mode
`run_signature.py`'s own module docstring opens with, re: the orphaned
`T24-observer1-M51-Luminance-bin1` directory). *Mitigation:* treat every
literal filename string as a stop-and-check point while moving code — grep
for it before and after each extraction to confirm the string appears the
same number of times, unchanged.

**[Round-2 review addition] Risk 5b — checkpoint LABEL strings are the
same class of on-disk contract as filenames, and weren't covered above.**
`checkpoints.save_checkpoints()`'s own docstring documents a real,
already-occurred production bug: 7 orphaned preview PNGs on disk with no
matching `checkpoints.json` entry, from an earlier truncate/append-label
inconsistency — `save_checkpoints` merges *by label* and prunes previews
*by label*. The refactor must preserve every literal checkpoint label
string byte-for-byte across the module move
(`"01_master_{LUMINANCE_FILTER.lower()}"`, `"02_primary_rgb_colour_calibrated"`,
`"03_lum_background_extracted"`, `"04_rgb_reconciled"`,
`"05_rgb_final"`/`"05_lrgb_final"`, and run_narrowband's
`"02_narrowband_colour_calibrated"`/`"03_narrowband_equalized"`/
`"04_{palette}_final"`), or a resumed run silently orphans/duplicates
checkpoint entries — exactly the failure class checkpoints.py's own
docstring warns about. *Mitigation:* same as Risk 5 — grep every literal
label string before/after each step that touches checkpoint-writing code.

---

## 4. Test-coverage plan (hybrid: few real smoke tests + broad fast mocked coverage)

### 4a. What the CURRENT suite already covers well (fast, no real data needed)
Confirmed via the full `def test_` inventory of `test_pipeline.py` (68
tests) plus `test_run_signature.py` (685 lines), `test_reconciliation.py`
(559 lines, mostly synthetic-FITS-with-fabricated-WCS via `tmp_path` — this
pattern is already proven to work and should be reused for new tests, see
4b), and `test_background_color.py`:
- Every leaf/discovery/decision function: `discover_luminance_contributors`,
  `discover_osc_contributors`, `resolve_lights`, `select_luminance_source`,
  `resolve_instrument_profile`, `infer_flat_policy`, `infer_calibration_mode`,
  `contributor_dir`, `normalize_narrowband_filter_name`,
  `_NarrowbandNormalizingReport`, `equalize_narrowband_channels`,
  `contributor_fwhm_arcsec` (parsing/edge cases).
- `_build_colour_contributor`/`_build_osc_colour_contributor`/`build_master`
  edge cases via monkeypatched Siril calls (partial RGB, too-few-frames,
  HOO dedup/`-nosum`, debayer-without-dark).
- `RunSignature`/`ContributorSignature`/`diff_invalidation`/`cascade_from`
  in complete isolation — this part of the safety net is already excellent.
- `reproject_to_reference`, `crop_to_common_coverage`, `fit_gain`,
  `match_gain_offset`, `combine_same_grid` in isolation, on synthetic data.
- `run_lrgb`'s input validation (`stop_after`/`force` unknown-value
  rejection) and its RGB-only/mixed-OSC-guard paths (via `@requires_siril` +
  monkeypatched `_build_colour_contributor` — needs a real Siril binary but
  not real astronomical data).

### 4b. The gap: `run_lrgb`'s own staged-orchestration control flow
As established in Section 0, finding 2: `stop_after`, `force`-cascading,
multi-contributor reconciliation-through-`run_lrgb` (not the underlying
`reconciliation.py` functions in isolation), and the RunSignature-driven
deletion cascade actually firing from `run_lrgb`'s real call sites (beyond
the one existing `flat_frame_hash` regression test) are covered *only* by
tests gated on Kaveh's personal, non-committable M51 project folder — which
is not present even in this session's own dev environment. This is exactly
the code this refactor moves into `LRGBOrchestrator`. **Write these before
starting the module split, using the exact synthetic-FITS-plus-monkeypatch
pattern `test_reconciliation.py`/`test_run_signature.py`'s
`flat_frame_hash` test already establish** (small `tmp_path` FITS with a
fabricated `WCS`, `monkeypatch.setattr(pipeline_module, "build_master", ...)`
/`scan_session`/etc., no real Siril/GraXpert needed):

1. `test_run_lrgb_stop_after_masters_returns_before_reconciliation_MOCKED`
   — synthetic single-contributor project via monkeypatched `build_master`
   + `_build_colour_contributor`; assert `result.masters` populated,
   `composite_path is None`, `export_result is None`, and (via a call
   counter on the mocked `run_graxpert_background_extraction`) that nothing
   past masters was even attempted.
2. `test_run_lrgb_stop_after_reconciled_then_final_does_not_rebuild_masters_MOCKED`
   — two staged calls with call-count assertions on the mocked
   `build_master`/`_build_colour_contributor` (call count must not increase
   on the second, `stop_after="reconciled"` call) — the mocked-call-count
   equivalent of the real-fixture test's mtime assertions, runnable with no
   real data.
3. `test_run_lrgb_multi_contributor_reconciliation_end_to_end_MOCKED` — two
   fake `ColourContributor`s (different `stack_total`) driving through the
   real `run_lrgb` reconciliation branch (not `_build_colour_contributor`
   itself — monkeypatch it to return canned contributors pointing at tiny
   synthetic reprojectable FITS), asserting: the higher-`stack_total`
   contributor is chosen as `reference_pos`, `match_gain_offset` is called
   for every *other* contributor exactly once, `combine_same_grid` receives
   `weights` matching `stack_total`, and `RunSignature.colour_reference`
   matches. This currently has **zero** coverage without real M51 data.
4. `test_run_lrgb_force_cascade_deletes_exactly_the_expected_files_MOCKED`
   — for each of `force={"masters"}`/`{"reconciled"}`/`{"final"}`, assert
   exactly the files `cascade_from` says should be deleted are gone and no
   others, driven through a real `run_lrgb` call (extending the one
   existing `flat_frame_hash`-focused regression test's technique to cover
   `luminance_selected` change, `colour_reference` flip, and
   `stretch_method` change — currently each of those is only asserted
   against `diff_invalidation` in isolation, never against `run_lrgb`'s
   real invocation of it).

Write these as part of Step 0 (below), against the *current*, unrefactored
`pipeline.py` — they must pass before the split starts, and they are the
tests most likely to catch a Risk-1/Risk-3-class regression during the
split (a monkeypatch that silently stops firing, or a dropped
`flat_frame_hash` argument) because they exercise the orchestration glue
directly, not just the leaf functions.

### 4c. Real Siril/GraXpert smoke tests (small in number, authoritative)
No new fixture data needs to be sourced — four real, already-configured
project folders exist as `tests/conftest.py` constants, gated by
`pytest.mark.skipif`, and already have real (currently-skipped-in-this-session)
tests written against them:

| Fixture | conftest constant | Exercises |
|---|---|---|
| M51 (T24+T21, multi-user, multi-binning) | `PROJECT_DIR` / `FINAL_DIR` | The richest case: multi-contributor RGB combine, multi-user raw-sub Luminance merge, mixed exptime. `test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`'s SHA-256 byte-identity check is the single best "did the refactor change any output byte" oracle in the whole suite. |
| NGC 3628/T73 | `NGC3628_PROJECT_DIR` | `CalibrationMode.PRECALIBRATED` end-to-end (touches `master_builder.py`'s PRECALIBRATED branch and `colour_contributor.py`'s calibration-mode threading). |
| Abell 6 and HFG1/T02 | `ABELL6_PROJECT_DIR` | OSC + RGB-only mode (`_build_osc_colour_contributor`, the no-Luminance `run_lrgb` branch). |
| IC 1396/T68 | `IC1396_PROJECT_DIR` | OSC + RAW_LOCAL bias-only-no-dark (`build_master`'s `has_any_dark` branch). |

Because these are personal, gitignored, non-public paths, they can never be
a CI gate for the public repo — they are Kaveh's own pre-merge manual
verification step. Make that explicit rather than assuming a CI pipeline
will ever run them: add a literal checklist item to the final step of
Section 5 ("run the full suite with `ASTRO_PIPELINE_DESKTOP_DIR`/
`ASTRO_PIPELINE_ITELESCOPE_DIR` configured; confirm the SHA-256 byte-identity
test passes and no fixture-gated test newly fails or newly skips") — this
is the actual authoritative check for "did the refactor preserve behavior,"
and it can only run on one machine.

---

## 5. Staged execution order

Each step: extract one seam → update its call sites (imports +
monkeypatches, per Risk 1/4) → `python -c "import astro_pipeline.pipeline"`
(cycle check) → run the full suite → confirm **284 passed, 35 skipped, 0
failed** (or whatever 4b's new tests bring that baseline to — record the
new number after Step 0 and hold it fixed for every step after) → commit.
Never batch two extractions into one commit, matching this project's own
established convention (small, narrow, single-purpose commits — see recent
git log).

0. **Write the new mocked orchestration tests from Section 4b** against
   today's unrefactored `pipeline.py`. Confirm they pass. Commit. This
   raises the safety net before any code moves, and is itself independently
   valuable (it plugs a real, currently-existing coverage gap) even if the
   refactor were to stop here.
1. Extract `logging_utils.py` (dedup `_log`/dedup nothing behaviorally —
   pure mechanical move). Update `pipeline.py` and `calibration.py` to
   import from it. Zero test changes expected (no test imports `_log`
   directly). Full suite green. Commit.
2. Extract `filter_constants.py`. Update `pipeline.py` to import+re-export.
   Full suite green. Commit.
3. Extract `resume_guard.py` (`usable`). Full suite green. Commit.
4. Extract `narrowband_filters.py`. Update `pipeline.py`'s re-exports;
   update `skill/run_narrowband_boost.py`'s and `skill/run_narrowband.py`'s
   imports (or keep the temporary facade one extra step — see Section 1's
   recommendation). Update `test_pipeline.py`'s import list. Full suite
   green. Commit.
5. Extract `calibration_policy.py`. Update `skill/interview.py`'s
   `infer_calibration_mode` import. Full suite green. Commit.
6. Move `resolve_instrument_profile`/`resolve_osc_instrument_profile` into
   `background_color.py` (ahead of that module's own split in **Step 16**
   [round-2 review: originally misnumbered "Step 11" here, which is
   actually `luminance_selection.py` — corrected], to avoid moving them
   twice). Update the one `test_pipeline.py` import. Full suite green.
   Commit.
7. Extract `master_builder.py` (`resolve_lights`, `build_master` — renamed
   `build_group_master` per the naming-collision fix above,
   `contributor_fwhm_arcsec`, `build_single_filter_master`). **This is the
   step where the majority of Risk-1 monkeypatch retargeting happens** —
   `build_master` alone has 5+ monkeypatch sites. Grep, retarget, verify.
   **[Round-1 review: explicitly this step's job, not Step 10's]** update
   `skill/run_narrowband_boost.py`'s `build_single_filter_master` import
   (it currently imports this and `_NarrowbandNormalizingReport` together
   from `astro_pipeline.pipeline` in one statement — the other half of that
   import moved in Step 4, not here; don't let the split import block
   confuse which step owns which half). Full suite green. Commit.
8. Move `contributor_dir` into `workspace.py`. Full suite green. Commit.
9. Extract `contributor_staleness.py`. Full suite green. Commit.
10. Extract `colour_contributor.py` (`ColourContributor` +
    `ColourContributorBuilder`, replacing `_build_colour_contributor`/
    `_build_osc_colour_contributor`). Neither of `skill/run_narrowband_boost.py`'s
    two `pipeline`-imported names (`_NarrowbandNormalizingReport`,
    `build_single_filter_master`) lives here — both already handled by
    Steps 4 and 7 respectively; nothing left to update for that file at
    this step. This step changes the
    *call shape* at every call site (function call → builder-method call) —
    the highest-diff, highest-review-attention step in the whole plan. Full
    suite green. Commit.
11. Extract `luminance_selection.py`. Full suite green. Commit.
12. Extract `lrgb_orchestrator.py` (`run_lrgb` + `LRGBOrchestrator`). This
    is the biggest single step (~825 lines moving, becoming 3 methods).
    Consider splitting it into three sub-commits if the diff proves
    unreviewable in one piece: (12a) move the function verbatim into the
    new file with zero internal restructuring, re-export from `pipeline.py`,
    confirm green; (12b) introduce `LRGBOrchestrator` and move the
    "masters" phase into `_build_masters()`; (12c) move "reconciled" into
    `_reconcile()`; (12d) move "final" into `_finalize()` — each of 12b-d
    independently green-checked. This matches Section 3's Risk 3 mitigation
    (diff every `contributor_stale`/`RunSignature` call site individually)
    far better than one giant move.
13. Extract `narrowband_orchestrator.py` (`run_narrowband`). Full suite
    green. Commit.
14. Decide, and execute, the re-export-facade-vs-update-callers question
    from Section 1 for whatever's left importing from `pipeline.py` by old
    names (should be small by now). Full suite green. Commit.
15. (Optional, medium priority) Split `reconciliation.py` into a package
    per Section 2. Full suite green. Commit.
16. (Optional, medium priority) Split `background_color.py` into
    `color_calibration.py`/`background_extraction.py` per Section 2. Full
    suite green. Commit.
17. **Manual real-data verification** (Section 4c's checklist) on Kaveh's
    own machine with `ASTRO_PIPELINE_DESKTOP_DIR`/`ASTRO_PIPELINE_ITELESCOPE_DIR`
    configured. This is the actual final behavior-preservation gate, not
    Step 16's commit.

---

## 6. Out of scope, and why

- **`ingest.py` (621 lines).** Read in full via signature grep: one cohesive
  concern (discover raw files on disk, classify into `LightFrame`/
  `CalibrationFrame`/`UnrecognizedFrame`, build an `IngestReport`). Large
  because frame classification has many real-world edge cases (zip
  extraction, filename regex variants), not because it mixes unrelated
  jobs. Not named in the task brief's target list; my own read agrees it
  doesn't need splitting for this pass.
- **`calibration.py`'s internal split** (see Section 2) — real but
  low-value today; explicitly deferred, not forgotten.
- **Renaming on-disk file/directory conventions** (`master_{filter}.fit`,
  `contrib_{telescope}_bin{n}`, etc.) — Risk 5 above. This refactor moves
  code between `.py` files; it must not also "clean up" naming that touches
  real users' existing `_pipeline/` directories.
- **Changing `run_lrgb`'s or `run_narrowband`'s public signatures** — both
  are real, tested, skill-facing entry points; Task 5 is explicitly
  behavior-preserving, so their parameter lists, defaults, return types,
  and exception types stay identical. Any signature cleanup ideas that come
  up during the split belong in a future, separate, non-behavior-preserving
  change.
- **A `LuminanceSelector`/`RGBOrchestrator` class beyond what's justified
  above** — per Section 1, `luminance_selection.py` and
  `narrowband_orchestrator.py` are deliberately left as plain functions/one
  function respectively, because no class boundary there would reduce
  parameter-count or duplication. Don't force classes where functions
  already work; that would be uplevelling in name only.
- **Adding new capabilities, fixing non-blocking bugs, or improving log
  messages while moving code** — strictly out of scope for a
  behavior-preserving pass; the one exception explicitly named by the task
  brief (the `test_run_lrgb_call_sites_actually_pass_flat_frame_hash_to_contributor_stale`
  failure) turned out not to reproduce (Section 0, finding 3) — re-confirm
  before touching it, and if it's genuinely still broken, fix it as its own
  isolated commit before Step 7 (it directly touches `master_builder.py`'s
  future contents), not folded into an extraction step's diff.

---

## Open questions for adversarial review / Kaveh

1. **Facade vs. call-site-update for `pipeline.py`'s re-exports (Section
   1).** I've recommended updating the ~50 call sites directly and treating
   any facade as temporary scaffolding. This is more invasive per-step but
   more honest about what "single-responsibility modules" means. If the
   preference is lower-diff-per-step over architectural purity, flip this
   to "keep `pipeline.py` as a permanent facade" and Section 5 shortens
   somewhat (Steps 4-13 skip their "update call sites" sub-work).
2. **The pre-existing-failure claim (Section 0, finding 3) didn't
   reproduce** in this session (284 passed, 0 failed, twice — isolated and
   full-suite). I could not determine from static reading alone whether
   this was already fixed upstream of this planning session, is
   environment/ordering-sensitive, or the task brief's information predates
   a fix. Needs a human check (e.g. `git log -p` on
   `test_run_signature.py`/`pipeline.py` around this test) before deciding
   whether it needs its own pre-Step-7 fix commit at all.
3. **`LRGBOrchestrator`'s exact method boundary for "masters"** is the one
   design choice in this plan I'd most want a second pass on: the real
   masters-stage code interleaves Luminance-loop work and RGB/OSC-loop work
   with the `RunSignature` construction that depends on outputs from
   *both* loops (reference-contributor STACKCNT is only known after both
   have run). Whether `_build_masters()` should be one method or two
   (`_build_luminance()` + `_build_colour_contributors()`) feeding a
   separate `_build_run_signature()` step is a real judgment call I've
   collapsed into one method above for simplicity; a reviewer with fresher
   eyes on the STACKCNT-reference-flip logic (run_signature.py's own
   docstring calls this out as subtle) may prefer three.
   **[Round-1 review correction: the `self.` state inventory in Section 1
   is incomplete — verified by tracing the real checkpoint chain.]** Missing
   from the original list: `previous`/`previous_linear` (the checkpoint
   delta-chaining variables threading through every checkpoint call across
   all three phases — checkpoint "01"/"02" in what becomes `_build_masters()`
   seed values consumed by checkpoint "03" at the *start* of `_reconcile()`,
   feeding checkpoint "04", then checkpoint "05" in `_finalize()`), plus
   `final` (Path), `target`, and `stretch_method` (all read again inside the
   finalize block). An implementation that follows only the original
   attribute list either fails to compile or, patched around ad hoc,
   silently computes wrong checkpoint deltas without necessarily failing a
   test unless one asserts exact delta values — treat this expanded list as
   the actual punch list for Step 12, not the shorter one in Section 1.
   **[Round-2 review: still incomplete — also missing `reference_pos`]**,
   the int index into `contributors` computed at pipeline.py:1966
   (`max(range(len(contributors)), key=...)`). Separate from `reference`
   (the object): read again inside `_reconcile()`'s gain-matching loop
   (`if i == reference_pos:`, line 2190) and passed directly to
   `combine_same_grid(..., reference_index=reference_pos)` (line 2212).
   Technically re-derivable as `contributors.index(reference)` (safe since
   each contributor's `composite_path` is unique) but not named in either
   round's list — an implementer copying the stated attributes literally
   hits a missing-attribute bug in `_reconcile()`.
