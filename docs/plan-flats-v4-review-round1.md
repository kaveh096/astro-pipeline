# plan-flats-v4: adversarial review, round 1

Reviewer scope: `docs/plan-flats-v4.md` against `main` @ `22cdf19` plus the
read-only data under `D:\Raw Photo Backups\iTelescope\`. No Siril,
GraXpert, pipeline or full-suite run. Evidence comes from code reads, the
Siril binary's embedded strings and bundled scripts, filename
classification (`classify_filename` / `classify_frame`, **not**
`scan_session`, because `scan_session` writes `_pipeline/_extracted_zips`
into the data folder), and astropy header and pixel reads of a few flats.

Spot-checks that **held**: the `_check_calibration_log` gap
(`calibration.py:191-222`), `build_osc` passing `{}` / `[]` / `SKIP`
(`colour_contributor.py:393-398`), `cal_index[(telescope,"Bias",...)]`
(`master_builder.py:176`), the hard-coded OSC `""` hashes
(`lrgb_orchestrator.py:614,618`), the colour flat hash being computed
regardless of mode (`:527`), `run_narrowband --force` deleting only the
final products (`narrowband_orchestrator.py:122-127`), the frame counts
for T68 (48 bias / 50 × 240 s darks / 88 flats), T05 (15 / 16 / 3 × 40)
and T20 (50+50 bias, 30 darks, 70 flats), T68 resolving to PRECALIBRATED
today (25 `calibrated-T68-…` lights parse), the 11 T21 `Master_Flat`
files with `CALSTAT=M`, and the NGC 3628 `T73 - calibration` masters all
being `CALSTAT=M`.

---

## Findings

### F1 (critical): T21 Luminance flats have duplicate basenames, so the one real flat path is already corrupted, and Step 5 would break M51

- **Evidence**: `Calibrations/T21/Flats/2024 06/raw flats/` has 12
  timestamped session folders. Luminance is split between
  `20240616_080204` (`skyflat0..19`) and `20240616_080536`
  (`skyflat0..9`). That gives 10 duplicated basenames across the 330
  flats, and all 10 are Luminance. `stage_frames` (`staging.py:28`) copies
  by basename into one directory, so M51's staged
  `_pipeline/T21-…-Luminance-bin1/flat/` holds **20** `scope_*` files,
  not 30. The same holds for the `-pedestal0` variant. The current M51
  master flat is therefore a 20-frame stack. Its session composition
  depends on copy order: `rglob` order, with the second session winning
  for 0..9.
- **Why it matters**:
  - Failure mode 5 ("No real project triggers this today") and Step 5's
    test ("T21 has 30 unique names and passes") are both false.
  - As written, Step 5 raises `FlatMismatchError` on M51's T21 Luminance
    group. That breaks M51 and the Step-17 byte-identity gate, and
    contradicts "No step in this plan changes that signature" in spirit:
    the pixels change or the run fails.
  - The M51 invariant ("11 keys × 30 frames") passes today and hides the
    bug.
  - The v3 "30-frame" figures (0.7735 / 0.764 corner ratio) were measured
    on a 20-frame master.
  - `_flat_frame_hash` over names (including duplicates) stays stable
    whatever the fix. A fix that stages unique names therefore changes the
    master's pixels **without** changing any signature, and `usable()`
    serves the old 20-frame master forever unless forced.
- **Fix**:
  - Restate G5 as a live bug on the only real flat path.
  - Split into two steps:
    - (a) Detect and report only. Add a note listing the duplicated names
      and the effective frame count, and extend the M51 invariant to
      assert "30 paths, 20 unique basenames" so the current state is
      pinned.
    - (b) A behaviour change for Kaveh to decide (new Q): stage under
      unique names (for example a `<parentdir>__<name>` prefix), rebuild
      M51 T21 L with `force={"masters"}`, and re-baseline the Step-17
      SHA fixture. The alternative is to accept 20 frames and document it.
  - Never raise on duplicates by default.
  - Re-measure §2.3 after the decision.

### F2 (major): the master flat is stacked without multiplicative normalisation

- **Evidence**:
  - `build_master_flat` (`calibration.py:541-545`) runs
    `stack bc_flat_ rej 3.0 3.0 -out=master` with no `-norm`.
  - Siril's own help string in `siril-cli.exe` says: "…Otherwise additive
    with scale method is applied by default". So the default is
    `addscale`.
  - Siril's bundled `C:\Program Files\Siril\scripts\Mono_Preprocessing.ssf`
    and `OSC_Preprocessing.ssf` stack flats with
    `stack pp_flat rej 3 3 -norm=mul`.
  - Sky-flat levels really do vary. Sampled medians: T21 L 22655–25681
    (about 13%), T68 20735–25859 (about 25%), T20 L 25114–25780, T05 R
    25332–26189.
- **Why it matters**: sky flats taken across twilight differ
  multiplicatively. Additive-scale normalisation plus sigma rejection is
  the wrong model. It biases the rejection and so the vignetting profile
  that Step 9 is supposed to measure. The plan never mentions
  normalisation, even though the brief asked for it.
- **Fix**:
  - Add a gap (G13) and an explicit, Kaveh-gated behaviour-change step:
    `-norm=mul` for flats only. Bias and dark stacks stay as they are.
  - That step changes the M51 T21 master flat without changing
    `flat_frame_hash`. It therefore needs either a recipe-version field in
    `ContributorSignature` (a resumability-contract change that must be
    called out) or a documented `force={"masters"}` and an SHA re-baseline.
  - Order it **before** Step 9, otherwise the measurement compares against
    a mis-stacked flat. It can be bundled with F1(b) so M51 is re-baselined
    once, not twice.

### F3 (major): Step 1 is incomplete (six FLAT strings, OFFSET not covered, and the flat's own calibrate is never checked)

- **Evidence**: the binary contains **six** `NOT USING FLAT:` strings, not
  four:
  - `cannot open the file`
  - `Could not load reference image`
  - `cannot open file '%s'`
  - `image dimensions are different`
  - `number of channels is different`
  - `could not parse the expression`

  It also contains four `NOT USING OFFSET:` strings. OFFSET is Siril's
  name for the bias. In addition, `build_master_flat`'s
  `calibrate … -bias= -prefix=bc_` result (`calibration.py:541-545`) is
  never passed to `_check_calibration_log`. A bias that silently fails to
  subtract from the flats is invisible, and the same applies to
  `subtract_bias=True` lights (scaled-dark and bias-only OSC).
- **Fix**: Step 1 should match `NOT USING (DARK|FLAT|OFFSET):`, run the
  check on `build_master_flat`'s calibrate result too, and test all six
  FLAT strings plus the OFFSET strings. The claim "no known real run hits
  this" should be backed by grepping existing Siril logs under M51's
  `_pipeline` if they are persisted, or else stated as unverified.

### F4 (major): Step 4's flag is not threaded to any caller, and the interview can disagree with the run

- **Evidence**: `scan_session` is called at `lrgb_orchestrator.py:263`,
  `narrowband_orchestrator.py:114`, `skill/interview.py:215` and
  `skill/run_narrowband_boost.py:90`. Step 4 changes only `ingest.py`.
  Step 11 adds CLI flags "on the scripts that scan", but `run_lrgb`,
  `run_narrowband` and the orchestrator classes have no parameter to
  receive the flag. Steps 7 and 9 depend on "Step 4's flag on", which is
  reachable only by calling `build_group_master` by hand.
- **Why it matters**:
  - Steps 7 and 9 as specified have no production path.
  - If the interview scans without the flag and the run scans with it, the
    preview (G3) shows the wrong mode and policy.
  - The flag flips `infer_calibration_mode` (T20, T68, T05 to RAW_LOCAL).
    That changes `frame_hash` (raw vs calibrated names) and so invalidates
    persisted signatures, but the flag itself is not recorded anywhere.
- **Fix**:
  - Add an explicit step, after Step 4 and before Step 7, that threads
    `calibration_header_fallback` through `run_lrgb`, `run_narrowband`,
    `run_narrowband_boost` and `interview.render`/`main`, with a mocked
    call-site test for each (mirroring the existing
    "call site actually passes flat_frame_hash" tests).
  - Record the flag in `RunSignature` as diagnostic information, the way
    `calibration_mode` is.

### F5 (major): Step 6 exposes the unfixed `-cc=dark` without `-cfa` shape before Step 7 fixes it

- **Evidence**: after Step 6, and with Step 4's flag on, T68 has 50 ×
  240 s darks at the lights' exact exposure, so `has_any_dark` is true
  (`master_builder.py:180-203`). `select_dark` then returns an exact match
  and the calibrate command becomes `-dark= -cc=dark -debayer` without
  `-cfa`. That is I2, which the plan fixes only in Step 7. Siril's own
  `OSC_Preprocessing.ssf` always pairs `-cc=dark` with `-cfa`.
- **Fix**: move the `-cfa` part of Step 7 into Step 6, or before it, as a
  pure `_calibrate_command` change with byte-identity tests for all
  existing shapes. Alternatively, make Step 6 explicitly require the flag
  to be off until Step 7 lands, and state that. Also verify the claim
  (`calibration.py:432-440` docstring, `tests/test_calibration.py:397`)
  that bias-only is the T68 path. With the fallback on it is not, and the
  existing real T68 test does not cover the dark path.

### F6 (major): the Step 9 comparison is confounded, so its decision rule is not a flat measurement

- **Evidence**: PRECALIBRATED (`CALSTAT=BDF`) differs from local RAW_LOCAL
  in darks, bias handling, pedestal (iTelescope uses `PEDESTAL=-100` on
  masters; the lights are unknown) and cosmetic correction (`-cc=dark`),
  not only in flats. The rule "metric 1 at least 0.02 closer to 1.0" has
  no stated noise floor, and 0.02 is arbitrary. F1 and F2 also mean the
  local master flat is currently mis-built.
- **Fix**:
  - Add a third arm: RAW_LOCAL **without** flat versus RAW_LOCAL
    **with** flat, same darks. That isolates the flat's effect.
  - Keep PRECALIBRATED as the reference that answers "local vs
    iTelescope".
  - Derive the threshold from arm-to-arm repeatability, for example
    splitting the lights into two halves, not from a fixed 0.02.
  - Make Step 9 depend on F1 and F2 being resolved.
  - For M31, the darks are also a confound (G12). Either skip M31, or do
    the no-flat vs flat comparison with the same wrong darks, since that
    still isolates the flat.

### F7 (major): Step 4 attribution rule (b) is underspecified for multi-token roots, and the "reuse one header read" claim is wrong

- **Evidence**:
  - M51's root name `M51 - Whirlpool galaxy - T24 & T21 - Jan 2025`
    contains two tokens. The plan does not say whether several distinct
    tokens in one directory name, or across ancestors, count as ambiguous.
  - `classify_frame` returns early for `_FLAT_RE_SKYFLAT` or
    `_CAL_RE_CAMERA` names with no T-folder (`ingest.py:505-513`),
    **before** `_read_imagetyp`. The 88 T68 flats and the 70 T20 flats
    therefore need a new header read; there is none to reuse.
  - For skyflats the filename already carries filter and binning. The
    plan does not say which source wins when the filename and the header
    `FILTER`/`XBINNING` disagree.
- **Fix**:
  - Specify that several distinct `T\d+` tokens in the nearest
    token-bearing directory mean the frame is unrecognised with that
    reason. Add a mocked test that uses the M51-style root name.
  - Correct the performance note.
  - Define the precedence: filename first, then cross-check the header,
    with a mismatch leaving the frame unrecognised.

### F8 (major): Steps 4 and 10 leave Abell 31 and Abell 6 unguarded and weaken the "colour flat hash is harmless" claim

- **Evidence**:
  - Step 10 claims Abell 31 and Abell 6 stay byte-unchanged, but only M51
    and NGC 3628 have invariant tests. A classify-only scan found no
    unrecognised calibration frames in Abell 31, and in Abell 6 only 7
    unrecognised `Light Frame`/`BDF` files, so today's risk is low but
    unasserted.
  - `_colour_contributor_flat_frame_hash` ignores mode
    (`lrgb_orchestrator.py:527`). With the fallback on, T20 gains R/G/B
    flat keys. A run with T20 forced to PRECALIBRATED then persists a
    non-empty colour `flat_frame_hash` for flats it never used, and toggling
    the flag invalidates the contributor for no pixel change.
- **Fix**:
  - Add "fallback on ⇒ `calibration_index()` and `flat_index()` unchanged"
    invariants for Abell 31, Abell 6 and NGC 3628, not just
    `flat_index()`.
  - Also assert `calibration_index()` is unchanged for M51. The current
    M51 invariant checks only flat keys and `instrument_groups()`, while
    fallback-recognised bias and darks would change `select_dark`.
  - Treat the mode rule for the colour flat hash as part of Step 4 or a
    new step, since Step 4 is what makes it non-harmless.

### F9 (major): Step 5's other mitigations and the missing flat-frame sanity checks

- **Evidence**: there is no flat-quality gate at all. That means no check
  of median ADU range (sampled real medians are about 25k/65k, and the T68
  maximum reaches 52k), no saturation or non-linearity check, and no
  exposure-time floor. T68 flats are 0.085–0.19 s on CMOS, where shutter
  or rolling effects and bias dominance matter. The plan dismisses
  dark-flats as "exposures short", but the "≤ 46 s" figure is not tied to
  any file. T20's 180 s darks give a direct way to measure dark current
  per second against flat signal, and nobody has done it.
- **Fix**:
  - Add a warning-only per-set flat sanity note: median within 15–85% of
    `2^BITPIX` after `BZERO`, the fraction of pixels ≥ 0.95 × max below a
    small bound, and a minimum `EXPTIME`.
  - Make "dark-flats unnecessary" a measured claim: estimate e⁻/s from the
    existing T20 and T68 darks versus the flat signal.
  - Record the result in G11 or remove the ≤ 46 s claim.

### F10 (minor): I3 is answerable from the code, so it should not be left as "not investigated"

- **Evidence**: `lrgb_orchestrator.py` (the comment block above the OSC
  loop, around lines 588-602) documents a bug fixed on 2026-09-16. The
  padded OSC loop wrote an empty-lights OSC hash into the same
  `colour_frame_hashes[contributor_key]` as the mono loop, and that value
  was persisted. `e3b0c44298fc1c14` is the SHA-256 of the empty string,
  which matches this. The NGC 3628 and Abell 31 rebuild is therefore the
  known one-time consequence of that fix, not a new bug.
- **Fix**: state this in I3 and drop it from Q7. Separately, keep the
  observation that `_colour_contributor_frame_hash` hashes the raw view
  for PRECALIBRATED. It still tracks the set, because the raw and
  calibrated copies are 1:1 for T73.

### F11 (minor): G3 and failure mode 2 overstate the invisibility

- **Evidence**: `skill/interview.py:186-196` already renders
  `missing_calibration_warnings()`, deduplicated, including "no Flat
  frames (needed for …)" per telescope and binning. What is actually
  missing is the **consequence** (BLOCKED vs skipped), plus the
  unrecognised frames.
- **Fix**: reword G3 and failure mode 2. Step 3a(ii) should state that it
  replaces or annotates the existing flat-gap lines rather than
  duplicating them. The lines are a parse contract (`_FLAT_RE`).

### F12 (minor): Step 2's `FlatMismatchError(CalibrationFramesMissingError)` inherits "missing" handling

- **Evidence**: `skill/interview.py:125` catches
  `CalibrationFramesMissingError` and renders it as `[BLOCKED]`. Any
  future `except CalibrationFramesMissingError` that means "missing →
  maybe skip" would also swallow a mismatch. Step 2 also duplicates
  Step 1's detection. That is fine, but the sampled header read ("first
  flat plus a sample") can miss a mixed set, which is exactly F1's
  multi-session situation.
- **Fix**: make `FlatMismatchError` subclass `RuntimeError` directly, or
  document why it inherits. Compare geometry on **every** flat, since
  header reads are cheap next to the copy that `stage_frames` already
  does.

### F13 (minor): Step 7's colour-cast guard contradicts `-equalize_cfa`

- **Evidence**: `-equalize_cfa` exists to change the per-CFA-channel
  balance of the master flat ("to avoid tinting", per the binary's help
  text). The guard "per-channel means within the non-flat stack's ratios
  ±5%" can fail precisely because the step works, and SPCC re-balances
  afterwards anyway.
- **Fix**: measure the cast after SPCC, or compare the flat-with-equalize
  result against flat-without-equalize, not against no flat.

### F14 (minor): Step 8(a) scope and staleness

- **Evidence**: narrowband and the boost path share group directories and
  master names with any other run on the same group. `force=True`
  deleting `master_<filter>.fit` also forces a rebuild for a later
  `run_lrgb` or boost run. Step 10's mode flip still leaves narrowband
  masters stale without `--force`, because there is no signature.
- **Fix**: call the shared-master effect out in the step. If Q4 = (a),
  add a Step 10 prerequisite that states the narrowband migration
  explicitly.

### F15 (minor): the plan is written against a dirty working tree

- **Evidence**: `git status` at review time shows uncommitted
  `tests/conftest.py` (the `DESKTOP_DIR` → `PROJECT_DIR_BASE` rename),
  `tests/local_paths.py.example` and `docs/ROADMAP.md`. The plan cites
  `tests/conftest.py:155`, and Steps 1 and 4 add fixtures there.
- **Fix**: add a Step 0: commit or park those changes, re-measure the
  baseline, and then fix the citations.

### F16 (minor): Step 9's "gated test writes only to tmp_path" is not achievable with the current API

- **Evidence**: `build_group_master` always writes to
  `pipeline_dir(project_dir)/group_name` (`master_builder.py:157`).
- **Fix**: say explicitly that Step 9 runs against a scratch **copy** of
  the M42 subset, as the plan's alternative already suggests, or calls
  `run_calibration` / `register_and_stack` directly with a `tmp_path`
  `work_dir`. Drop the `tmp_path` wording for `build_group_master`.

### F17 (cosmetic): wording and citation nits

- T20 flats are all in one flat `Flats/` directory (70 files). The "no
  `T20` token anywhere in the path" statement is right, but the table
  could name the directory. The M42 root has no token, so rule (c) is the
  only one that works there. Say so in Step 4.
- The IC 1396 `lights/iTelescope calibrated/` folder holds 25 unparsed
  `calibrated-Color-T68-…` files as well as the 25 parsed ones. List
  them in §2.1, since they will appear in Step 3a's unrecognised section.
- The Step 1 test count "+3–4 passed" will change once F3 is applied.

---

## Missing decisions to surface to Kaveh (new or changed Qs)

- F1: fix the T21 duplicate-basename staging (a behaviour change that
  rebuilds M51 and re-baselines the SHA), or document 20 effective frames.
- F2: `-norm=mul` for master flats (the same re-baseline; bundle it with
  F1).
- F4: whether the header-fallback flag becomes part of `RunSignature`.

VERDICT: REVISE
