# astro-pipeline Roadmap

Living project roadmap. Update this file (don't create a new one) whenever
priorities change, a task finishes, or a decision gets made. This is the
first thing a new session should read after `README.md`/`SKILL.md`.

Last updated: 2026-09-17. Current `main` HEAD: `6fc7d64` (1 commit ahead
of `origin/main` -- **not yet pushed**, see "Immediate next step" below).
The prior handoff's "25 commits ahead, not pushed" was stale -- those 25
were already pushed by the time this session picked the work back up;
only Step 11 (below) is new since then.

---

## 1. Immediate next step (do this first)

1. Push everything: `git push origin main` (1 commit, nothing destructive
   -- this is a fast-forward, no force needed. Verify with `git status`/
   `git log origin/main..main` first as usual before pushing).
2. Resume **Task 5** (the OOP refactor) at **Step 12** of
   `docs/task5-oop-refactor-plan.md` -- the biggest, highest-risk step in
   the plan (`lrgb_orchestrator.py`). Read that file in full, including
   its inline `[Round-1/Round-2 review]` correction annotations (authoritative
   over the plan's original base text wherever they disagree) and
   especially "Open questions" item 3 (the `self.` state inventory for
   `LRGBOrchestrator`, wrong twice already across 2 review rounds) before
   touching any code. Steps 0-11 are done (see Section 2 below); Steps
   12-17 remain. Consider a fresh, independent adversarial review pass
   before/after Step 12 specifically, the same way the original plan was
   reviewed -- see the plan's own recommendation.
3. Full test suite baseline to hold after every step: **288 passed, 35
   skipped, 0 failed** (`.venv/Scripts/python.exe -m pytest tests/ -q`,
   ~5-8 minutes). If this number ever changes unexpectedly, stop and
   understand why before continuing -- don't assume it's fine.

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

**Plan document**: `docs/task5-oop-refactor-plan.md` (moved here from a
session scratchpad this session -- previously untracked, now permanent).
Read it in full before continuing; it has 2 full rounds of independent
adversarial review merged inline (14 total findings, all corrected in
place) and is the actual source of truth for the remaining steps, not
this summary.

**Goal** (Kaveh's own framing): "refactor the code to uplevel it in terms
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

`pipeline.py`: 2478 -> ~1200 lines so far (was 1370 after Step 10; Step 11
removed another ~120).

**Remaining (Steps 12-17), per the plan document:**
- Step 12: `lrgb_orchestrator.py` -- the biggest single step (~800 lines,
  `run_lrgb` -> an `LRGBOrchestrator` class with `_build_masters()`/
  `_reconcile()`/`_finalize()` methods). The plan recommends splitting
  this into 4 sub-commits (12a-d) if the diff proves unreviewable in one
  piece. **Read the plan's "Open questions" section 3 and its round-2
  correction carefully before starting this step** -- the required
  shared-state inventory for the orchestrator class (what becomes
  `self.` attributes) was wrong twice already across 2 review rounds
  (missing `previous`/`previous_linear`/`final`/`target`/`stretch_method`
  after round 1, then `reference_pos` too after round 2) -- don't trust
  any single version of that list without re-deriving it from the real
  checkpoint-chain code yourself.
- Step 13: `narrowband_orchestrator.py` (`run_narrowband` -- stays a
  plain function, no forced base class with `LRGBOrchestrator`).
- Step 14: resolve the facade-vs-update-callers question for whatever's
  left importing old names from `pipeline.py` (the plan recommends
  updating callers directly, not a permanent re-export facade).
- Step 15 (optional): split `reconciliation.py` into a package
  (reprojection / coverage-cropping / gain-offset -- 3 genuinely
  independent algorithms, no external caller needs to change).
- Step 16 (optional): split `background_color.py` into
  `color_calibration.py` (SPCC) + `background_extraction.py` (GraXpert).
- Step 17: **manual real-data verification** on Kaveh's own machine
  (`ASTRO_PIPELINE_DESKTOP_DIR`/`ASTRO_PIPELINE_ITELESCOPE_DIR`
  configured) -- the actual final behavior-preservation gate, especially
  the SHA-256 byte-identity check in
  `test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`.
  This can only run on one machine; it is not a CI-able step.

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
Only T21 has real flat frames wired in (Section 2.2). T24 structurally
never will for the M51 delivery, but this was never revisited for other
telescopes as new data comes in (T73, T59, T02, T68, or any future
telescope). If/when flat frames exist for one of those, the
`build_master_flat()`/`calibration_index()` machinery already generalizes
-- confirm by reading `plan-flats-v3.md`'s own commits (`git log
46ce43f..0314e47`) for what's already generic vs. T21-specific before
assuming a from-scratch design is needed. Kaveh: **explicitly asked to
keep this on the roadmap, wants to get to it eventually.**

### 4.2 Multi-instrument RGB/colour discovery generalization
Luminance discovery was generalized across telescopes in `plan-rev4`
(2026-09-07), but RGB/colour discovery is still deliberately hard-scoped
to the caller's own telescope (`pipeline.py`'s own comment: "colour
discovery is hard-scoped to the caller's own telescope"). Broadening
this (combining RGB data captured across multiple telescopes for one
target) is real, unstarted work, last flagged as a priority on
2026-09-09 and not picked up since -- worth asking Kaveh whether this is
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

### 4.6 Task 5's optional Steps 15/16 (reconciliation.py / background_color.py splits)
Deferred by the plan itself as lower-priority than the `pipeline.py`
split -- see Section 3.

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

---

## 6. Explicitly rejected ideas -- re-ask before assuming these still don't apply

Per Kaveh's own instruction: anything he's said he'll "never" want should
stay visible here, not silently vanish, and should be actively
re-questioned in a future session rather than assumed permanent.

1. **Photoshop-scripted creative grading automation** (contrast/gamma/
   vibrance automation). Rejected 2026-09-12: contradicts the skill's own
   repeated "ends at TIFF" design boundary (Section 5), has no testable
   ground truth (unlike SNR/star-count/background, which the rest of the
   pipeline already measures), and requires paid Photoshop, shrinking the
   published skill's real audience. Kaveh agreed with this reasoning at
   the time. **Re-ask if**: the project's scope or audience changes (e.g.
   if this stops being a "publish for others" project and becomes
   personal-workflow-only again), or if a genuinely testable/open-source
   creative-grading approach emerges.
2. **PII/personal-target-tracking automation** (an early brainstormed
   capability -- automatically tracking Kaveh's own target list/
   preferences/Instagram activity). Rejected 2026-09-12 specifically
   because it's "not useful for others in general," in the context of
   preparing this repo for public release. **Re-ask if**: Kaveh wants a
   separate, personal (un-published, or a private fork/branch) automation
   for his own use -- the rejection was about the PUBLISHED skill's
   scope, not a judgment that the feature itself is bad.
3. **Narrowband support** (SHO/HOO, HaRGB) was originally deprioritized
   2026-09-09 ("I don't usually do narrowbands... don't build HOO/SHO/
   Ha-blending unless asked"). **Already resolved** -- Kaveh explicitly
   reversed this 2026-09-14 and it shipped as capabilities A/A2 (Section
   2.3). Kept here only as a real example of exactly this pattern (a
   "never" that became a "yes, build it" once framed for the public-skill
   use case) -- no action needed, historical note only.

---

## 7. How to pick this up cold

1. Read `README.md`, then `skill/SKILL.md` (the actual user-facing
   entry point), then this file.
2. `git log --oneline -30` to see what's actually landed vs. what this
   doc describes (this doc can go stale; git is ground truth).
3. For Task 5 specifically: read `docs/task5-oop-refactor-plan.md` in
   full, including its inline review-correction annotations, before
   touching any source file.
4. Run the full suite once before changing anything:
   `.venv/Scripts/python.exe -m pytest tests/ -q` -- confirm the baseline
   (288 passed / 35 skipped as of this writing) before assuming any
   number that follows.
5. For real-data-gated tests: copy `tests/local_paths.py.example` to
   `tests/local_paths.py` and fill in real local paths (gitignored,
   never committed) if working on Kaveh's own machine; otherwise those
   tests will just skip, which is expected and fine.
