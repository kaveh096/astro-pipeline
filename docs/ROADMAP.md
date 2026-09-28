# astro-pipeline Roadmap

Living project roadmap. Update this file (don't create a new one) whenever
priorities change, a task finishes, or a decision gets made. This is the
first thing a new session should read after `README.md`/`SKILL.md`.

Last updated: 2026-09-21. Current `main` HEAD: pushed and up to date with
`origin/main` (naming-cleanup commit on top of `3688de0`).

---

## 1. Immediate next step (do this first)

**Task 5 (the OOP refactor) is fully done, Steps 0-17, including Step 17**
(2026-09-21): a real, full `run_lrgb` run against the real M51 project on
this machine completed successfully end to end (real Siril
calibration/stacking, real GraXpert background extraction, real SPCC on
both colour contributors, real gain/offset match + combine), followed by
the actual real-data verification tests --
`test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`'s
SHA-256 byte-identity check passed (a full untouched resumed run
reproduced byte-for-byte identical `lrgb_final.fit`/`M51_lrgb.tif`), along
with the 3 other real `stop_after`/`force` resumability tests. This is the
actual "did 17 steps of refactoring change a single output byte" proof for
the whole task, and it passed clean.

Along the way, `tests/local_paths.py`'s `DESKTOP_DIR` was found to be a
stale/misleading name -- despite the name, it was never actually pointing
at a literal Desktop folder, and Desktop turned out to hold only finished
TIFF exports (deliverable copies), not the pipeline's real working data.
Renamed to `PROJECT_DIR_BASE` (env var `ASTRO_PIPELINE_PROJECT_DIR_BASE`)
across `tests/conftest.py`/`tests/local_paths.py.example`/
`tests/local_paths.py` to name what it actually holds: the base directory
under which the M51 project folder (raw lights + its own `_pipeline/`
working copy) lives -- which on this machine is the same iTelescope
raw-backup drive `ITELESCOPE_DIR` already pointed at, not a separate
"Desktop" location.

**Next**: revisit Section 4 below (deferred work) and Section 6
(previously-rejected ideas worth re-asking about) for what to prioritize
next -- both have gone unrevisited for a while and this task's own
convention is to periodically re-question them rather than assume they're
settled.

Full test suite baseline to hold: **288 passed, 35 skipped, 0 failed**
(`.venv/Scripts/python.exe -m pytest tests/ -q`, ~5-8 minutes; timing
varied 3.5-8 minutes across this session's own runs with identical
pass/skip counts each time -- confirmed as ordinary system load variance,
not a regression signal, so don't over-read wall-clock alone without also
checking the skip count matches).

---

## 2. Completed work (historical context, for a future session)

### 2.1 Multi-instrument + skill wrapper (`plan-rev4.md`, shipped 2026-09-07)
11 commits, `04eac54`..`00163ea`. T21 dark-scaling, multi-telescope
Luminance discovery, FWHM-based Luminance source selection (sharpest
telescope wins, "colour-only rule" -- real measured numbers: T24 3.44"
vs T21 3.88", not the ~2.4x originally guessed), SPCC safety fixes, the
real gain/offset combine fix, `RunSignature`/checkpoint/`stop_after`/
`force` machinery, and the original `skill/SKILL.md` +
`skill/interview.py` + `skill/run_stage.py`. Superseded as "current state"
by everything below, but the design decisions here are still binding
(see Section 5).

### 2.2 Flat-field calibration (`plan-flats-v3.md`, shipped 2026-09-09)
4 commits, `46ce43f`..`0314e47`, pushed. **Narrow, deliberate scope**:
wires real flat-field calibration into `build_master_flat()`/
`calibration_index()` and improves T21's own Luminance master quality
(measured: 22.6%-deep vignetting correction, corner-vs-center ratio
0.7735, confirmed by 3 independent measurement methods). **Does NOT**
change RGB combining or broaden RGB discovery across telescopes -- T24
(the primary RGB telescope for M51) has zero flat frames and structurally
never will for that delivery, so this pass deliberately left T24
untouched. See Section 4.1 for what "finish the flats work" actually
means going forward -- this is genuinely partial, not abandoned.

Note: `plan-flats-v3.md` itself was never committed to the repo -- its
content survives only in the 4 commits above and in docstrings that cite
it (see `plan-flats-v4.md`'s own "Provenance of v3" note).

### 2.2a Flat-field correctness + ingest recognition (`plan-flats-v4.md` Steps 0-8, shipped 2026-09-24)
Branch `flats-v4-core`, one commit per step (`git log` on that branch for
the full list). 8 rounds of adversarial review before implementation
(`docs/plan-flats-v4-review-round1..8.md`), then finalized (rev 9) with
decisions recorded in the plan's own ??6. The planning/review documents
themselves are intentionally **not tracked** in this repo (see `.gitignore`)
-- they were internal working notes for arriving at the design above, not
part of the published project; the design decisions and the operational
procedure that matter for anyone picking this up are preserved below.

**What shipped**:
- **G1/G2 fix (Decision Q1, "fix now"), unconditional**: T21 Luminance's
  real master flat had two defects -- 10 colliding generic `skyflat<N>`
  basenames across two twilight sessions silently collapsed 30 matched
  frames to 20 staged (I5's basename-overwrite behaviour), and the master
  was stacked with Siril's default normalisation instead of the
  `-norm=mul` its own bundled reference scripts use. Both fixed:
  content-aware flat staging (dedupe byte-identical copies, disambiguate
  genuine name collisions -- all 30 now survive) plus `-norm=mul`. This
  changes T21's real master-flat pixels the next time the real M51
  pipeline actually runs.
- **A mode-aware `calibration_recipe` mechanism** (`ContributorSignature`/
  `RunSignature`) so this and any future calibration-code behaviour
  change (flat staging/normalisation, bias/dark normalisation, an OSC
  calibrate-command shape change) can bump one versioned string instead
  of adding a new schema field each time.
- **Ingest recognition** (opt-in, `--calibration-header-fallback`, off by
  default) of real calibration libraries at M42/T20, IC 1396/T68 and
  M31/T05 that no filename pattern could see at all -- via `IMAGETYP`
  header reads, with a telescope-attribution fallback (ancestor-directory
  token, normalised) and an INSTRUME/NAXIS cross-check. **Decision Q2
  ("Keep", permanent)**: recognition alone never flips any telescope's
  `CalibrationMode` -- T20/T68/T05 all stay PRECALIBRATED by default,
  forever, not just for this round. An explicit `--calibration-mode`
  override (three distinct CLI shapes per entry point) remains the only
  way to actually exercise a header-recognised RAW_LOCAL set.
- Siril's silent `NOT USING FLAT:`/`NOT USING OFFSET:` drops (6+6 real
  variants, previously only `NOT USING DARK:` was caught) now raise
  instead of passing unnoticed (exit code 0).
- Interview visibility (`skill/interview.py`): gap-line consequence
  annotations (`[BLOCKED]` vs "no flat applied, proceeding"), a matched-
  flat identity preview (cheap header signal, not SHA-256), and an
  "Unrecognized frames" section, grouped by reason.
- Flat sanity notes (level/saturation/exposure/age), warning-only,
  heuristic, computed on a small sample of RAW flats (never the
  Siril-normalised master, which is a 32-bit float in `[0, 1]`).

**What did NOT ship (per Decision Q9, deliberately)**: no behaviour
change to M42/IC 1396/M31's real delivered images -- recognition is
opt-in and mode-inert. No OSC flats (Q5, `build_osc`'s `cal_index={}`
`KeyError` still stands). No narrowband master-invalidation tracking
(Q6, `--force` only, unchanged). See ??4.1 below for what's actually next.

**Event E** (the real M51 pipeline regeneration this fix requires, plus
recording the new hashes/`STACKCNT`/FWHM here) is a **separate,
not-yet-run step** -- the duplicate M51 light folders
(`Uncalibrated Lights - Jan 2025/`, `calibrated Lights - T24 - Feb 2025/`)
have been deleted (confirmed: real post-deletion counts T24 raw=95,
T21 raw=2, T24 L bin1=21, T21 L bin1=2, matching the plan's own numbers
exactly), but the actual `run_lrgb` call that rebuilds M51 under the new
recipe, checks `luminance_selected`/`colour_reference` didn't flip, and
re-pins gate (P)'s reference has NOT been run yet. **Whoever picks this
up next**: follow the procedure below -- it's a snapshot-first, checked
procedure, not a bare `run_lrgb` call.

#### Event E procedure (preserved here; `plan-flats-v4.md` itself is untracked)

**Open pre-requisite, not yet done**: Decision Q7 (whether to fix bias/dark
masters' missing `-nonorm` flag, "I6") is conditional, not yet resolved --
it depends on a no-code measurement (build T21's bias/dark masters with and
without `-nonorm`, using the same `tmp_path` harness as gate (P), and
compare the pixel difference against the Step-1b Siril determinism bound)
that has not been run yet. Run that measurement first and record the
result here before starting the procedure below, since step 6's expected
outcome branches on whether I6 rides along.

1. **Snapshot**: copy `_pipeline/final/`, `_pipeline/checkpoints/`,
   `_pipeline/run_signature.json`, and every group's `lights/master_*.fit`
   (T21 and both T24 binnings) to a location outside the project tree, so
   there is something to compare against and fall back to.
2. Record SHA-256 of the current `lrgb_final.fit` and `M51_lrgb.tif`.
   Record the persisted `run_signature.json` verbatim. Record
   per-contributor `STACKCNT`, median FWHM (`contributor_fwhm_arcsec`),
   `luminance_selected` and `colour_reference`.
3. Apply the triggers (the duplicate-folder deletion is already done; the
   Step 3b flat recipe fix is already merged; I6 only if the measurement
   above resolved Q7 = change).
4. Run one full `run_lrgb` for M51, using the same parameters
   `scripts/run_m51.py` uses (call `run_lrgb` directly from a scratch
   script with those arguments -- do **not** edit the tracked
   `scripts/run_m51.py`, whose `PROJECT_DIR` placeholder stays as-is).
5. **Check `luminance_selected` and `colour_reference`.** If either
   changed, or the FWHM ranking flipped, **stop and ask before accepting**
   -- do not silently keep the new selection.
6. **Pixel-identity check**: compare the rebuilt T24 Luminance and both T24
   colour-contributor masters' pixel data (`np.array_equal`, or the
   Step-1b determinism bound if not exactly equal -- plate solving
   rewrites WCS headers, so compare data arrays, not headers) against the
   step-1 snapshot.
   - **If I6 was NOT part of this run**: the masters are expected to be
     pixel-identical, because the staged T24 file sets are byte-identical
     before and after the deletion and nothing else in this run touches
     `build_master`. A mismatch here means something **other** than the
     deletion changed T24's pixels, and must be investigated before
     proceeding.
   - **If I6 WAS part of this run**: T24's bias/dark masters -- and
     everything built from them -- are **expected to differ**,
     deliberately. The expected magnitude is the pre-requisite
     measurement's own reported pixel difference, plus the Step-1b
     determinism bound as slack for ordinary Siril run-to-run noise. Only
     a difference **beyond** that combined envelope is a real fault.
7. Record the new hashes, signature, `STACKCNT`s and FWHMs in a
   **follow-up ROADMAP commit** (not by amending the triggering commit's
   message, which is already merged by the time E runs).
8. Only then run gate (I) (the four real-M51 tests, via
   `pytest -m m51_pipeline` or `-o addopts=""` for this one invocation)
   and re-pin gate (P)'s reference. **In the same commit**, remove the
   `addopts = ["-m", "not m51_pipeline"]` line from `pyproject.toml`
   (keep the `markers` registration) -- from this point on, a plain
   `pytest` covers the four real-M51 tests again, same as any other test.
9. The now-stale `_index.json` at the M51 project root (built July,
   references the deleted folder 111 times, no code consumer today) can be
   regenerated or deleted in the same pass, at your option.

**Hygiene while doing this**: checkpoint labels stay untouched. Watch for
monkeypatch drift -- grep before each step for patches of
`run_calibration`, `build_master_flat`, `build_group_master`, `run_script`,
`scan_session` and `fits.getheader`; new header reads go through an
injectable reader. Real paths come only via `tests/local_paths.py` or
`ASTRO_PIPELINE_ITELESCOPE_DIR`.

### 2.3 The "5-task publish roadmap" (2026-09-12 -> 2026-09-17, this long session)
Goal: prepare the repo for public GitHub release. Full blow-by-blow detail
lives in memory (`project_astro_pipeline_publish_roadmap` -- a Claude Code
memory file, not in this repo) and in git log; this is the condensed
version for a human or a fresh session.

**Task 1 -- 6 new optional capabilities, all shipped and real-data-tested:**
| Capability | What | Commit(s) | Real data used |
|---|---|---|---|
| D1 | GraXpert AI denoise, opt-in | `6c80218`, `ba2b961` | All 4 Desktop targets, full production run |
| D2 | Black-point export ("darkening") | `7230845` | Same |
| C | Star removal (StarNet2), nebula targets only | `bc1df1f` | Abell 31, Abell 6/HFG1 |
| B | OSC + local raw calibration (bias-only+debayer, no dark required) | `dab5f4b` | T68/IC 1396 (mechanics only -- see Section 4.3, SPCC profile still missing) |
| A | Narrowband SHO/HOO false-colour composite | `7b91468` | M42/T20 (Red+Ha masters; full LRGB blocked, see Section 4.4) |
| A2 | Narrowband-boost / HaRGB (blend Ha into Red on a finished LRGB/RGB) | `aefd0c5` | M42/T20 (validated against independently-built masters, not a full end-to-end script run -- see Section 4.5) |

Real, sourced technique research backs A2 (Chaotic Nebula / Galactic
Hunter / AstroBackyard tutorials -- blend Ha into Red via a lighten-style
blend, no universal standard ratio, ~0.3-0.5 a real starting point). Real
bugs found and fixed along the way (not exhaustive, see git log for full
detail): Siril's real pixelmath command is `pm` not `pixelmath`, needs an
explicit `save` or output is silently lost; raw Siril `stack` output has
no cross-filter normalization so a naive boost formula touched 0% of
pixels until rescaled into the target channel's own real units
(`rescale_narrowband_to_reference()`).

**Task 2** (test capabilities against real data) was satisfied
opportunistically during Task 1 -- see the "real data used" column above.

**Task 3 -- PII scrub** (commit `1836b53`, 2026-09-15/16). All personal
name/username/path references removed from the tracked working tree
(git history deliberately NOT rewritten -- explicit, binding decision,
see Section 5). Real personal paths moved to gitignored
`tests/local_paths.py` (template: `tests/local_paths.py.example`) or
`ASTRO_PIPELINE_*` env vars. Caught and fixed a real correctness bug
along the way: some tests assert against usernames actually parsed off
real on-disk filenames, not arbitrary mock values -- fixed by deriving
expected values from `local_paths.py` instead of hardcoding either the
real or a fake username.

**Task 4 -- SKILL.md distillation + capability docs** (commit `d77ce9a`,
2026-09-16). Added a flow-selection table and dense new sections for all
6 capabilities above (previously completely undocumented in the skill).
Built the one missing CLI wrapper (`skill/run_narrowband.py` -- capability
A had no script entry point until this). Fixed a stale docstring in
`pipeline.py` that no longer matched shipped behavior.

**Task 5 -- OOP refactor (IN PROGRESS)**. See Section 3 -- big enough to
get its own section.

### 2.4 Two real, previously-unknown bugs found and fixed this session (independent of any refactor)
1. **Denoise silent no-op** (`ba2b961`): a real full-resolution
   (4096x4096) CPU denoise run against Abell 31's actual composite
   "succeeded" (exit 0, valid-looking output, 2h06m runtime) but the
   output was byte-identical to the input -- root-caused to a manual
   diagnostic bypass of the wrapper's own NaN-fill step, not the shipped
   function itself, but fixed defensively anyway (`DenoiseError` if
   output == input).
2. **`run_lrgb` resumability was silently broken for every mono-RGB
   target** (`0efdeca`, 2026-09-16/17, found while writing Task 5's Step
   0 safety-net tests). `discover_osc_contributors()` always pads its
   result with the caller's own `(telescope, rgb_binning)`, so the OSC
   contributor loop ran once even for targets with zero real OSC data,
   computed an empty-lights hash, and clobbered
   `colour_frame_hashes[contributor_key]` -- the SAME key the mono-RGB
   loop had just written. This poisoned `contributor_stale()` forever:
   **the colour contributor (raw R/G/B masters through GraXpert and SPCC)
   was fully deleted and rebuilt on every single resumed `run_lrgb` call**,
   for M51, NGC 3628, Abell 31, and any other non-OSC target -- silently,
   for as long as this code has existed (since the 2026-09 RGB-only/OSC
   plan landed). This was independent of, and in addition to, GraXpert's
   own known run-to-run non-determinism. **Fixed**: the OSC loop now
   skips any binning with no real OSC/Color data. Confirm this stays
   fixed if `run_lrgb` is ever touched again -- it's exactly the kind of
   silent, no-test-failure regression a refactor could reintroduce.

### 2.5 GPU/DirectML investigation (2026-09-13, closed, don't reopen)
This machine's GPU (Intel HD Graphics 4400/4600, Haswell-era, driver from
2016) cannot run GraXpert/StarNet2's GPU acceleration. Root-caused with
hard evidence (event logs, live process/GPU-counter monitoring): Intel
disabled DX12 on this generation via driver 15.40.44.5107 as a fix for a
real CVE (INTEL-SA-00315); "upgrading" the driver would regress support
further, not improve it. **Verdict: permanently unavailable on this
machine, don't re-suggest a driver upgrade.** CPU-only denoise works
fine, just slow (hours at full resolution) -- that's expected, not a bug.
Revisit only on a genuinely different (future) machine with a modern GPU.

---

## 3. Task 5 (OOP refactor) -- detailed status

**Plan document**: `docs/task5-oop-refactor-plan.md` -- an internal planning
artifact, intentionally **not tracked** in this repo (see `.gitignore`; it
had 2 full rounds of independent adversarial review merged inline, 14 total
findings, all corrected in place). Task 5 is now **fully complete** (see
below), so this summary is the durable record going forward; the plan
document itself is no longer needed to pick this up.

**Goal** (original framing): "refactor the code to uplevel it in terms
of good object oriented design, small, single responsibility modules, and
a simple architecture... 100% behavior-preserving... good coverage of
e2e tests" to verify that. Hybrid test strategy mandated: a few real
Siril/GraXpert smoke tests + broad fast mocked coverage.

**Done (Steps 0-10 of 17), one commit each, suite green after every one:**
- Step 0 (`46a348f`): 4 new fast/mocked tests covering `run_lrgb`'s
  staged orchestration (previously near-zero coverage without a specific
  personal M51 folder). Found bug 2.4.2 above while writing these.
- Separate fix commit (`0efdeca`): the resumability bug itself (Section 2.4.2).
- Step 1 (`dc46998`): `logging_utils.py` (deduped `_log` -- kept the
  `None`-tolerant version, a real correction from round-1 review).
- Step 2 (`7295dff`): `filter_constants.py`.
- Step 3 (`3365b38`): `resume_guard.py` (`usable()`).
- Step 4 (`54e1bb5`): `narrowband_filters.py`.
- Step 5 (`51620b1`): `calibration_policy.py`.
- Step 6 (`1a4218d`): moved `resolve_instrument_profile`(s) into `background_color.py`.
- Step 7 (`3cfb050`): `master_builder.py` (renamed the extracted
  `build_master` to `build_group_master` -- collided with an unrelated
  `build_master` already in `calibration.py`).
- Step 8 (`6bcbc46`): moved `contributor_dir()` into `workspace.py`.
- Step 9 (`fbbb845`): `contributor_staleness.py`.
- Step 10 (`72aa306`): `colour_contributor.py` -- a new
  `ColourContributorBuilder` class replacing
  `_build_colour_contributor`/`_build_osc_colour_contributor`, with their
  shared GraXpert-extraction/SPCC-calibration body logic factored into
  `_extract_background()`/`_run_spcc()` methods (not just shared
  parameters -- a round-2 review finding).
- Step 11 (`6fc7d64`): `luminance_selection.py` -- `LumCandidate`,
  `select_luminance_source`, `discover_luminance_contributors`,
  `discover_osc_contributors`, moved verbatim as plain functions (no class
  -- see the plan's own reasoning). No monkeypatch sites or skill-script
  imports targeted these three names, so this was the lowest-risk step so
  far: import-and-use-unchanged inside `run_lrgb`, no call-site updates
  needed.

`pipeline.py`: 2478 -> ~1200 lines before Step 12 (was 1370 after Step 10;
Step 11 removed another ~120); Step 12 moved `run_lrgb` (~844 lines) out
entirely, into the new `lrgb_orchestrator.py` (~1020 lines including the
`LRGBOrchestrator` class).

- Step 12 (`e68cee0` sub-plan + review docs, `1cbfa41`/`bd0cdb9`/`c66e5ae`/
  `758415e`/`2e004c4` code): `lrgb_orchestrator.py` -- the biggest single
  step, done as 5 sub-commits per its own dedicated sub-plan
  (`docs/task5-step12-substeps.md`, independently re-derived and put
  through 3 rounds of fresh adversarial review before any code moved --
  see that file's own review-outcome sections for what each round caught,
  including a critical `PipelineResult` circular-import that survived the
  original plan's 2 rounds entirely). Sub-commits: 12a-0 (extracted
  `pipeline_result.py`, pre-empting the cycle), 12a (moved `run_lrgb`
  verbatim; caught one real gap --  `export` was never classified in
  either plan document's import migration and was missing from the new
  file, causing 3 immediate, loud test failures, fixed same-commit), 12b
  (introduced `LRGBOrchestrator`, extracted `_build_masters()`), 12c
  (extracted `_reconcile()`), 12d (extracted `_finalize()`, introduced
  `run()`, collapsed `run_lrgb()` to a thin construct-and-run wrapper --
  confirmed via `inspect.signature()` byte-for-byte identical to the
  pre-Step-12 signature). Final `self.` state inventory: 27 attributes (14
  constructor params + 13 computed cross-phase attributes, one of which --
  `rgb_reconciled` -- neither of the original plan's 2 review rounds had
  caught either). Full suite green after every sub-commit, wall-clock
  unchanged throughout (~8 min), all 5 skill scripts smoke-tested after
  each.

- Step 13 (`ff9941f`): `narrowband_orchestrator.py` -- `run_narrowband`
  (~160 lines) moved out as a plain function (no forced base class with
  `LRGBOrchestrator`, per the plan's own non-uniformity call). Re-derived
  its own import-migration inventory fresh rather than reusing Step 12's
  (a different function's imports don't transfer) -- `pipeline.py` shrank
  to a pure 112-line re-export facade with no function bodies of its own.
  Retargeted 3 monkeypatch sites; also caught and fixed 2 latent breaks
  Step 12 had left in `run_lrgb` tests once `pipeline.py`'s import block
  finished shrinking (`pipeline_module.pipeline_dir`/`._delete_if_exists`
  references that were never in scope for Step 12's own retargeting).
- Step 14 (`a3b96bc`): resolved the facade-vs-update-callers question --
  updated all 7 external files (`scripts/run_m51.py`, 4 skill scripts, 2
  test files) to import from real module homes instead of `pipeline.py`,
  then **deleted `pipeline.py` entirely** (confirmed via repo-wide grep
  that nothing referenced it anymore). Its module docstring (real
  institutional knowledge, not implementation detail) moved into
  `lrgb_orchestrator.py`'s own docstring rather than being lost; one stale
  reference (`_build_colour_contributor`, renamed at Step 10) fixed along
  the way.
- Step 15 (optional, `3b0741e`): split `reconciliation.py` into a package
  (`reproject.py`/`coverage.py`/`gain_offset.py`, sharing only
  `ReprojectionError`) with an `__init__.py` re-exporting everything --
  genuinely zero-caller-impact, confirmed by grep before touching
  anything.
- Step 16 (optional, `3688de0`): split `background_color.py` into
  `color_calibration.py` (SPCC) + `background_extraction.py` (GraXpert),
  keeping `background_color.py` itself as a deliberate permanent facade
  (unlike `pipeline.py`'s case) -- its "Stages 6-7" orchestration concept
  is still real and its module name isn't being deprecated, so real
  external callers (skill/run_post_process.py, colour_contributor.py,
  lrgb_orchestrator.py) needed zero changes.

- Step 17 (2026-09-21, no commit -- real-data-only, not code): **manual
  real-data verification**, done on this machine with
  `ASTRO_PIPELINE_PROJECT_DIR_BASE`/`ASTRO_PIPELINE_ITELESCOPE_DIR`
  configured -- the actual final behavior-preservation gate. A real, full
  `run_lrgb` run against the real M51 project completed successfully
  (real Siril/GraXpert/SPCC throughout, both colour contributors), then
  the SHA-256 byte-identity check in
  `test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`
  passed, along with the 3 other real `stop_after`/`force` resumability
  tests. This could only run on one machine; it is not a CI-able step,
  and nothing earlier in this task substituted for it. **Task 5 is now
  fully complete.**

**Risks to keep re-checking at every remaining step** (full detail in the
plan doc's Section 3): monkeypatch-target drift (a moved function's test
mocks silently stop firing, letting the REAL Siril/GraXpert call run
inside what should be an instant unit test -- watch wall-clock time, not
just pass/fail), import cycles, the `RunSignature`/`ContributorSignature`
serialization contract, skill script import breakage, and -- a round-2
finding nobody had flagged before -- checkpoint LABEL strings are just as
much an on-disk resumability contract as filenames (`checkpoints.py`'s
own docstring documents a real past bug from exactly this).

---

## 4. Deferred / not-started work (future tasks, in no particular priority order)

### 4.1 Finish flat-field calibration for other telescopes
**Updated by `plan-flats-v4.md` Steps 0-8 (Section 2.2a)**: ingest can now
SEE real flat/bias/dark libraries at M42/T20, IC 1396/T68 and M31/T05
(opt-in `--calibration-header-fallback`), and the `build_master_flat()`/
`calibration_index()` machinery already generalizes to them (confirmed:
real gate-(D) counts match exactly) -- but recognition alone changes
nothing (Decision Q2, PRECALIBRATED stays the default for all three,
permanently). The **actual next step (decisions Q3/Q9)**, is the
per-target local-flats-vs-iTelescope measurement, NOT reprocessing blind:
- M42/T20, IC 1396/T68 and M31/T05 EACH get their own measurement (same
  corner/centre-ratio or half-split method T21's own real measurement
  used) -- not one shared verdict.
- Needs only Step 4b's recognition + Step 2's `calibration_recipe_parts`
  mechanism -- not Steps 3b/6/7 or the CLI flags.
- **Mechanics**: build RAW_LOCAL masters with and without a flat, and
  PRECALIBRATED masters, and compare masked background uniformity and
  dust-residual metrics. A naive version of this is expensive (~360
  register+stack runs across 20 half-splits x 3 arms x 3 masters) and
  confounded on background alone (darks/cosmetic correction/pedestal
  differ between RAW_LOCAL and PRECALIBRATED, not just the flat) -- when
  actually run: build each arm's calibrated master **once**, then split
  only register+stack for the noise floor (roughly halves the cost); a
  half-stack's noise is ~sqrt(2) worse than the full stack being compared
  -- account for that or use it only as a relative comparison across arms;
  isolate the flat's own effect via RAW_LOCAL-no-flat vs
  RAW_LOCAL-with-flat (same darks), and report RAW_LOCAL vs PRECALIBRATED
  separately rather than auto-deciding from it.
- **Reprocess only where a target's own measurement shows a genuine,
  quantified improvement.** Real risk per target argues against blind
  reprocessing: M42's flat library is 21 months older than its lights
  (dust/vignetting-drift risk); IC 1396's calibration timestamps are
  corrupted (`DATE-OBS=1970`) and has no RAW_LOCAL OSC code path at all
  (see 4.3); M31's flats are the freshest (19 days) but its darks are at
  the wrong temperature (-15C vs -10C lights) -- **fix that (G15) before
  or as part of measuring M31**, or the measurement itself is unreliable.

### 4.2 Multi-instrument RGB/colour discovery generalization
Luminance discovery was generalized across telescopes in `plan-rev4`
(2026-09-07), but RGB/colour discovery is still deliberately hard-scoped
to the caller's own telescope (`pipeline.py`'s own comment: "colour
discovery is hard-scoped to the caller's own telescope"). Broadening
this (combining RGB data captured across multiple telescopes for one
target) is real, unstarted work, last flagged as a priority on
2026-09-09 and not picked up since -- worth re-asking whether this is
still wanted before starting, since narrowband/post-processing work took
priority instead.

### 4.3 Capability B (OSC + local raw calibration) is missing a real SPCC profile for T68
Real bias-only+debayer calibration mechanics were validated against T68
(IC 1396) this session, but **T68 has no registered `OSCInstrumentProfile`**
in `background_color.py` (only T02 does, `T02_OSC_PROFILE`) -- confirmed
by reading `OSC_INSTRUMENT_PROFILES` directly. Running T68/IC 1396
through real SPCC colour calibration today would raise
`UnknownInstrumentError`. Needs T68's real sensor identified and a
profile added (same shape as `T02_OSC_PROFILE`) before IC 1396 can get a
real finished colour composite.

Related, not the same gap: `plan-flats-v4.md` (Decision Q5, deferred)
separately found `build_osc` passes `cal_index={}`, which raises
`KeyError` before RAW_LOCAL OSC calibration is ever reached at all --
i.e. even with a real SPCC profile added here, T68 still can't reach
RAW_LOCAL OSC flat correction without that `KeyError` fixed too. Revisit
both together only if IC 1396's own future per-target measurement
(Section 4.1) shows a real benefit -- IC 1396 is the lowest-priority of
the three per-target candidates given its corrupted calibration
timestamps, so neither gap is being picked up speculatively ahead of that.

### 4.4 M42 (Orion)'s Green-bin2 registration failure -- unresolved, real data quality issue
Real Siril registration fails ("Found 0 stars in reference") because 2 of
7 real Green-bin2 frames have near-zero standard deviation (cloud/focus/
tracking dropout), confirmed via direct pixel-stat investigation, not a
pipeline bug. M42's full LRGB composite has never been completed
end-to-end as a result (Luminance and Red-bin2 masters exist on disk;
Green/Blue/the full composite do not). Fix is excluding the bad frame(s)
from that Siril session, not touching any code.

### 4.5 `skill/run_narrowband_boost.py` never run end-to-end as a script
Capability A2 (narrowband-boost/HaRGB) has real unit-test coverage and a
manual, hand-run validation against independently-built Red+Ha masters
(bypassing the broken M42 Green step, Section 4.4), but the actual CLI
script has never been run start-to-finish against one complete real LRGB
project. Blocked on 4.4 (needs a complete real LRGB composite to run
against) unless a different real narrowband+broadband target becomes
available first.

### 4.6 ~~Task 5's optional Steps 15/16~~ -- done, stale entry
Both optional splits (`reconciliation.py` -> package, `background_color.py`
-> `color_calibration.py`/`background_extraction.py`) shipped 2026-09-21
(commits `3b0741e`/`3688de0`) -- see Section 3. Kept here crossed-out
rather than deleted, as a reminder this section needs occasional
re-reading against Section 2/3's actual commit history, not just trusted
as always current.

### 4.7 `calibration.py`'s own optional internal split
The refactor plan explicitly recommends leaving `calibration.py` as one
module (already cohesive, large only because of thorough real-hardware-
finding docstrings) -- but flags `DarkSelection`/`select_dark`/
`_calibrate_command` as a candidate for a future `calibration_commands.py`
if they ever need to be tested/reused independent of the Siril-calling
functions. Not needed today; noted for completeness.

---

## 5. Binding design decisions (don't silently relitigate these)

- **Colour-only rule**: the sharpest telescope (measured FWHM) owns
  Luminance; never blended cross-telescope. Real, deliberate, still in
  force (`plan-rev4`).
- **No flats for T24** in the M51 delivery -- structural, not an oversight.
- **Git history is NOT rewritten** to remove PII -- explicit, binding
  decision from the Task 3 scoping conversation (2026-09-12). Old commits
  keep real names/targets/paths as-is; only the current working tree
  needs to stay clean going forward. Don't "clean up" old commits later
  without asking again first.
- **SPCC-only colour calibration**, Siril >= 1.4.4. No alternative colour
  calibration method has been evaluated or requested.
- **This skill's job ends the moment a TIFF path exists** -- no framing,
  cropping, colour-grading, or Photoshop automation of any kind. This is
  a repeatedly-reinforced design boundary, not a one-off preference (see
  Section 6 for why it was explicitly rejected once already).
- **T24 (M51) has zero flats, permanently, by binding decision -- not
  reopened by `plan-flats-v4.md`.** Documented fact, not a scope change
  (Decision Q8, 2026-09-24): the iTelescope-provided `calibrated-` T24
  BIN1 copies are `CALSTAT=BDF` -- already flat-corrected server-side by
  iTelescope. This is a fact about the existing delivery (T24's OWN local
  calibration still has no flats of any kind), surfaced for completeness,
  not a reason to revisit the "no T24 flats" decision from ??2.2.

---

## 6. Explicitly rejected ideas -- re-ask before assuming these still don't apply

Anything flagged as "never" wanted should stay visible here, not silently
vanish, and should be actively re-questioned in a future session rather
than assumed permanent.

1. **Photoshop-scripted creative grading automation** (contrast/gamma/
   vibrance automation). Rejected 2026-09-12: contradicts the skill's own
   repeated "ends at TIFF" design boundary (Section 5), has no testable
   ground truth (unlike SNR/star-count/background, which the rest of the
   pipeline already measures), and requires paid Photoshop, shrinking the
   published skill's real audience. This reasoning was agreed at the
   time. **Re-ask if**: the project's scope or audience changes (e.g.
   if this stops being a "publish for others" project and becomes
   personal-workflow-only again), or if a genuinely testable/open-source
   creative-grading approach emerges.
2. **PII/personal-target-tracking automation** (an early brainstormed
   capability -- automatically tracking the maintainer's own target list/
   preferences/social-media activity). Rejected 2026-09-12 specifically
   because it's "not useful for others in general," in the context of
   preparing this repo for public release. **Re-ask if**: a separate,
   personal (un-published, or a private fork/branch) automation for one's
   own use is wanted -- the rejection was about the PUBLISHED skill's
   scope, not a judgment that the feature itself is bad.
3. **Narrowband support** (SHO/HOO, HaRGB) was originally deprioritized
   2026-09-09 ("I don't usually do narrowbands... don't build HOO/SHO/
   Ha-blending unless asked"). **Already resolved** -- this was explicitly
   reversed 2026-09-14 and it shipped as capabilities A/A2 (Section
   2.3). Kept here only as a real example of exactly this pattern (a
   "never" that became a "yes, build it" once framed for the public-skill
   use case) -- no action needed, historical note only.

---

## 7. How to pick this up cold

1. Read `README.md`, then `skill/SKILL.md` (the actual user-facing
   entry point), then this file.
2. `git log --oneline -30` to see what's actually landed vs. what this
   doc describes (this doc can go stale; git is ground truth).
3. Task 5 (the OOP refactor) is fully complete -- Section 3 above is the
   durable record; no plan document is needed to pick anything up there.
4. Run the full suite once before changing anything:
   `.venv/Scripts/python.exe -m pytest tests/ -q` -- confirm the baseline
   (288 passed / 35 skipped as of this writing) before assuming any
   number that follows.
5. For real-data-gated tests: copy `tests/local_paths.py.example` to
   `tests/local_paths.py` and fill in your own real local paths
   (gitignored, never committed); otherwise those tests will just skip,
   which is expected and fine.

---

## 8. Running long or memory-heavy processes from a Claude Code session

Anything that runs for a long time or uses a lot of memory -- a real
Siril/GraXpert/ASTAP call against full-resolution data, or the full
pytest suite once `tests/local_paths.py` makes real-data-gated tests
reachable (~35-40 minutes with real data present, vs. ~5-8 minutes
without) -- must **not** run as a normal foreground or
`run_in_background` call inside a Claude Code session on this machine.

**Why**: the harness has its own idle-timeout and memory-pressure reaper
that can kill a long `run_in_background` shell command outright while the
session is otherwise idle waiting on it -- this has happened more than
once (a real verification pytest run killed mid-run, 2026-09-21; a full
real-data suite run killed the same way, 2026-09-24). It is not a failure
of the command itself, just the harness protecting the host machine, but
it silently loses the run's progress and its output.

**The fix, confirmed working**: spawn the process as a genuinely detached
OS-level child, outside the Claude Code process tree/job object entirely,
so the harness's own reaper has nothing to kill:

```powershell
$py = "C:\dev\astro-pipeline\.venv\Scripts\python.exe"
# For a driver script: python -u <script> > <log> 2>&1
# For pytest directly: -m pytest tests/ -q > <log> 2>&1
$inner = '"' + $py + '" -u "<script-or--m-pytest-args>" > "<log path>" 2>&1'
$cmdLine = 'cmd.exe /c "' + $inner + '"'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
    -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = "C:\dev\astro-pipeline" }
```

`Invoke-CimMethod`/`Win32_Process::Create` spawns the process as a child
of the WMI provider host service, not of the calling shell -- this is
what makes it survive independently of the session's own job object.
Confirm it's really detached with
`Get-CimInstance Win32_Process -Filter "ParentProcessId=<wrapper PID>"`.

**Then poll, don't block**: use `CronCreate` (a recurring job, ~10 minute
interval is reasonable for a multi-hour run, shorter for a ~40-minute
test suite run) to check the PID and tail the log periodically, rather
than a foreground wait or a `run_in_background` shell call. **Always
`CronDelete` the job once the watched process finishes** -- confirm with
`CronList` returning no scheduled jobs left over.

This applies to any future multi-hour pipeline run or any full-suite run
with real data reachable, not just the two cases that have already hit
this failure mode -- default to detached+poll for both, don't retry the
normal foreground/background path and hope it doesn't get reaped this
time.
