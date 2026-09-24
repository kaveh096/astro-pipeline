# plan-flats-v4: adversarial review, round 3

Reviewer scope: `docs/plan-flats-v4.md` rev 3, checked against `main` @
`22cdf19` (plus the dirty `conftest.py`/`local_paths.py.example`), the
round-1/2 reviews and the review log, and the read-only data under
`D:\Raw Photo Backups\iTelescope\`.

- **Not run**: Siril, GraXpert, the pipeline, `scan_session`, or any test.
- **Evidence**: code reads; `classify_filename`/`classify_frame` plus
  zip-name peeks over the M51 tree (read-only, no extraction); astropy
  header reads; `frame_identity_hash` recomputations; persisted
  `run_signature.json` files. Scripts are in the session scratchpad.

**Checked and correct**:
- The T21 duplicate hash: current tree `[a,b,a,b]` gives `83bc8bee12b82922`
  (matches persisted); post-deletion gives `c2b76b333110b4a6`.
- No header-fallback leak on M51 today. Its only unrecognised FITS are the
  11 `IMAGETYP=FLAT, CALSTAT=M` masters, which Step 4 rejects.
- The persisted T21 `flat/` was built on 2026-09-21 with current code
  (after the last `calibration.py` change on 09-17), so gate (P) has a
  valid reference.
- Every persisted colour/Luminance `flat_frame_hash` on Abell 31, Abell 6
  and NGC 3628 is `""`. The G10 fix is therefore behaviour-free on real
  data today.
- The M31 flats are one filter per session folder, with no colliding
  basenames (120 files, 0 duplicate names). T21 L is the only live
  collision case.
- `stage_frames` always `rmtree`s and restages, so a stale
  `flat/master.fit` cannot be reused after a recipe change.

---

## Findings

### R3-1 (major): the per-step "full suite" protocol triggers event E early, and today's real-data suite is not green

- **Evidence**:
  - On Kaveh's machine, `tests/local_paths.py` sets `PROJECT_DIR_BASE`
    to the D: tree (dirty `conftest.py`). The full suite therefore runs
    the four `_run_m51` tests (`test_pipeline.py:1346-1418`), which is
    gate (I).
  - After the Step-0 deletion those tests make every M51 contributor
    stale (R3-2) and run a full rebuild with pre-3b code. That is exactly
    what §4.0 forbids ("do not run any real M51 pipeline test"). Yet
    §4.0 and Step 0 both say to run the full suite before and after each
    step.
  - Before the deletion, the existing real ingest tests should fail on the
    current tree. `test_scan_real_multi_telescope_session` asserts T24 raw
    = 95 and T21 = 2; `test_instrument_groups_merges_users…` asserts
    T24 L = 21. By my count (not run) the tree gives 190, 4 and 42. So
    the Step-17 run on 09-21 cannot have been a green full suite with
    real data. It ran the 4 resumability tests.
  - Those tests already encode the post-deletion counts. That is
    independent evidence for the deletion, and Step 1a partly duplicates
    them.
- **Fix**:
  - State the suite protocol explicitly. Until E, run
    `pytest --deselect` on the four `_run_m51` tests, or add an
    `m51_pipeline` marker and use `-m "not m51_pipeline"`. After E, run
    the full suite.
  - Record two baselines in Step 0: unconfigured (288/35) and configured
    with M51 deselected.
  - Note that the three existing ingest tests turn green on deletion.
  - Have Step 1a extend them instead of re-pinning the same counts.

### R3-2 (major): the deletion changes every T24 hash, so E rebuilds the delivered image, and it has no rollback and no oracle

- **Evidence**: I recomputed from filenames, current tree → post-deletion:

  | Group | Current | Post-deletion |
  |---|---|---|
  | T24 L BIN1 | 42 names, `d2f430e6232fd176` (= persisted) | 21, `de55083822298fad` |
  | T24 R/G/B BIN1 and BIN2 | all doubled | all change |

  - Both colour contributors (`T24_bin1`, `T24_bin2`) and the **selected**
    Luminance go stale. §1.5 says "very likely"; it is certain.
  - E rebuilds every M51 master through Siril/GraXpert/SPCC. It is not
    just T21 plus the finals.
  - E step 4 says "stop and ask Kaveh before accepting". But by then
    `_delete_if_exists` has removed the masters, `lum_bg`,
    `rgb_reconciled` and `lrgb_final` (`lrgb_orchestrator.py:730-740`).
    There is nothing to go back to.
  - `stage_frames` collapses byte-identical basenames (I5), so the staged
    T24 sets are identical before and after the deletion. `n` stays on
    the same side of the `n >= 10` quality-filter threshold for every
    group (42→21, 28→14, 24→12; T21 4→2). **The T24 masters should
    therefore be pixel-identical**, within Siril determinism. That is a
    free oracle the plan does not use.
  - `colour_reference` is chosen by STACKCNT. E records FWHM and
    `luminance_selected`, but not STACKCNT or `colour_reference`.
- **Fix**:
  - E step 0: copy `_pipeline/{final,checkpoints,run_signature.json}`
    and the T21/T24 group `lights/master_*.fit` somewhere outside the
    tree.
  - E step 4: assert `np.array_equal` (or the measured bound) for every
    T24 master's data (not its headers, since plate solving rewrites
    WCS), unless I6 is batched. Also check STACKCNT per contributor and
    `colour_reference`.
  - Update §1.5 with the verified T24 hashes.

### R3-3 (major): with Q1 = (b), the mode-aware helper has no home, and Step 5's hash contract depends on it

- **Evidence**:
  - The `flats_used(mode, flats)` helper, and with it the G10 fix, lives
    only in 3b. 3b ships only if Q1 is (a) or (c).
  - Step 5's "Hash contract" cites "3b's (or Step 5's, if Q1 = b)
    mode-aware helper", but Step 5's **Change** list does not contain
    it.
  - Under (b), with the flag on, `_colour_contributor_flat_frame_hash`
    (`contributor_staleness.py:85-100`, called unconditionally at
    `lrgb_orchestrator.py:527`) picks up header-sourced T20/T05 R/G/B
    flats for PRECALIBRATED contributors. Toggling the flag then
    invalidates them, which is G10 live.
  - 3b is also over-bundled: staging, `-norm=mul`, recipe constant,
    schema field, call-site threading and an unrelated bug fix (G10).
- **Fix**: move the helper plus the G10 fix into its own commit (Step 3c,
  or "2b"), before 3b and independent of Q1. It is behaviour-free on real
  data (verified above), and its mocked test is PRECALIBRATED with flats
  → `""`. 3b then only adds the recipe half. Update Step 5's contract
  reference.

### R3-4 (major): Step 11's "Prefer iTelescope" is mis-scoped: mode is per telescope, not per binning

- **Evidence**:
  - `infer_calibration_mode(report, telescope)` has no binning argument.
    `has_calibrated_lights` is `any(key[0] == telescope …)`.
  - M51 post-deletion has calibrated T24 lights (jmwill L 8, R/G/B BIN1
    14/12/12, plus 1 kaveh L).
  - "Prefer iTelescope" would flip **all** of T24, not "T24 BIN1":
    - Luminance would fall from 21 raw subs to 9 calibrated;
    - the BIN2 colour contributor (kaveh096 R/G/B, 0 calibrated copies)
      would vanish.
  - The proposed "exclude T24/M51 explicitly" means hardcoding by
    telescope name, which contradicts `calibration_policy.py`'s stated
    principle ("never hardcode by name").
- **Fix**: correct the text. Recommend removing Prefer-iTelescope from
  Q3. With Step 4's `source` rule, "Keep" already gives PRECALIBRATED
  whenever the local bias/darks are header-only, which is the only real
  case. Q3 becomes Keep vs Lift.

### R3-5 (major): under the default "Keep", recognised local flats are unreachable from any CLI or skill entry point

- **Evidence**:
  - After Step 4, T20, T68 and T05 stay PRECALIBRATED. Flats are used
    only with an explicit `calibration_mode` override.
  - No entry point exposes that override. `run_stage.py:53-60` and
    `run_narrowband.py:27-36` have no `--calibration-mode`, and Step 12
    adds only `--flat-policy` and `--calibration-header-fallback`.
  - The net user-visible result of Steps 4–5 is therefore "flats are
    listed in the interview", not "flats can be applied". That falls
    short of "build the gaps".
- **Fix**: add `--calibration-mode TEL=raw_local|precalibrated`
  (repeatable) to Step 12's CLI list, or to Step 5. Otherwise state
  plainly in the TL;DR and Q9 that local flats for M42/IC 1396/M31
  remain API-only until Step 11 "Lift".

### R3-6 (major): `flat_recipe` is too narrow for the changes the plan already anticipates

- **Evidence**:
  - I6 (`-nonorm` for bias/dark) and 8a (`-cfa`/`-equalize_cfa` on the
    calibrate command) both change calibrated pixels. No signature field
    covers bias/dark composition, bias/dark stacking or the light
    calibrate command. `ContributorSignature` has only `frame_hash`,
    `flat_frame_hash`, `spcc_profile` and `calibration_mode`
    (`run_signature.py:118-140`).
  - Batching I6 into E "works" only because the deletion happens to make
    T24 stale (R3-2). I6 done at any later time would leave every master
    silently stale.
  - The schema field is an on-disk contract, so a second one later costs
    another one-time rebuild.
- **Fix**: name the field `calibration_recipe` (for example
  `"flat:v2:dedup+uniq+mul"`), built by one function covering the
  bias/dark/flat stack flags and the calibrate-command shape. It stays
  mode-aware through the same helper. I6 and 8a then bump the constant
  instead of adding schema.

### R3-7 (minor): header-sourced frames are excluded from the mode decision, but not from consumption or the signature

- **Evidence**:
  - `source` is honoured only in `infer_calibration_mode`.
    `calibration_index()` (`ingest.py:293-299`) merges header and
    filename frames. So does `select_dark`, as does `build_group_master`'s
    bias lookup (`master_builder.py:176`).
  - A telescope that is RAW_LOCAL by filename and also has header-only
    bias/darks therefore gets them mixed into its masters when the flag
    is on. Bias and dark sets are never hashed, so toggling the flag
    neither invalidates nor records this.
  - Also, `missing_calibration_warnings` and `dark_scaling_notes` treat
    header frames as present. The interview hides this for PRECALIBRATED
    telescopes, but under an explicit override it does not.
  - No current project hits this (M51 checked above).
  - Step 5's "changes no persisted hash or recipe on **any** project"
    holds only for the seven projects. A filename-RAW_LOCAL telescope
    with header-only flats would flip `infer_flat_policy` SKIP→REQUIRE
    and change its flat hash.
- **Fix**:
  - Scope the Step-5 claim to "the seven gate-(D) projects".
  - Add a rule: header frames of a type are used only when the telescope
    has no filename frames of that type. Otherwise they are listed as
    unrecognised with the reason "shadowed by filename-recognised
    frames". Mocked test.

### R3-8 (minor): the `source`-aware `infer_calibration_mode` breaks about 20 test fakes

- **Evidence**:
  - `infer_calibration_mode` today reads only `calibration_index()`
    **keys**.
  - Filtering on `frame.source` means reading the values. Test fakes
    return strings (`{("T24","Bias",1,0.0): ["b"]}`,
    `test_pipeline.py:448-450`, `1469-1475`, and similar in
    `test_run_signature.py` and `test_skill_interview.py`).
- **Fix**: specify `getattr(f, "source", "filename")`, or a dedicated
  `IngestReport` method with a documented fake-update list. Either way,
  budget it into Step 4's diff.

### R3-9 (minor): Step 4 is not one reviewable commit

- **Evidence**: Step 4 does all of the following at once:
  - extracts `classify_tree` from `scan_session`, which is
    behaviour-preserving;
  - adds zip peeking;
  - adds eight fallback rules and three attribution rules;
  - adds `CalibrationFrame.source`;
  - changes `infer_calibration_mode`.

  Also unspecified: if `classify_tree` puts zip-peeked lights into
  `report.lights`, `scan_session` must **replace** them with extracted
  paths, not append. Appending recreates the double-count class of bug
  in §1.5.
- **Fix**:
  - 4a: a pure `classify_tree` refactor, with flag-off gate (D) equality
    against Step 1a and an explicit "`scan_session` output equals the old
    one, including list order".
  - 4b: fallback, `source` and the mode rule.

### R3-10 (minor): Step 6a(iii) content-hashes every matched flat in the interview

- **Evidence**: 3a is "cheap next to the copy", but the interview makes no
  copy. That is T21 (330 × 12.6 MB ≈ 4.2 GB) and T68 (88 frames at
  6248×4176) read on every interview run.
- **Fix**: the interview reports name collisions only, or uses a header
  identity (`DATE-OBS`, `EXPTIME`, file size). The SHA-256 stays in
  `run_calibration`.

### R3-11 (minor): Step 10's noise floor is expensive and on a different scale

- **Evidence**:
  - 20 splits × 2 halves × 3 arms × 3 masters is about 360
    register+stack runs.
  - Half-stacks are about √2 noisier than the full stacks being
    compared, so the IQR is conservative by roughly that factor. The plan
    does not say so.
  - The prerequisites "5 and 7" are unnecessary: the script can call
    `classify_tree(…, calibration_header_fallback=True)` directly and
    does not use the sanity notes.
- **Fix**: calibrate once per arm, then split only register+stack. State
  the √2 scaling or rescale. Drop the Step 5/7 prerequisites. Consider 10
  splits.

### R3-12 (minor): the determinism bound comes from a single pair, and gate (P) compares against a third run

- **Evidence**: Siril writes `bc_flat_*` in parallel (the persisted mtimes
  interleave). Two tmp runs give one Δ sample. The persisted reference
  came from a different process and session.
- **Fix**: take at least 3 tmp runs **plus** tmp-vs-persisted at Step 1b.
  Set the bound at 2× the maximum observed, and fail loudly if
  tmp-vs-persisted exceeds tmp-vs-tmp.

### R3-13 (minor): Step 7's level heuristic fires on the one production path

- **Evidence**:
  - §1.3.6 measures T21 L sky flats at about 13%. That is below the
    15% floor, so every T21 rebuild warns.
  - "ADU after `BZERO` as a fraction of 65535" does not apply to Siril's
    32-bit float master, which is in [0,1].
- **Fix**: measure on a raw-frame sample (the same ≤ 3 frames used for
  saturation), or on the float master × 65535 with that stated. Pick the
  floor knowing T21 = 13%, for example 10%.

### R3-14 (minor): scope. Several steps can be cut or deferred against the actual ask

- **Step 2 (G8)**: no real trigger. The T68-2021 flats (IC 1396 folder)
  and the T68-2023 lights (NGC 3628 folder) are never co-scanned, because
  `scan_session` is per project. Fold `check_flat_compatibility` into the
  Step 6 pre-flight only, or defer.
- **Step 8 (OSC)**: unreachable under "Keep" without the R3-5 override.
  Step 10 does not need it. Move it after Step 11, gated on "Lift" or an
  override use-case.
- **Step 9 (G11)**: narrowband staleness, not flat-specific. Defer.
- **Step 7's dark-current measurement and 6a(iii)**: nice-to-have.
- **Q9's bucketing conflicts with Step 12**: Step 12 documents
  narrowband `--force` (Step 9) and boost flags even if 8–11 are
  declined. Make Step 12's items conditional.

The core deliverable is 0, 1a, 1b, 3a, 3b (+R3-3's split), 4, 5, 6 and 12.

### R3-15 (minor): the I6 measurement is not scheduled, yet E batching requires it first

- **Evidence**: Q7 is "measure `-nonorm` … and decide; if wanted, batch
  into E". No step performs the measurement, and E follows 3b directly.
- **Fix**: add a no-code measurement next to Step 3a, using the same
  `tmp_path` harness as gate (P) (bias/dark master with and without
  `-nonorm`, and the calibrated T24 L sub), so Q7 can be answered before
  E.

### R3-16 (cosmetic)

- **E step 5**: "record in the commit message of the step that triggered
  E" is impossible, because the code is merged before E runs. Record the
  hashes in a follow-up ROADMAP commit instead of amending.
- **E step 3**: `scripts/run_m51.py` has a placeholder `PROJECT_DIR` in a
  tracked file. Do not edit it; call `run_lrgb` with the `_run_m51`
  arguments from a scratch script.
- **`_index.json`**: the M51 root's `_index.json` (July, 111 references
  to `Uncalibrated Lights`) goes stale on deletion. It has no consumer
  (`workspace.index_path` is unused). Mention it in Step 0.
- **Q10**: "confirm it can wait for E" reads as if the deletion waits.
  Step 0 deletes immediately; it is the **M51 run** that waits.
- **§2.1 M31 row**: the bias filename telescope token is `T5` (not `T05`)
  with the user `Local Admin`, so rule (b)'s `T%02d` normalisation is
  load-bearing for M31. Say so in Step 4.

---

## Round-2 fixes: checked

- **Held**: R2-2 (gate taxonomy), R2-4, R2-7, R2-8 (extended by R3-2),
  R2-9, R2-10 (the rejection is correct), R2-11, R2-12, R2-13, R2-14,
  R2-15, R2-16, R2-17 (apart from R3-13), R2-18, R2-19.
- **Partially held**:
  - R2-1: the mode is decoupled, but consumption is not (R3-7), and
    Step 11's "Prefer" option is mis-scoped (R3-4).
  - R2-3: the sequencing exists, but the suite protocol undermines it
    (R3-1), and the T24 impact is understated (R3-2).
  - R2-5: the helper is right, but it is tied to Q1 (R3-3).
  - R2-6: the statistics are fixed; the cost and scale are not (R3-11).

VERDICT: REVISE
