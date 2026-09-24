# plan-flats-v4: adversarial review, round 2

Reviewer scope: `docs/plan-flats-v4.md` rev 2, checked against `main` @
`22cdf19`, the round-1 review and its review log, and the read-only data
under `D:\Raw Photo Backups\iTelescope\`.

- **Not run**: Siril, GraXpert, the pipeline, `scan_session`, or the test
  suite.
- **Evidence**: code reads; strings embedded in `siril-cli.exe` and the
  bundled `.ssf` scripts; astropy header and pixel reads; one
  `frame_identity_hash` recomputation.

**Checked and correct**:
- the `file:line` citations in §1.1 and §1.2;
- the persisted `349469062e57762c` T21 flat hash;
- the staged T21 `flat_.seq`: `nb_images=20`, with 20 `bc_flat_*` files;
- the T20, T05 and T68 frame counts (bias, darks, flats), including the
  truncated M42 bias and OIII files;
- the M42 SII flat range (43.2–46.0 s), so rejecting round-1 F9's
  "≤ 46 s" sub-claim was correct;
- the 6 `NOT USING FLAT:` strings;
- the Siril `stack` help text ("Otherwise addtive with scale method is
  applied by default", with that typo in the binary) and the bundled
  `-norm=mul` flat stacks;
- `OSC_FILTER == "Color"`, which matches the T68 flats' `FILTER='Color'`;
- a raw T20/T05/T68 light's `INSTRUME` equals the `INSTRUME` of its own
  calibration frames.

---

## Findings

### R2-1 (critical): Step 4's flag changes `infer_calibration_mode` straight away, not in Step 11

- **Evidence**: `calibration_policy.py` `infer_calibration_mode` returns
  PRECALIBRATED only if `(not has_bias or not has_dark) and
  has_calibrated_lights`. Step 4 with the flag on makes T20 (M42), T68
  (IC 1396) and T05 (M31) all have bias **and** darks, so all three
  resolve to **RAW_LOCAL** the moment a caller passes the flag (possible
  from Step 5). Round-1 F4 said this ("the flag flips
  infer_calibration_mode"), but the revised plan still treats it as a
  future decision. Step 11 says "teach `infer_calibration_mode` that …
  bias+dark+flat … resolves to RAW_LOCAL", which is already what the code
  does, and "otherwise PRECALIBRATED stays preferred when `calibrated-`
  lights with `F` in `CALSTAT` exist", which needs a code change that no
  step schedules.
- **Consequences once Step 5 lands and the flag is on**:
  - **IC 1396 (T68)**: RAW_LOCAL OSC hits the `build_osc` `cal_index={}`
    `KeyError` (I1). The flag crashes `run_lrgb` until Step 8b, and for
    good if Kaveh declines Q5.
  - **M31 (T05)**: silently runs RAW_LOCAL with the −15 °C/2019 darks
    against −10 °C lights. That is G14, which the plan lists as out of
    scope, but it becomes live here.
  - **M42 (T20)**: no `run_signature.json` exists, so `usable()` serves
    the existing PRECALIBRATED-built masters. Narrowband masters have no
    signature at all. The run labels them `calibration_mode=raw_local`,
    and Step 5's `calibration_header_fallback=True` is recorded next to
    them. That is exactly the Q4 migration hazard, which the plan places
    in Step 11.
  - Steps 4 and 5 say "Behaviour change: none by default". That is true,
    but it hides that the flag's only real-world effect is a mode flip.
- **Fix** (pick one, and list it as a new Q for Kaveh):
  - (a) The fallback feeds `flat_index()` and `calibration_index()`, but
    `infer_calibration_mode` ignores fallback-sourced bias/dark
    (`CalibrationFrame` gains a `source` field, or the report keeps them
    in a separate list) until Step 11 decides.
  - (b) Make Step 8b (the I1 fix) and a Q4 migration prerequisites of
    Step 5, and call out the M31 dark-temperature issue.

  Either way:
  - add `infer_calibration_mode` and `infer_flat_policy` for every
    telescope to the flag-on data invariants (M42, IC 1396, M31 as well as
    M51, NGC 3628 and the Abells);
  - rewrite Step 11 so it describes the code that actually exists.

### R2-2 (major): the Step-17 "SHA-256 oracle" is described wrongly; it has no fixture and cannot see pixel changes

- **Evidence**: `tests/test_pipeline.py:1387-1405`
  (`test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`):
  - it hashes the **current on-disk** `lrgb_final.fit`/`M51_lrgb.tif`,
    calls `_run_m51()` and asserts the bytes are unchanged;
  - no stored hash exists (grep for hex constants: only
    `test_reconciliation.py` has any).

  On a resumed run, `usable()` skips every master, so `run_calibration`,
  `build_master_flat` and Step 1/2/3a/7 code never execute on M51.
- **Why it matters**:
  - Step 3b's "Re-baseline the Step-17 SHA-256 fixture in the same commit"
    has nothing to change. Right after 3b, the first real session is
    **order-dependent**:
    - the full-run test fails, because T21 is stale, I4 regenerates the
      finals and the before-hash is from the old files;
    - `test_run_lrgb_stop_after_masters_stops_before_reconciliation`'s
      mtime assertions may fail too.
  - The global rules say the oracle "breaks" on "any change to T21's …
    master pixels". That is false, and §2.3 of the plan itself says the
    hash is blind to pixel changes. For Steps 1 and 4–8 the oracle only
    proves "nothing was invalidated", not "no pixel changed".
  - "38-minute real run" describes a full rebuild; the gate as specified
    is a resume.
- **Fix**:
  - (1) Re-describe the gate as an **invalidation oracle**.
  - (2) For steps that touch calibration code (1, 2, 3a, 7, 8a), add a
    real **pixel** check: rebuild T21 L's master flat and calibrated
    lights into a `tmp_path` (`run_calibration` with a `tmp_path`
    `work_dir`), then compare them with M51's persisted
    `_pipeline/T21-…-Luminance-bin1/flat/master.fit` (byte-equal, or
    `np.array_equal`; first measure whether Siril is deterministic run to
    run).
  - (3) Replace 3b's "re-baseline the fixture" with a procedure:
    - record the SHA-256 of the old `lrgb_final.fit`/TIFF;
    - run one full `run_lrgb` on M51;
    - record the new hashes and put both in the commit message;
    - only then run the real-data test module.

### R2-3 (major): Kaveh's pending M51 duplicate-folder deletion will invalidate M51 and break the Step-1 invariants

- **Evidence**:
  - `Uncalibrated Lights - Jan 2025/` holds the same 6 zips as `Lights/`
    (T21 and T24 raw lights).
  - `_extract_zipped_lights` extracts both to the same
    `_extracted_zips/<name>` and appends a `LightFrame` for **each**, and
    `_instrument_groups_from` does not deduplicate.
  - The persisted T21 `frame_hash` is `83bc8bee12b82922`. Recomputed:
    `frame_identity_hash([a, b, a, b]) == "83bc8bee12b82922"`, while
    `frame_identity_hash([a, b]) == "c2b76b333110b4a6"`.
  - `M51 from Jean-Marie/` and `calibrated Lights - T24 - Feb 2025/` are
    likewise duplicates (92 files each).
- **Why it matters**:
  - Deleting the folders changes T21's (and possibly T24's) `frame_hash`.
    That makes the contributors stale, forces a full regeneration through
    I4, and fails the oracle.
  - The Step-1 invariants ("`instrument_groups()` keys and counts
    unchanged") would pin the duplicate-inflated counts, and they would
    then break.
  - The §2 inventory **excluded** these folders, so its numbers do not
    match what the invariant tests will see while the folders exist.
- **Fix**:
  - Put the deletion in Step 0: delete, do one full M51 run, record the
    new hashes, **then** pin the invariants.
  - Or, if Q1 = (a), fold the deletion into Step 3b's single re-baseline.
  - Either way, state which tree state the invariants pin.
  - Surface as a new incidental finding (I5): the same `stage_frames`
    basename collapse silently deduplicates **lights** too (benign here
    only because they are byte-identical copies).

### R2-4 (major): Step 3b's `<parentdir>__<name>` staging would double-count byte-identical duplicate flats

- **Evidence**: this dataset already shows the "same files copied into
  two folders" pattern (R2-3). T21's colliding Luminance flats are
  **distinct** exposures, with different `EXPTIME` and `DATE-OBS`.
  Unconditional parent-dir prefixing keeps both distinct frames and exact
  duplicates, so a duplicated flat folder would give some frames double
  weight in the master and bias the sigma rejection. Step 3a's
  "`N` matched, `M` unique names" cannot tell collisions from copies.
- **Fix**:
  - Deduplicate on content identity (file SHA-256, or `(basename,
    DATE-OBS, EXPTIME)` from the header), then uniquify the survivors'
    names.
  - 3a reports both counts: "`N` matched, `K` identical copies dropped,
    `C` name collisions between distinct frames".
  - Add a mocked test with two byte-identical folders plus two colliding
    distinct frames.
  - Sanitise parent names that contain spaces, and allow for two parents
    with the same name (use an index or a short path hash).

### R2-5 (major): `flat_recipe` is not mode-aware and is missing from the OSC path, which recreates G10

- **Evidence**:
  - Step 3b adds `flat_recipe`, "set only when the flat set is non-empty".
  - Step 5 makes only the colour **flat hash** `""` under PRECALIBRATED
    (`lrgb_orchestrator.py:527`).
  - Step 8b replaces the OSC `""` hash (`:614,618`) but never mentions
    `flat_recipe`.
  - With the flag on and T20 forced PRECALIBRATED, the colour contributor
    has recognised R/G/B flats, so `flat_recipe="v2:uniq+mul"` is
    persisted for flats that were never used, and toggling the flag
    invalidates the contributor. That is the G10 bug, just moved to a
    different field.
- **Fix**:
  - Derive `flat_recipe` from the same mode-aware "flats actually used"
    list as the hash, with one helper for both.
  - Add the PRECALIBRATED→`""` recipe test to Step 5 and the OSC recipe
    call-site test to Step 8b.
  - Define the recipe constant in `calibration.py`, next to
    `build_master_flat`, so the constant changes together with the stack
    command.

### R2-6 (major): Steps 10 and 11 contradict each other, and the noise floor is a single sample

- **Evidence**:
  - Step 10 says B vs C is "reported, not used as a pass/fail". Step 11
    branches on "**If B beats C**".
  - The "half-split" noise floor gives **one** difference per arm, so a
    "3σ" computed from one sample is not a statistic.
  - M42 has L = 10 and Red = 8 lights, so the halves are 4–5 subs.
  - Arm A ("RAW_LOCAL, no flat") has no production path:
    `infer_flat_policy` → REQUIRE, and `SKIP_IF_MISSING` still **uses**
    flats when they are present.
- **Fix**:
  - Define Step 11's trigger explicitly, as Kaveh's judgement on the
    reported B-vs-C numbers or as a stated rule, and put it in Q3.
  - Use repeated random half-splits (for example 20) or per-sub
    jackknife, and report the median and spread.
  - State that arms A and B are built by calling
    `run_calibration(flat_frames=[] / flats)` directly from a
    measurement script, not through `run_lrgb`.
  - Note that A vs B vignetting will be far above any noise floor; the
    3σ rule matters only for metric 2 (dust residual).

### R2-7 (major, surface-only): bias and dark masters also use Siril's default `addscale` normalisation

- **Evidence**:
  - `calibration.py` `build_master` runs `stack {seq} rej 3.0 3.0
    -out=master` (no `-norm`) for bias and dark.
  - The bundled `Mono_Preprocessing.ssf`/`OSC_Preprocessing.ssf` use
    `stack bias rej 3 3 -nonorm` and `stack dark rej 3 3 -nonorm`.
  - By the same help text the plan cites for G2, the bias and dark
    masters are addscale-normalised.
  - Step 3b explicitly keeps "bias/dark stacks unchanged" without saying
    that they deviate from the same reference.
- **Why it matters**: this is the **selected** T24 Luminance's and both
  colour contributors' master dark, which feeds M51's actual deliverable.
  It is exactly the kind of incidental finding the repo rules say must be
  surfaced, not dropped or bundled.
- **Fix**: add it as I6 and a new Q ("`-nonorm` for bias/dark: measure
  first, then decide; it would re-baseline every M51 master"). Do **not**
  bundle it into 3b.

### R2-8 (major): after 3b, "the selected Luminance is T24 and does not change" is asserted, not checked

- **Evidence**: `luminance_selection.select_luminance_source` picks the
  lowest measured FWHM. `_run_m51` passes no `lum_source`. Rebuilding T21
  L with a different flat changes the calibrated pixels that registration
  measures FWHM on. The project memory records that a previous T21 FWHM
  claim was wrong.
- **Fix**: in 3b's verification, record T21 and T24 FWHM and
  `luminance_selected` before and after. If the selection flips, stop and
  ask Kaveh before re-baselining; do not silently accept a different
  composite.

### R2-9 (minor): the invariant tests and the flag cannot be run the way the plan describes

- **Evidence**:
  - The global rules say "classification only, never `scan_session` on
    zip-bearing projects", but the flag is a `scan_session` parameter.
  - Rule (c) and the `INSTRUME`/`NAXIS` cross-check need the full
    raw-light set, and M51's T21 raw lights exist **only** inside zips.
    Without extraction, `instrument_groups()` for M51 is not what a real
    run sees.
  - `astro_pipeline/index.py` `build_index` already classifies a whole
    tree, peeks inside zips without extracting, and calls
    `classify_frame`. The plan does not mention it, so the fallback would
    not reach it.
  - Also, M42's only zip is corrupt (`BadZipFile`), so `scan_session` on
    M42 writes nothing. The write risk is M51 only, and M51's
    `_extracted_zips` already exist, which makes extraction idempotent.
- **Fix**:
  - Put the fallback in a pure function, for example
    `classify_tree(root, *, calibration_header_fallback, zip_mode)`, that
    both `scan_session` and the invariant tests call. Rule (c) takes light
    names peeked from zips; the cross-check uses a bare raw light, or
    falls back to "unrecognised: no readable raw light".
  - Say whether `build_index` gets the flag or is left as is (it is
    test-only today).
  - Make the global rule accurate.

### R2-10 (minor): Step 2's real negative test does not discriminate

- **Evidence**: all 44 "T68-2023" lights in NGC 3628 are
  `LeoTriplet from Jean-Marie/calibrated-Color-T68-jmwill-…` files. The
  filenames do not parse, they are `CALSTAT=BDF`, and they have
  `INSTRUME='ASCOM'` and `BAYERPAT='INVALID'`. Calibrated lights rewrite
  `INSTRUME` everywhere: T20 has `T20 SBIG STL11000M` and T05 has
  `T05-SBIG-ST10XME`, against the raw `SBIG … 3 CCD Camera`. The test
  would pass on `INSTRUME` even if the `NAXIS` check were broken, and
  these lights never reach `run_calibration`.
- **Fix**:
  - Assert the specific mismatch reason (`NAXIS1/2`), and add a
    synthetic `NAXIS`-only mismatch.
  - Document that `check_flat_compatibility` must only ever see
    **raw** light headers.
  - Correct §2.1's NGC 3628/T68 row: "calibrated-only, jmwill, names
    unparsed".

### R2-11 (minor): Step 4 does not state the bias exptime and dark exptime rules

- **Evidence**: `master_builder.py:176` looks up
  `cal_index[(telescope, "Bias", binning, 0.0)]`, an exact float key.
  `select_dark` matches light exposure times (integers from the filename)
  against dark exposure times. Real headers are `EXPTIME=0.0` for bias and
  180.0 or 240.0 for darks today, but CMOS bias frames often carry
  non-zero `EXPTIME`, and a dark with 239.999 would silently miss.
- **Fix**: specify that fallback bias frames get `exptime=0.0` whatever
  the header says, and that dark `EXPTIME` is rounded (to 0.1 s, or an
  integer as the light filenames use). Add mocked tests for both.

### R2-12 (minor): Step 5's "exactly like `calibration_mode`" is the wrong model

- **Evidence**: `calibration_mode` sits on `ContributorSignature`, so it
  **is** compared through dataclass equality in `diff_invalidation`
  (`old.luminance != new.luminance`, `run_signature.py`). Only
  `contributor_stale` ignores it. A field modelled on it and placed
  per-contributor would cascade every stage when the flag is toggled.
- **Fix**: say it goes on `RunSignature` top-level (which
  `diff_invalidation` compares field by field and would ignore), and add
  a test that toggling it gives `diff_invalidation(...) == set()`.

### R2-13 (minor): the `NOT USING OFFSET:` count is 6, not 5

- **Evidence**: `siril-cli.exe` contains these `NOT USING OFFSET:`
  strings:
  - `cannot open the file`
  - `could not parse the expression`
  - `image dimensions are different`
  - `number of channels is different`
  - `the offset value could not be parsed`
  - `the offset value is not consistent with image bitdepth`

  It also has 5 DARK and 6 FLAT variants.
- **Fix**: correct the review log (F3) and Step 1. Test the regex on the
  prefix, not on an enumerated list.

### R2-14 (minor): G11's stale-docs list is incomplete

These locations also claim "T68 has no local darks", or that IC 1396 is
already RAW_LOCAL:
- `skill/SKILL.md:162` says "IC 1396, RAW_LOCAL -- local bias". That is
  false today: T68's bias is unrecognised, the mode is PRECALIBRATED, and
  RAW_LOCAL OSC raises a `KeyError`.
- `tests/conftest.py:149`
- `calibration.py:425,635,775`
- `colour_contributor.py:355`
- `master_builder.py:188-199`

Add them to Step 12.

### R2-15 (minor): Step 1's file list and scope

- There is no Abell 31 fixture in `tests/conftest.py`: only M51,
  NGC 3628, Abell 6 and IC 1396. Step 1 must edit `conftest.py`, and the
  dirty `conftest.py` from Step 0 has to land first.
- Step 1 bundles two logical changes: log detection, which changes
  behaviour, and data invariants, which are test-only. Split them into 1a
  and 1b so the invariant commit is provably behaviour-free.

### R2-16 (minor): Step 2 is called "pre-flight" but runs mid-run

- **Evidence**: `check_flat_compatibility` runs inside `run_calibration`,
  per group. A mismatch in the second or later group aborts `run_lrgb`
  after earlier masters have already been built.
- **Fix**: also run it from the interview (Step 6a) over every matched
  flat set, so the user sees it before any Siril work. Keep the
  `run_calibration` check as the backstop.

### R2-17 (minor): Step 7 specifics are undefined or costly

- **Undefined**: "warn below 1 s on CMOS", because no header identifies
  CMOS (`INSTRUME='ASI Camera (1)'`). Use an exposure floor for all
  cameras, or leave it out.
- **Costly**: reading the pixels of every flat to measure saturation
  (T68 is 88 × 26 Mpx) inside `run_calibration`. Sample N frames, or
  compute it once on the master.
- **Unsourced**: the 15–85% median band and the 0.1% threshold. Cite a
  source or mark them as heuristics.

### R2-18 (minor): Q1 couples G2 for every future telescope to the M51 re-baseline

- **Evidence**: if Kaveh picks (b) to keep M51 stable, `-norm=mul` is
  never applied to M42, M31 or IC 1396 either.
- **Fix**: add option (c): ship `-norm=mul` plus unique staging **with**
  `flat_recipe`, but defer M51's actual rebuild until the next deliberate
  M51 regeneration (for example together with R2-3's deletion). Or at
  least state that (b) also freezes G2 everywhere.

### R2-19 (cosmetic)

- Step 3a's note text is misleading once 3b lands, because staging no
  longer "would keep `M`". Word it per mode.
- `interview.render(project_dir, report)` already takes a report, so the
  flag belongs to `main` only.
- M42 bias filenames (`Bias-Darks-T20-001-bias-B1.fit`) do carry a `T20`
  token. "No `T20` token anywhere in the path" is true only for directory
  names.
- M42 Green BIN2 flats are 40.5–42.0 s, against Red 4.3 s and Blue 9.9 s
  from the same dawn session, at similar medians (~27k against ~25k ADU).
  That is worth a line in §2.1; Step 7's exposure notes should surface
  it.
- The binary spells it "addtive with scale". Quote it exactly if the plan
  cites the string.

---

## Round-1 fixes: checked

- **Held**: F1 (facts), F2, F3 (apart from the count, R2-13), F4
  (threading), F7, F8 (structure), F10, F11, F12, F13, F14, F15, F16,
  F17.
- **Did not hold**:
  - F4's "flag flips `infer_calibration_mode`" was accepted in the review
    log but not reflected in Steps 5 and 11 (R2-1).
  - F1's "re-baseline the Step-17 SHA fixture" rests on a fixture that
    does not exist (R2-2).
  - F6's noise floor is still a single sample (R2-6).
  - F8's colour-hash mode rule does not cover the new `flat_recipe`
    (R2-5).

## New questions for Kaveh

1. Should flag-recognised bias and darks be allowed to flip
   `infer_calibration_mode` before Step 11 (R2-1)?
2. When is the M51 duplicate-folder deletion scheduled, relative to the
   invariants and the 3b re-baseline (R2-3)?
3. `-nonorm` for bias and dark masters: measure it, or leave it
   (R2-7)?
4. Should Q1 get option (c): fix the recipe now but defer M51's rebuild
   (R2-18)?

VERDICT: REVISE
