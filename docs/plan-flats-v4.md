# plan-flats-v4: flat-field calibration beyond T21 Luminance

**Status**: rev 9, **FINALIZED**. Converged after eight rounds of
adversarial review (round 8: `docs/plan-flats-v4-review-round8.md`,
verdict CONVERGED, no new findings). Kaveh has since answered every item
in §6, which is now a decisions record, not an open-questions list. No
code has been changed; this revision only resolves §6 and adjusts scope/
sequencing text to match those decisions — it does not re-open any
adversarial gap-hunting.

- **Base**: written 2026-09-22 against `main` @ `22cdf19`. That is
  post-Task-5, so `pipeline.py` no longer exists. All `file:line`
  references point at that commit.
- **Dirty tree**: `tests/conftest.py`, `tests/local_paths.py.example` and
  `docs/ROADMAP.md` carry uncommitted `DESKTOP_DIR` → `PROJECT_DIR_BASE`
  changes. See Step 0.
- **Pending data change**: Kaveh has decided to delete the byte-identical
  duplicate M51 folders, `Uncalibrated Lights - Jan 2025/` and
  `calibrated Lights - T24 - Feb 2025/`. That deletion changes **both**
  T21's and T24's run signatures (§1.5) and rebuilds M51's whole delivered
  image, not just T21. Sequenced in Step 0 / §4.0's event E.
- **Pre-existing, independent finding**: with real data configured, three
  of today's ingest tests assert **post-deletion** M51 counts (T24 raw =
  95, T21 raw = 2, T24 L bin1 = 21) against the tree as it stands **today**
  (still duplicated). Recounted directly: the current tree gives T24 = 190,
  T21 = 4, T24 L bin1 = 42. Those three tests are therefore already red on
  Kaveh's machine, **before this plan changes anything**. This plan does
  not cause it and is not asked to fix it beyond noting it; see Step 0.
- **Scope — decided (§6, Q9)**: Steps 0–8 ship now as the core, immediate
  deliverable (T21 fix, ingest recognition, safety checks, the CLI flag,
  signature fixes) — **no behaviour change to M42/IC 1396/M31 in this
  plan.** OSC flats, narrowband master invalidation, and per-target
  local-flats-vs-iTelescope measurement are explicit, separate follow-ups
  in **Future work** (§5) — measurement is the genuine next step; OSC and
  narrowband signature tracking are not committed to. Kaveh has also
  explicitly decided **against** blind full reprocessing of M42/IC 1396/
  M31 with local flats — see Q9 in §6 for the per-target risk reasoning
  (stale flat libraries, corrupted timestamps, a real dark-temperature
  mismatch) that motivates measuring before reprocessing anything.

**Provenance of v3**: `docs/plan-flats-v3.md` is not in the repo. It was
never committed. Its content survives only in commits `46ce43f`,
`0a7cf33`, `7f03e5c` and `0314e47`, and in docstrings that cite it.
ROADMAP 2.2 and 4.1 still point to it.

---

## 0. TL;DR

- **Flats are already supported for mono RAW_LOCAL groups.** "The pipeline
  does not support flats" is wrong. Flats are wired for Luminance, mono
  R/G/B, narrowband and narrowband-boost groups calibrated locally
  (RAW_LOCAL). Matching is on `(telescope, binning, filter_name)`, exact
  string. Real flats have only ever been used for T21 Luminance on M51.
- **That one real path has two defects:**
  - (a) T21's Luminance flats come from two twilight sessions with
    colliding generic filenames, so staging collapses 30 matched flats to
    **20** (`flat_.seq`: `nb_images=20`).
  - (b) Master flats (and, surfaced separately, bias and dark masters)
    are stacked with Siril's default normalisation ("addtive with scale",
    sic), not the `-norm=mul` (or `-nonorm`) that Siril's bundled scripts
    use.

  Fixing either changes pixels without changing any signature hash today.
  A mode-aware `calibration_recipe` mechanism (Step 2) is added first,
  unconditionally, so the actual fix (Step 3b) is a single, clean
  trigger. **Decided (§6, Q1): fix now** — Step 3b ships unconditionally,
  batched into the same M51 rebuild that Step 0's deletion already
  forces (below).
- **Deleting the M51 duplicate folders (already decided) rebuilds the
  entire delivered M51 image**, not just T21: it changes T24's Luminance
  and both colour-contributor hashes too (verified, §1.5). This is folded
  into the same single regeneration event (E) as Step 3b's fix, with a
  before/after snapshot and a "free" pixel-identity check on T24 (§4.0).
- **OSC never uses flats.** RAW_LOCAL OSC is unreachable: `build_osc`
  passes `cal_index={}`, which raises `KeyError`. Fixing this is real
  work; it is **Future work** (§5), and **decided (§6, Q5): deferred, not
  committed to** — revisit only if IC 1396's own eventual measurement
  shows a real benefit, itself lower-priority given its corrupted 1970
  calibration timestamps.
- **The blocker for other telescopes is ingest.** It does not see three
  real raw flat libraries (M42/T20, IC 1396/T68, M31/T05) or their
  bias/darks. Recognising those bias/darks would, by today's
  `infer_calibration_mode` rule, **flip** those telescopes from
  PRECALIBRATED to RAW_LOCAL. The plan decouples recognition from the mode
  decision (Step 4), and ships no CLI or default path that actually
  exercises local flats on those telescopes beyond an explicit override
  (`--calibration-mode`, added in Step 5). **Decided (§6, Q2): Keep** —
  header-recognised bias/darks never auto-flip the mode; PRECALIBRATED
  stays the default everywhere it is today, permanently, not just for
  this round.
- **Siril's silent warnings are not detected.** `NOT USING FLAT:` and
  `NOT USING OFFSET:` (exit code 0) pass unnoticed, so
  `flat_corrected=True` can be false.
- **Challenge.** No finished target of Kaveh's renders differently from
  flat work alone: every one either uses iTelescope's server-side flats
  (`CALSTAT=BDF`/`BF`) or has none (T24). The value here is correctness
  and honest reporting on the one real flat-corrected master, plus safe,
  inert recognition of calibration data the pipeline currently cannot see
  at all. Whether local flats are *worth* using instead of iTelescope's is
  a measurement question — **decided (§6, Q3/Q9): yes, do the
  measurement, per target, as the explicit next step after Steps 0–8
  ship** — but Kaveh has also decided **against** committing to blind
  full reprocessing of M42/IC 1396/M31 the way M51/T21 was fixed: each
  target carries its own real risk (M42's flat library is 21 months
  older than its lights; IC 1396's calibration timestamps are corrupted;
  M31's flats are fresh but its darks are at the wrong temperature, G15,
  unfixed), so reprocessing happens only where a target's own measurement
  shows a genuine, quantified improvement — never blind.

---

## 1. Current state (with evidence)

### 1.1 What exists

| Piece | Where | What it does |
|---|---|---|
| Flat filename parse | `ingest.py:116-120` `_FLAT_RE_SKYFLAT` | Matches `scope_<Filter>_<b>x<b>_skyflat<N>.fit`.<br>The telescope comes **only** from the path (`ingest.py:473-481`).<br>With no hint, `classify_frame` returns `UnrecognizedFrame` early (`ingest.py:505-513`), before any header read. |
| T24-style flat | `ingest.py:101-106` | No filter token, so `filter_name=None`.<br>Dropped by `flat_index()` (`:335-345`) via `logger.warning` only. |
| Telescope inference | `ingest.py:71,416-424` | `^T\d+$` must be an entire ancestor directory name. |
| Flat index | `ingest.py:301-348` | Keyed on `(telescope, binning, filter_name)`.<br>No session, date, exptime, rotation, geometry, `INSTRUME` or temperature dimension. Sessions are merged. |
| Missing-flat warning | `ingest.py:350-404` | Per filter.<br>Its wording is a parse contract with `skill/interview.py:56,86`, which renders it deduplicated (`interview.py:186-196`). |
| Policy | `calibration.py:87-89`; `calibration_policy.py:12-39` | `REQUIRE` if the telescope has any flat, else `SKIP_IF_MISSING`.<br>Per-run override only. No CLI flag. |
| Calibration mode | `calibration_policy.py:42-73` | PRECALIBRATED iff `(no bias or no dark) and calibrated lights exist`. Per telescope; **no binning argument at all**. Otherwise RAW_LOCAL. |
| Master bias/dark | `calibration.py:451-486` `build_master` | `stack <seq> rej 3.0 3.0 -out=master`, with **no normalisation flag**. Siril therefore applies its default ("addtive with scale"). The bundled `Mono_Preprocessing.ssf` uses `stack bias/dark rej 3 3 -nonorm`. See I6. |
| Master flat | `calibration.py:489-557` | `convert`, then stage the bias, then `calibrate -bias -prefix=bc_`, then `stack bc_flat_ rej 3.0 3.0 -out=master` (`:540-553`). **No `-norm`** (the bundled scripts use `-norm=mul`).<br>The flat's calibrate result is not log-checked.<br>Bias-only (no dark-flat). Rebuilt per group. |
| Light calibration | `calibration.py:378-448,575-736,739-829` | `-flat=masterflat` is appended to the chosen dark/bias mode.<br>`flat_corrected = master_flat is not None` (`:827`). |
| Staging | `staging.py:17-29` | Copies by basename into one directory. Colliding names silently overwrite each other. This applies to lights, bias and dark as well as flats (I5). |
| Group wiring | `master_builder.py:79-248` | `work_dir = pipeline_dir(project_dir)/group_name` (`:156`).<br>`usable()` + `PLTSOLVD` skip (`:159`).<br>Flat count is logged only when flats exist (`:206,219-224`). |
| Resumability | `run_signature.py:128,255,314`; `contributor_staleness.py:57-100` | `flat_frame_hash` is a hash of flat **basenames**, duplicates included. The empty set hashes to `""`.<br>`calibration_mode` sits on `ContributorSignature`: it is ignored by `contributor_stale` but compared by `diff_invalidation`'s dict equality (`:314`). |
| Test fakes | `test_pipeline.py:448-450,1469-1475` and similar in `test_run_signature.py`/`test_skill_interview.py` | Fake `calibration_index()` maps return plain lists of strings, not `CalibrationFrame` objects. Any code reading a frame **attribute** (not just its dict key) must tolerate this. |

### 1.2 Which flows use flats

| Flow | Flats? | Evidence | Notes |
|---|---|---|---|
| Mono Luminance | Yes | `lrgb_orchestrator.py:337-348,374-379` | RAW_LOCAL only. |
| Mono R/G/B | Yes | `colour_contributor.py:249-254`; `lrgb_orchestrator.py:513-517,527` | RAW_LOCAL only. The colour flat hash (`:527`) ignores the mode (G10) — **verified inert on real data today**: every persisted colour/Luminance `flat_frame_hash` on Abell 31, Abell 6 and NGC 3628 is `""`. |
| Narrowband SHO/HOO | Yes | `narrowband_orchestrator.py:118,131-134`; `narrowband_filters.py:110-115` | RAW_LOCAL only. No staleness tracking. `force` deletes only the final products (`:122-127`). |
| Narrowband boost | Yes | `master_builder.py:366-372` | RAW_LOCAL only; `usable()` only. |
| OSC | **No** | `colour_contributor.py:393-398`; `lrgb_orchestrator.py:614,618` | RAW_LOCAL raises `KeyError` at `master_builder.py:176`. Deferred (§5). |
| PRECALIBRATED (T73, T02, T59, T20 today) | No, by design | `calibration.py:286-375` | `CALSTAT=BDF`/`BF`. |

### 1.3 Failure modes today

1. **REQUIRE, flat missing for one filter.**
   `CalibrationFramesMissingError` is raised mid-run
   (`calibration.py:792-796`). The interview shows the gap line but not the
   consequence (darks get `[BLOCKED]`; flats do not).
2. **SKIP, no flat.** The gap line is shown. No "no flat applied" note is
   written to `notes`.
3. **Flats present but unrecognised.** They become `UnrecognizedFrame`,
   which is never rendered. The telescope looks flat-less and is SKIPped
   silently.
4. **Silent Siril drops.** Siril emits `NOT USING FLAT:` (6 variants in
   `siril-cli.exe`) and `NOT USING OFFSET:` (6 variants; OFFSET is the
   bias) with exit code 0. `_check_calibration_log`
   (`calibration.py:191-222`) catches neither. It is also never run on
   `build_master_flat`'s own calibrate.
5. **Duplicate flat basenames (live on M51).**
   - Luminance lives in two of the 12 session folders under
     `Calibrations/T21/Flats/2024 06/raw flats/`:
     - `20240616_080204`: `skyflat0..19`, 5.43–7.06 s, 02:02 UT;
     - `20240616_080536`: `skyflat0..9`, 4.91–5.75 s, 02:05 UT.
   - These are **distinct** exposures with 10 colliding names.
   - The staged `flat/` directory holds 20 frames, in an order that depends
     on `rglob`.
   - The name hash is blind to all of this.
6. **Flat normalisation.** Sky-flat levels vary (sampled medians: T21 L
   about 13% session-to-session spread, T68 about 25%). Default
   additive-with-scale normalisation biases sigma rejection and the
   vignetting profile.
7. **Filter-less T24-style flats are dropped invisibly.**

### 1.4 Flat-matching semantics

- Telescope, filter (case-sensitive; narrowband aliases only) and binning
  are all exact.
- Date/session, rotation, `NAXIS`, `INSTRUME` and `SET-TEMP` are ignored.
- Normalisation is Siril's default.
- Flats are bias-only; there is no dark-flat.

### 1.5 M51 run-signature facts that constrain every step

- **Duplicated T21 hash.** The persisted T21 `frame_hash` is
  `83bc8bee12b82922`.
  - That equals `frame_identity_hash([a, b, a, b])`, the two T21 raw
    Luminance names each counted **twice** (verified directly).
  - `frame_identity_hash([a, b])` is `c2b76b333110b4a6`.
- **The deletion changes T24's hash too — verified directly, not
  "very likely":**

  | Group | Current (duplicated) tree | Post-deletion |
  |---|---|---|
  | T24 Luminance BIN1 | 42 names, hash `d2f430e6232fd176` (**equals the persisted value**) | 21 names, hash `de55083822298fad` |
  | T24 R/G/B, BIN1 and BIN2 | all doubled | all change |
  | T21 Luminance | 4 names (2×2), hash `83bc8bee12b82922` (**equals the persisted value**) | 2 names, hash `c2b76b333110b4a6` |

  - **Both colour contributors** (`T24_bin1`, `T24_bin2`) and the
    **selected** Luminance therefore go stale at the same moment.
  - `stage_frames`'s basename collapse (I5) means the **staged** file sets
    for T24 groups are byte-identical before and after the deletion (42
    duplicated names collapse to the same 21 files on disk either way; the
    same holds for R/G/B). The `n >= 10` quality-filter threshold sits on
    the same side both times (42→21, 28→14, 24→12 are all still ≥ 10 or
    both above/below consistently — checked per group in Step 0). **The
    rebuilt T24 masters should therefore be pixel-identical to today's**,
    within Siril's own run-to-run determinism. That is a free correctness
    oracle this plan uses (§4.0, E step 4).
  - The origin of the duplication: `Uncalibrated Lights - Jan 2025/`
    repeats `Lights/`'s zips; `_extract_zipped_lights` appends a
    `LightFrame` per zip and `_instrument_groups_from` does not
    deduplicate. `calibrated Lights - T24 - Feb 2025/` (92 files)
    duplicates `M51 from Jean-Marie/` the same way.
- **The Step-17 "oracle" is not a stored fixture.**
  `test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`
  (`tests/test_pipeline.py:1386-1405`) hashes the current on-disk
  `lrgb_final.fit`/TIFF, calls `_run_m51()` and asserts the bytes are
  unchanged.
  - On a resume, `usable()` skips every master.
  - It is therefore an **invalidation oracle**: it proves nothing was
    invalidated. It says nothing about calibration-code pixel changes.
  - This test, and 3 siblings (`test_run_lrgb_stop_after_masters_…`,
    `test_run_lrgb_force_final_only_touches_final_…`, and one more in the
    same module), are real-M51-pipeline tests: they run `_run_m51()` for
    real. **They must not run before event E** (§4.0), or they will
    themselves trigger the very regeneration E is meant to control and
    checkpoint.
- **Pre-existing, unrelated to this plan**: three ingest tests
  (`test_scan_real_multi_telescope_session`,
  `test_instrument_groups_merges_users_sharing_telescope_and_binning`, and
  the flat-warning count test near `test_ingest.py:406`) assert the
  **post-deletion** counts against the tree as it is **today**
  (pre-deletion). Recounted directly on the current tree: T24 raw = 190
  (not 95), T21 raw = 4 (not 2), T24 L bin1 = 42 (not 21). These three
  tests are already red on Kaveh's machine before this plan touches
  anything. They will turn green the moment Step 0's deletion happens,
  with no code change. Surfaced in Step 0; not a target of this plan.

---

## 2. Real-data inventory (`D:\Raw Photo Backups\iTelescope\`, read-only)

**Method**:
- Every `.fit/.fits/.fts` outside `_pipeline/` and outside the two
  duplicate M51 folders went through `classify_frame` plus an astropy
  header read. Zip entries were classified by name.
- `scan_session` was not used on M51, where it would write
  `_extracted_zips`. M42's only zip is corrupt (`BadZipFile`), so
  `scan_session` writes nothing there.
- Total: 2047 entries. Scripts and output are in the session scratchpad.
- **The invariant tests will see the tree as it is at test time.** Until
  the Step-0 deletion they include the duplicates.

### 2.1 Flats vs lights

| Target | Telescope | Camera (raw `INSTRUME`) | Raw lights | Flats on disk | Recognised today? | Match quality |
|---|---|---|---|---|---|---|
| M51 | T21 | FLI6303 3072×2048 | L BIN1 × 2 (zips), 300/600 s, 2025-01 | 330 raw skyflats: 11 filters BIN1, 12 session folders, 2024-06-17. **L is split over 2 sessions with 10 colliding names.**<br>Plus 11 `Master_Flat*` (`CALSTAT=M`). | Yes (330); masters no | Match; about 7 months old. **Effective master is 20 of 30 frames.** |
| M51 | T24 | KAF16803 | L BIN1 21; R/G/B BIN1 (jmwill) 14/12/12; BIN2 12 each | none | n/a | Binding: no T24 flats. jmwill's `calibrated-` BIN1 copies are `CALSTAT=BDF` (Q8). |
| M42 | T20 | SBIG STL-11000, −15 °C | L BIN1 10; R 8/G 6/B 7/Ha 15/OIII 14/SII 13 BIN2 | One `Flats/` directory, 70 files: 10 each of L/1 and R/G/B/Ha/SII/2; OIII 9 + 1 truncated.<br>Dated 2020-03-19.<br>Exposures: SII 43.2–46.0 s; **Green 40.5–42.0 s vs Red 4.3 s, Blue 9.9 s** at similar medians (~27k vs ~25k ADU). | **No.** No `T20` token in any **directory** name (bias filenames do carry a `T20` token, e.g. `Bias-Darks-T20-001-bias-B1.fit`). Rule 4(c) is the only attribution route for the flats/darks. | Match on filter/bin/NAXIS/`INSTRUME`/temperature. **About 21 months old.**<br>Also bias B1 50 / B2 49 (+1 truncated); darks B1 180 s × 10, B2 180/300 s × 20. All unrecognised. |
| IC 1396 | T68 | ASI 6248×4176, gain 25, −10 °C | Color BIN1 × 25, 240 s, 2021-09 (+25 parsed `calibrated-`, +25 **unparsed** `calibrated-Color-T68-…`) | Color BIN1 × 88, 0.08–0.19 s (`flats2/20201129_063356`) | **No** | Match. Calibration `DATE-OBS=1970` (clock unset).<br>Bias × 48 and **darks 240 s × 50**, both unrecognised. |
| M31 | T05 (folder `T5`; user token `Local Admin`) | SBIG ST-10 | R/G/B BIN1 × 7, 180 s, 2021-08-08 | R/G/B BIN1 × 40, 2021-07-20 | **No** | 19 days old, no colliding basenames (120 flats, 0 duplicate names — the only real project without this problem).<br>Bias × 15 unrecognised. Darks × 16 at 180 s are **−15 °C/2019** vs −10 °C lights.<br>Attribution rule (b)'s `T5`→`T05` normalisation is **load-bearing** here: the bias filenames literally say `T5`, not `T05`. |
| NGC 3628 | T73 | ASI 6248×4176 | L BIN1 13; R/G/B BIN2 12 (PRECALIBRATED, `BF`) | 4 `Master_Flat` (BIN1) + 2 `Master_Bias`, all `CALSTAT=M` | No (`flat_index()` empty) | BIN1 vs BIN2; irrelevant while PRECALIBRATED. |
| NGC 3628 | T68 (2023, jmwill `LeoTriplet from Jean-Marie/`) | raw: ASI **4944×3284**, gain 0 | Color BIN1 × 44 parsed raw (+44 parsed `calibrated-`, +43 unparsed `calibrated-Color-…` with `INSTRUME='ASCOM'`) | none | n/a | Same telescope ID, different camera from 2021. The raw `INSTRUME` string is identical to 2021's ("ASI Camera (1)"), so **only `NAXIS` discriminates**. Never co-scanned with the 2021 IC 1396 data in one `scan_session` call (different project folders), so this mismatch has no real production trigger today (§4.0/§5). |
| Abell 31 / Abell 6 | T59 / T02 | — | `CALSTAT=BDF` | none | n/a | PRECALIBRATED. No unrecognised calibration frames. |
| Others | — | — | no parseable raw lights | none | n/a | Out of scope. |

Note: `calibrated-` lights rewrite `INSTRUME`. Examples: T20
`T20 SBIG STL11000M`, T05 `T05-SBIG-ST10XME`, T68 (2023) `ASCOM`.
Compatibility checks (§5) must use **raw** light headers only.

### 2.2 iTelescope library facts

- Flat libraries are per telescope/filter/binning, at each filter's
  default binning.
- Session folders are timestamps. Filenames repeat across sessions, even
  within one filter (T21 L).
- `calibrated-` lights are `CALSTAT=BDF` (T59, T02, T68, T05, T20, T24)
  or `BF` (T73). iTelescope holds flats server-side for every telescope,
  including T24.
- Library age spans 19 days (T05) to 21 months (T20).
- Headers always carry `IMAGETYP`, `XBINNING` and (for flats) `FILTER`.
  Bias `EXPTIME=0.0`; darks are exactly 180.0/240.0/300.0 today.

### 2.3 Before-picture measurement (read-only)

**Method**: 8% boxes, 3% margin, sigma-clipped medians, pedestal removed.

| Artefact | Corner / centre (mean) |
|---|---|
| T21 L master flat (20-frame, default normalisation) | 0.764 |
| T21 L stacked, no flat | 0.694 |
| T21 L stacked, with that flat | 0.929 |

- The centre box sits on M51, so these numbers are for A-vs-B comparison
  only.
- The provenance of v3's 0.7735 is unknown.
- `_flat_frame_hash(T21 L)` recomputes to the persisted
  `349469062e57762c`, computed over 30 names including duplicates. It is
  unchanged by any staging or normalisation fix.
- The **master flat itself** is Siril's normalised 32-bit float, in
  `[0, 1]`: verified directly, median 0.354 (`BITPIX=-32`). A "level as a
  fraction of 65535" check must not be run on the master (old Step 7,
  now revised, R3-13) — multiplying back by 65535 recovers the ADU scale
  (0.354 × 65535 ≈ 23202, consistent with the raw per-frame medians below
  the ~13% spread figure in §1.3.6, which describes session-to-session
  spread, not absolute level).

---

## 3. Gaps (prioritised)

| # | Pri | Gap | Kind | Evidence | Real data |
|---|---|---|---|---|---|
| G1 | CRIT | T21 L master flat uses 20 of 30 distinct frames; the hash is blind to it | code, live | §1.3.5 | M51 |
| G2 | HIGH | Master flat stacked without `-norm=mul` | code | `calibration.py:550`; Siril help text; bundled `.ssf` | M51 and all future flats |
| G3 | HIGH | `NOT USING FLAT/OFFSET` undetected; flat calibrate unchecked | code | `calibration.py:191-222,549-553,827` | mocked; T21 |
| G4 | HIGH | Real calibration layouts and names invisible | code | `ingest.py:71,101-120,416-424,505-513` | M42, IC 1396, M31 |
| G5 | HIGH | Recognising bias/darks flips `infer_calibration_mode` implicitly | code/design | `calibration_policy.py:66-73` (no binning arg; per-telescope only) | M42, IC 1396, M31 |
| G6 | HIGH | Consequences invisible: unrecognised frames, BLOCKED vs skip, "no flat applied" | code | `interview.py` `render`; `master_builder.py:206,219` | all |
| G7 | HIGH (bug) | `build_osc` passes `{}` (`KeyError`); OSC flats never used; `-cc=dark` without `-cfa` | code | `colour_contributor.py:393-398`; `master_builder.py:176,188-203`; bundled `OSC_Preprocessing.ssf` | IC 1396. **Deferred to §5.** |
| G8 | LOW/defer | No flat↔light geometry check (raw headers; `NAXIS` is the discriminator) | code | §2.1 (T68 2021 vs 2023; never co-scanned) | **No real production trigger. Deferred to §5.** |
| G9 | MED | No flat sanity notes (level, saturation, exposure, age); the naive "fraction of 65535" check is wrong on a Siril master (§2.3) | code | — | all |
| G10 | MED, **verified inert on real data** | Colour flat hash (and any future recipe field) ignores mode | code | `lrgb_orchestrator.py:527`; every persisted real value is already `""` | M42, once Step 4 is live |
| G11 | LOW/defer | Narrowband/boost masters never invalidated; `--force` does not delete them | code | `narrowband_orchestrator.py:122-127` | M42. **Deferred to §5; not flat-specific.** |
| G12 | LOW | Stale docs (full list in Step 8) | docs | — | — |
| G13 | LOW | No `--flat-policy` / fallback / `--calibration-mode` CLI flags | code | `run_stage.py:51-70`, `run_narrowband.py:25-36` | — |
| G14 | LOW/defer | BIN1→BIN2 flat binning; `Master_Flat*` use; dark-flats | code + data | — | **Mixed, corrected (round 7, minor — this row previously said "Deferred to §5" for all three, contradicting the "Deliberately out of scope" list below, which names two of these three items with the opposite disposition; verified directly against §5's actual five bullets, neither appears there):** dark-flats **only** — deferred to §5 (its "Dark-current / dark-flat-necessity measurement" bullet decides whether they're ever needed). BIN1→BIN2 flat binning and `Master_Flat*` use — **genuinely out of scope**, not deferred anywhere; see the "Deliberately out of scope" list at the end of §4. |
| G15 | INFO | `select_dark` ignores `SET-TEMP` | code | `calibration.py:124-188` | M31 |

### Incidental findings (not bundled; for decision)

- **I1**: the `build_osc` `cal_index={}` bug. It predates flats. **Deferred
  to §5** along with the rest of OSC.
- **I2**: T68 has 50 × 240 s darks. Once seen (under an explicit override;
  see Step 4), T68 would go through `-dark= -cc=dark -debayer` without
  `-cfa`, which has never been run. Also, docs and tests wrongly say T68
  has no darks (G12). Fixing the command shape is part of §5's OSC work.
- **I3** (explained): the persisted colour `frame_hash`
  `e3b0c44298fc1c14` (SHA-256 of "") on NGC 3628 `T73_bin2` and Abell 31
  `T59_bin2` is residue of the padded-OSC-loop bug fixed in `0efdeca`
  (`lrgb_orchestrator.py:591-606`). The next run rebuilds those
  contributors once. Separately, `_colour_contributor_frame_hash` hashes
  the raw view for PRECALIBRATED contributors; that is still 1:1.
- **I4**: `diff_invalidation` compares the whole `luminance` dict
  (`run_signature.py:314`). A change to a **non-selected** contributor
  (T21) therefore regenerates M51's finals.
- **I5**: `stage_frames` basename collapse also applies to lights.
  - M51's duplicated zip lights (§1.5) are silently deduplicated at
    staging — this is exactly why the T24/T21 masters should be
    pixel-identical after the deletion (§1.5, §4.0).
  - The hash, meanwhile, counts them twice.
- **I6**: bias and dark masters are stacked with Siril's default
  normalisation ("addtive with scale"). The bundled reference uses
  `-nonorm`. These masters feed M51's **delivered** image: the
  selected T24 Luminance and both colour contributors. **A no-code
  measurement is scheduled before event E** (Step 3, sub-bullet) so Q7 can
  be answered with numbers before E runs, whether or not the change itself
  is accepted this round.

---

## 4. Plan

### 4.0 Global rules, test protocol, and the M51 regeneration event

**Suite**: one commit per step, with the full suite before and after —
**with the real-M51-pipeline tests deselected until event E** (see
protocol below).

- The baseline is re-measured in Step 0. ROADMAP's unconfigured baseline
  is 288 passed / 35 skipped; Step 0 also records the **configured**
  baseline (real `local_paths.py`), which today is red on the three
  pre-existing tests named in §1.5 and will need a second baseline
  recorded once those turn green after the deletion.
- Expect "+passed" for mocked tests and "+skipped" for real-data tests.

**Real-data suite protocol** (fixes R3-1; enforced by config, not memory
— F2, round 5):

- Mark the four real-M51-pipeline tests
  (`test_run_lrgb_full_run_after_staged_calls_reproduces_slice3_output`
  and its three `stop_after`/`force` siblings in `test_pipeline.py`) with
  a `pytest.mark.m51_pipeline` marker, as part of Step 0.
- **The exclusion must be the default, not something typed by hand on
  every invocation.** `pyproject.toml`'s `[tool.pytest.ini_options]` has
  no `addopts` or `markers` today (verified: it is exactly
  `testpaths = ["tests"]`), so relying on everyone remembering
  `pytest -m "not m51_pipeline"` across the roughly nine step invocations
  between Step 0 and event E is exactly the kind of "forgot the flag"
  risk that would trigger the very unsnapshotted regeneration E exists to
  prevent — a bare `pytest`, or an IDE's own "run tests" button that
  doesn't carry custom CLI args, would silently run the four real tests
  against live Siril on the real, post-deletion M51 tree. Step 0
  therefore adds, in `pyproject.toml`:
  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  markers = [
      "m51_pipeline: real Siril run against the live M51 project; excluded by default, see docs/plan-flats-v4.md §4.0",
  ]
  addopts = ["-m", "not m51_pipeline"]
  ```
  A plain `pytest` then excludes the four tests **by construction**, from
  Step 0 through Step 5, with no flag to remember.
- Only event E itself runs the four marked tests, and only **after** E's
  own procedure has already rebuilt and verified M51 (below). E's own
  commit explicitly overrides the default for that one run
  (`pytest -m m51_pipeline`, or `-o addopts=""`) and, as part of the
  **same** commit, **removes the `addopts` line from `pyproject.toml`**
  (the `markers` registration itself can stay) — so ordinary `pytest`
  invocations resume covering those four tests once E has actually run,
  rather than silently excluding them forever.
- The three pre-existing ingest-count tests (§1.5) are **not** deselected:
  they are expected to be red until Step 0's deletion and green
  immediately after, with no code change. Confirm that transition in Step
  0 itself, separate from any of this plan's own code changes.

**Gates**, three kinds, each named per step:

- **(P) Pixel check.** Real data, Siril, cheap, run on Kaveh's machine.
  - `run_calibration` for T21 L runs into a `tmp_path` `work_dir` with the
    real 2 lights, the matching bias/dark and the 30 matched flat
    `CalibrationFrame`s.
  - The resulting `flat/master.fit` and calibrated `pp_*` are compared
    with M51's persisted `_pipeline/T21-…-Luminance-bin1/flat/master.fit`
    and the calibrated lights.
  - **Step 1b measures Siril run-to-run determinism** with **at least 3**
    `tmp_path` runs, plus one comparison of a `tmp_path` run against the
    persisted reference (a different process/session). The bound is 2×
    the maximum observed tmp-vs-tmp `Δ`; if tmp-vs-persisted exceeds that
    bound, fail loudly rather than silently widening the tolerance.
  - After Step 3b the reference switches to the new recipe's output,
    recorded in 3b.
- **(I) Invalidation oracle.** The existing M51 real-data module:
  `stop_after`, `force`, and the full-run "bytes unchanged after resume"
  test — the four tests marked `m51_pipeline` above. It proves nothing was
  invalidated. It cannot see calibration-code pixel changes (§1.5). It
  must not run before E.
- **(D) Data invariants.** Test-only; no Siril. They go through the new
  pure classifier (Step 4a).
  - Assert `flat_index()`/`calibration_index()` keys and counts, flat
    unique-basename counts, `_flat_frame_hash(T21 L)`, and (from Step 4b)
    `infer_calibration_mode`/`infer_flat_policy` for **every** telescope,
    with the fallback flag **off and on**.
  - Projects: M51, NGC 3628, Abell 31, Abell 6, M42, IC 1396, M31.
  - Pinned against the **post-deletion** tree (§4.0 event).

**The single M51 regeneration event (E)**, done once on Kaveh's machine:

- **Triggers**, whichever apply, batched:
  - the duplicate-folder deletion (always; it changes T21/T24 frame
    hashes — §1.5);
  - Step 3b (**decided: ships**, Q1 = fix now — always batched into E);
  - I6 (only if Q7 = change; the no-code measurement in Step 3 answers
    this before E, whichever way it comes out).
- **Procedure**:
  1. **Snapshot**: copy `_pipeline/final/`, `_pipeline/checkpoints/`,
     `_pipeline/run_signature.json`, and every group's
     `lights/master_*.fit` (T21 and both T24 binnings) to a location
     outside the project tree, so there is something to compare against
     and fall back to.
  2. Record SHA-256 of the current `lrgb_final.fit` and `M51_lrgb.tif`.
     Record the persisted `run_signature.json` verbatim. Record
     per-contributor `STACKCNT`, median FWHM
     (`contributor_fwhm_arcsec`), `luminance_selected` and
     `colour_reference`.
  3. Apply the triggers: code merged, folders deleted.
  4. Run one full `run_lrgb` for M51, using the same parameters
     `scripts/run_m51.py` uses (call `run_lrgb` directly from a scratch
     script with those arguments — do **not** edit the tracked
     `scripts/run_m51.py`, whose `PROJECT_DIR` placeholder stays as-is).
  5. **Check `luminance_selected` and `colour_reference`.** If either
     changed, or the FWHM ranking flipped, **stop and ask Kaveh** before
     accepting (R2-8) — do not silently keep the new selection.
  6. **Free pixel-identity check (R3-2), with an explicit branch for
     whether I6 rode along on this run of E (round 7, major — the check
     as originally written only accounted for the deletion trigger, not
     I6, one of this same event's own three listed triggers above)**:
     compare the rebuilt T24 Luminance and both T24 colour-contributor
     masters' pixel data (`np.array_equal`, or the Step-1b determinism
     bound if not exactly equal — plate solving rewrites WCS headers, so
     compare data arrays, not headers) against the Step-1 snapshot.
     - **If I6 was NOT part of this run of E** (Q7 = leave, or Q7 = change
       but batched into a later, separate E): the masters are expected to
       be pixel-identical, because the staged T24 file sets are
       byte-identical before and after the deletion (§1.5) and nothing
       else in this run touches `build_master`. A mismatch here means
       something **other** than the deletion changed T24's pixels, and
       must be investigated before proceeding.
     - **If I6 WAS part of this run of E** (Q7 = change, merged at
       procedure step 3 above): T24's bias/dark masters — and everything
       built from them — are **expected to differ**, deliberately, by
       design; `build_master` is I6's own target, not per-telescope, so
       T24 is exactly as affected as T21. The expected magnitude is not
       "identical" but **the Step-3 I6 measurement's own reported pixel
       difference** (the number recorded there when Q7 was answered),
       plus the Step-1b determinism bound as slack for ordinary Siril
       run-to-run noise. Only a difference **beyond** that combined
       envelope is the actual fault signal here — do not flag the
       expected, already-measured, already-approved change as something
       to investigate; that would waste time re-deriving a number this
       plan already has, or worse, cast doubt on a deliberate, wanted
       fix.
  7. Record the new hashes, signature, STACKCNTs and FWHMs in a
     **follow-up ROADMAP commit** (not by amending the triggering
     commit's message, which is already merged by the time E runs).
  8. Only then run gate (I) (the four real-M51 tests, via
     `pytest -m m51_pipeline` or `-o addopts=""` for this one invocation)
     and re-pin gate (P)'s reference. **In the same commit**, remove the
     `addopts = ["-m", "not m51_pipeline"]` line from `pyproject.toml`
     (keep the `markers` registration) — from this point on, a plain
     `pytest` covers the four real-M51 tests again, same as any other
     test.
  9. Mention the now-stale `_index.json` at the M51 project root (built
     July, references the deleted folder 111 times; it has no code
     consumer today, `workspace.index_path` is unused) — regenerate or
     delete it, at Kaveh's option, in the same pass.
- **Sequencing**: Step 0 deletes the folders immediately (that part does
  not wait). What waits for E is running any real M51 **pipeline** test or
  a real `run_lrgb` call. Steps 1–4 rely on gates (P) and (D) only.

**Hygiene**:
- Checkpoint labels are untouched.
- **Monkeypatch drift**: grep before each step for patches of
  `run_calibration`, `build_master_flat`, `build_group_master`,
  `run_script`, `scan_session` and `fits.getheader`. New header reads go
  through an injectable reader. Watch wall-clock time.
- Real paths come only via `tests/local_paths.py` or
  `ASTRO_PIPELINE_ITELESCOPE_DIR`.

### Step 0: clean base, confirm the pre-existing gap, delete (no production code)

- Kaveh commits or parks the pending `conftest.py`/example/ROADMAP edits.
- Add the `m51_pipeline` pytest marker to the four real-M51-pipeline
  tests, **and** add the `markers`/`addopts` entries to
  `pyproject.toml` (§4.0, F2) so the exclusion is the default for every
  plain `pytest` invocation from here through Step 5, not something
  anyone has to remember to type.
- Re-run the suite (a bare `pytest` now suffices; `addopts` supplies the
  exclusion) and record the baseline (unconfigured, and
  configured-but-not-yet-deleted).
- Confirm the three pre-existing ingest-count tests are red on the current
  tree (§1.5) — this is expected and independent of this plan.
- Re-check this plan's `conftest.py` citations.
- Kaveh deletes the duplicate M51 folders now.
- Re-run the three ingest-count tests: they should now be green with zero
  code changes. Record this transition.
- **Do not** run any real M51 pipeline test yet; see E.

### Step 1a: data invariants (test-only; provably behaviour-free)

- **Change**:
  - Add fixtures for Abell 31, M42 and M31 to `conftest.py`, via
    `_under(_ITELESCOPE_DIR, …)` like IC 1396.
  - Add gate-(D) tests, fallback-off form, for all seven projects.
  - M51 assertions, on the post-deletion tree:
    - 11 T21 BIN1 flat keys, 30 paths each;
    - T21 L has **20 unique basenames**;
    - `_flat_frame_hash(T21 L) == "349469062e57762c"`;
    - the `calibration_index()` snapshot;
    - `instrument_groups()` counts, with T21 L bin1 = 2 and T24 L bin1 =
      21 (extending, not re-pinning, the existing real ingest tests per
      R3-1's fix).
- **Files**: `tests/conftest.py`, `tests/test_ingest.py`. No `src`
  change.

### Step 1b: detect Siril silent drops, and measure determinism (G3)

- **Change**:
  - `_check_calibration_log` matches the **prefix**
    `NOT USING (DARK|FLAT|OFFSET):` rather than an enumerated list.
  - `build_master_flat` captures its `SirilResult` and runs the same
    check.
  - Set up gate (P)'s determinism measurement per §4.0 (≥ 3 tmp runs plus
    one tmp-vs-persisted comparison; bound = 2× max observed tmp-vs-tmp).
- **Tests**:
  - Mocked: the 5 DARK, 6 FLAT and 6 OFFSET real strings each raise
    (tested against the prefix regex, not an enumerated list, so a 7th
    future variant is caught too). Clean logs pass. `build_master_flat`
    with an OFFSET line raises.
  - Real: gate (P) determinism measurement, recorded in this doc.
- **Behaviour change**: silent drops raise. "No real run hits this" is
  unverified, because Siril logs are not persisted; gate (P) is the
  evidence.
- **Gates**: D, P.

### Step 2: mode-aware `calibration_recipe` helper (fixes G10; ships unconditionally)

Moved out of the old Step 3b and made independent of Q1, because it is
verified behaviour-free on every real persisted signature today (§1.2)
and Step 5's hash guarantees depend on it regardless of which way Q1 goes
(R3-3).

- **Change**:
  - One helper, `calibration_recipe_parts(calibration_mode, flats) ->
    (hash, recipe)`, replacing the ad hoc `_flat_frame_hash` call sites.
    Named `..._parts` (R4-6), not `calibration_recipe`, so the function
    isn't confused with the single-string field described below — the
    function's first return element feeds the *existing* `flat_frame_hash`
    field, and only its second element feeds the new `calibration_recipe`
    field; giving the function and one field of its own return value the
    identical name invited exactly that confusion.
    - Returns `("", "")` for PRECALIBRATED, or for an empty flat set.
    - Otherwise returns `(frame_identity_hash(flat_basenames), "")` —
      note the **recipe half stays `""`** until an actual behaviour
      change ships (Step 3b, or later I6/OSC work in §5), so this step
      changes no persisted value on any real project (verified: every
      real flat/colour hash today is already `""`).
  - `ContributorSignature` gains `calibration_recipe: str = ""`.
  - **`to_dict()`/`from_dict()` must be edited explicitly, or the field
    never persists (round 6, major).** `ContributorSignature` and
    `RunSignature` (`run_signature.py:142-149,153-161`) use **hand-written**
    `to_dict()`/`from_dict()` — not `dataclasses.asdict()` — each
    enumerating every field by name. Adding a dataclass field does
    **not** automatically make it appear in either method:
    - `to_dict()` gains one line: `"calibration_recipe":
      self.calibration_recipe,`.
    - `from_dict()` gains one line: `calibration_recipe=d.get(
      "calibration_recipe", ""),`.
    - Without this, the exact failure this whole mechanism exists to
      prevent recurs one level up: Step 3b's non-empty recipe would be
      correctly *detected* as a change on the run right after it ships
      (in-memory `contributor_stale` comparison still works), but
      `to_dict()` would silently drop it from the saved
      `run_signature.json`, so the **next** run's `from_dict()` loads it
      back as `""` regardless of what was just computed and saved — the
      freshly-computed value then permanently mismatches the persisted
      `""` on every subsequent run, forcing a full real-Siril T21
      Luminance rebuild **every time**, not the single one-time rebuild
      Step 3b explicitly promises. This is silent: nothing raises, and
      no test that only constructs `ContributorSignature` objects
      in-memory (rather than round-tripping through JSON) would catch
      it.
  - **`RunSignature.contributor_stale` itself must change, not just gain a
    passthrough parameter (R4-1)** — this is the exact failure mode this
    file's own `flat_frame_hash` docstring already warns about ("the one
    place a keyword-default is NOT enough on its own"), and the plan must
    not silently reintroduce it for a second field:
    - `contributor_stale(self, section, key, frame_hash, pedestal,
      flat_frame_hash="", calibration_recipe="")` gains the new keyword
      parameter.
    - Its `return` line changes from
      `return existing.frame_hash != frame_hash or existing.flat_frame_hash
      != flat_frame_hash` to
      `return (existing.frame_hash != frame_hash or existing.flat_frame_hash
      != flat_frame_hash or existing.calibration_recipe != calibration_recipe)`.
    - There are **three** real `contributor_stale` call sites in
      `lrgb_orchestrator.py`, not two (verified: `grep -n
      "contributor_stale(" src/astro_pipeline/lrgb_orchestrator.py` gives
      lines 363, 543 and 618). The Luminance loop (`:361-365`) and the
      mono-RGB colour loop (`:541-545`) both pass the freshly-computed
      `calibration_recipe` value explicitly, exactly as they already do
      for `flat_frame_hash` — a defaulted parameter alone would compile
      and pass every existing test while never actually invalidating
      anything on a real recipe bump.
    - **The third call site — the OSC colour loop (`:618`) — is left at
      `contributor_stale`'s default `calibration_recipe=""`, deliberately,
      not by omission.** That line already hardcodes a literal `""` for
      `flat_frame_hash` too (`:614`, "OSC never consults flats"), because
      `build_group_master` for OSC always passes `flat_frames=[]`. Per
      this step's own `calibration_recipe_parts()` spec,
      `calibration_recipe_parts(mode, flats=[])` returns `("", "")`
      **regardless of mode**, and separately every real OSC contributor
      today is PRECALIBRATED, which also forces `("", "")` — so the
      freshly-computed value, if it were computed here, would still be
      `""`, matching the untouched default. This is **verified inert
      today, for the same reason as G10/R4-4**: RAW_LOCAL OSC is
      structurally unreachable while `build_osc`'s `cal_index={}`
      `KeyError` stands (G7/I1, deferred to §5). Unlike G10/R4-4, this
      inertness must be stated explicitly here rather than left to be
      rediscovered, because it is a real gap in this step's own
      "both/all real call sites are wired" claim, not merely a downstream
      consequence of it.
    - **One-line note for whoever picks up §5's OSC work later**: lifting
      the `build_osc` `KeyError` and making RAW_LOCAL OSC reachable is not
      enough on its own to make OSC flats resumability-safe — line 618
      must also be updated to pass a real, freshly-computed
      `calibration_recipe` (and `flat_frame_hash`) once OSC flats are
      matched and non-empty, or a future flat-recipe bump (for example
      `-cfa`/`-equalize_cfa`, itself listed as future OSC work in §5) will
      silently fail to invalidate an OSC contributor's master — exactly
      the failure class this mechanism exists to prevent. Added to §5's
      OSC bullet as well, not just here.
  - The helper replaces the mode-blind call at
    `lrgb_orchestrator.py:527` (fixing G10) and the Luminance loop's
    existing mode-aware call (`:337-348`), so both go through one place
    and both feed `contributor_stale`'s new parameter.
  - The field is named `calibration_recipe`, not `flat_recipe` (R3-6): its
    job is to let **any** future calibration-code behaviour change
    (flat staging/normalisation, bias/dark normalisation, an OSC
    calibrate-command shape change) bump one versioned string without
    adding a new schema field each time. A value looks like
    `"flat:v2:dedup+uniq+mul"` today (once Step 3b lands) and could later
    gain segments, e.g. `"flat:v2:dedup+uniq+mul|bias:v1:nonorm"`, bumped
    by whichever step changes that behaviour.
  - `RECIPE_VERSION`-style constants live in `calibration.py`, next to the
    functions whose behaviour they describe, so a constant changes in the
    same commit as the code it describes.
- **Tests**:
  - Mocked: PRECALIBRATED with a non-empty flat set → `("", "")`.
  - Mocked: RAW_LOCAL with an empty flat set → `("", "")`.
  - Mocked: RAW_LOCAL with a non-empty flat set → `(hash, "")` (today's
    unversioned baseline).
  - **A direct-call test mirroring
    `test_contributor_stale_true_on_flat_frame_hash_change_direct_call`
    (`tests/test_run_signature.py:215-229`)**: same `frame_hash`, same
    `flat_frame_hash`, only `calibration_recipe` differs — asserted
    `True`. And the reverse — everything unchanged including
    `calibration_recipe` — asserted `False`.
  - A defaulting test mirroring
    `test_contributor_stale_defaults_flat_frame_hash_to_empty_string`, for
    `calibration_recipe`.
  - Call-site tests: both the Luminance and colour loops pass through the
    helper **and** pass the resulting recipe string into
    `contributor_stale`'s new parameter explicitly (not left at the
    default) — the same "actually wired at the call site, not just present
    in the function signature" test this codebase already has for
    `flat_frame_hash`.
  - **Round-trip test (round 6, major) — the one this step's serialization
    change actually needs**: construct a `ContributorSignature` with a
    **non-empty** `calibration_recipe` (e.g. `"flat:v2:dedup+uniq+mul"`),
    call `to_dict()`, serialise through `json.dumps`/`json.loads` (or call
    `to_dict()`/`from_dict()` directly — either way the value must survive
    a dict round-trip, not just live in the in-memory object), call
    `from_dict()`, and assert the reconstructed object's
    `calibration_recipe` equals the original non-empty string. This is
    the missing half of the existing "old JSON (no `calibration_recipe`
    field) loads and reads back `''`" test (Step 3b) — that one only
    exercises the missing-key-defaults-to-`""` path and would not fail if
    `to_dict`/`from_dict` were never updated to include the field at all.
  - Real (gate D): every persisted real signature's hash/recipe pair is
    unchanged by this step.

### Step 3: correct the master flat (G1, G2). 3a always; 3b **decided: ships** (Q1 = fix now).

**3a (report only, never raises)**:
- Before staging, `run_calibration` classifies the matched flats by
  content identity: a streamed file SHA-256, cheap next to the copy
  `stage_frames` already does.
- It notes "`N` matched; `K` byte-identical copies; `C` name collisions
  between distinct frames; staged `S`".
- **Tests**: mocked, with two byte-identical folders plus two colliding
  distinct frames. Real (gate D/P): T21 L gives `N=30, K=0, C=10, S=20`.

**I6 no-code measurement (scheduled here so Q7 has numbers before E)**:
- Using the same `tmp_path` harness as gate (P): build T21's bias and dark
  masters with and without `-nonorm`, and the calibrated T24 L sub with
  each. Report the pixel difference and whether it is inside or outside
  the Step-1b determinism bound. Record the answer here and in Q7; no
  production code changes as a result of this measurement alone.

**3b (behaviour change, one commit — decided: ships, Q1 = fix now)**:
- **Staging**:
  - Flat-only content-aware staging:
    - drop byte-identical copies, keeping the first by sorted path;
    - rename surviving collisions to `<sanitised parent>_<8-hex path
      hash>__<name>`.
  - The sanitiser strips spaces and punctuation. The path hash
    disambiguates same-named parents.
  - Lights, bias and dark staging stay byte-unchanged; I5 is not
    touched.
- **Stacking**: the flat stack gets `-norm=mul`. The bias/dark stacks are
  unchanged (I6 is separate and gated on Q7, not this step).
- **Recipe bump**: `calibration_recipe_parts()` (Step 2) now returns
  `(hash, "flat:v2:dedup+uniq+mul")` for a non-empty RAW_LOCAL flat set —
  the **first** non-empty recipe string this mechanism ever produces.
  This is the **single** trigger for the T21_bin1 rebuild (with the
  `contributor_stale` wiring from Step 2 actually acting on it); T24 and
  every flat-less or PRECALIBRATED contributor stays at `("", "")` from
  this step alone (the deletion is what changes T24, independently —
  §1.5).
- **Narrowband/boost inertness (R4-4)**: `build_master_flat`/
  `stage_frames` are the same functions regardless of caller —
  `run_narrowband`/boost reach them exactly as `run_lrgb` does
  (`narrowband_orchestrator.py:118,131-134`; `master_builder.py:366-372`
  for boost). This step's `-norm=mul`/dedupe-staging change is therefore
  reachable from narrowband too, not just LRGB/colour. It is **verified
  inert today** for the same reason as G10's colour-hash fix: no real
  narrowband project (M42/T20's Ha/OIII/SII flats) has a
  filename-recognised RAW_LOCAL flat set yet — that needs Step 4b **and**
  an explicit `--calibration-mode` override (Step 5), neither of which
  changes any real narrowband project's mode by default. The day a real
  narrowband RAW_LOCAL flat set *is* recognised this way, this step's new
  behaviour applies to it silently, with **no** invalidation mechanism
  narrowband-side to force a rebuild of an already-built narrowband
  master — narrowband has no staleness tracking at all (§1.2), which is
  the already-deferred G11 gap (§5), just named here as a second, concrete
  way it bites once Step 3b/4b/5 are all live together.
- **Tests**:
  - Dedupe and uniq staging: 30 distinct → 30 staged; 2 identical → 1.
  - Command strings: flat `-norm=mul`; bias/dark byte-identical.
  - `calibration_recipe` returns the bumped value only for non-empty
    RAW_LOCAL flats; PRECALIBRATED and empty sets stay `("", "")`.
  - An old JSON (no `calibration_recipe` field) loads and reads back `""`.
- **Verification**: folded into event E (§4.0), including the
  `luminance_selected`/`colour_reference`/FWHM check and the free T24
  pixel-identity check. Then re-measure §2.3 on the 30-frame `-norm=mul`
  master, with corner/centre for both the flat and the stacked L, flat vs
  no-flat.
- **Decided (§6, Q1): fix now.** 3b's code ships unconditionally; M51's
  rebuild happens at E, which the deletion forces anyway — so this is a
  single regeneration, not two.

### Step 4a: pure `classify_tree` extraction (behaviour-preserving refactor)

Split out of the old single Step 4 (R3-9): this half is a pure refactor
with an equality gate, so it is reviewable independent of the fallback
logic in 4b.

- **Change**: extract a pure, zip-aware classifier
  `classify_tree(root) -> IngestReport` from `scan_session`.
  - It peeks lights inside zips by name, as `index._scan_zip` already
    does, and does **not** extract.
  - `scan_session` becomes: call `classify_tree`, then **replace** (not
    append) each zip-peeked light with its extracted-path equivalent once
    extraction has happened — appending both would recreate exactly the
    double-count class of bug documented in §1.5. `_extract_zipped_lights`
    already returns the extracted `LightFrame`s; `scan_session` drops the
    zip-peeked placeholders for names it just extracted before adding
    those.
  - `index.build_index` is left as-is (test-only today; noted, not
    changed).
- **Tests**:
  - Gate (D), flag-off, on all seven projects: `classify_tree` plus
    `scan_session`'s own zip-extraction step gives byte-identical
    `IngestReport` contents (including list order) to today's
    `scan_session` output, for every project including M51 (with its
    zips).
  - A dedicated mocked test for the replace-not-append rule: a zip whose
    peeked light and extracted light must not both end up in
    `report.lights`.

### Step 4b: opt-in header-based calibration recognition, without a mode flip (G4, G5)

- **Change**: `classify_tree(root, *, calibration_header_fallback=False)`.
- **Fallback rules**:
  - **When it runs**: for files that no filename pattern matched, and
    for skyflat/camera names returned early at `ingest.py:505-513`. The
    latter need a **new** header read.
  - **`IMAGETYP`** normalises to Bias/Dark/Flat. Lights are never
    classified this way.
  - **Rejected**: `CALSTAT` containing `M` (masters) and unreadable or
    truncated headers.
  - **Binning**: `XBINNING`, which must equal `YBINNING`.
  - **Bias exptime**: forced to `0.0` whatever the header says (the
    `master_builder.py:176` key is exact).
  - **Dark exptime**: header `EXPTIME` rounded to 0.1 s. Mocked tests
    for 239.999 → 240.0 and a non-zero-exptime bias.
  - **Flat filter**: from `FILTER` (required).
  - **Filename vs header**: the filename wins. A disagreement leaves the
    frame unrecognised.
  - **Shadowing rule (R3-7)**: header-sourced frames of a given
    `(telescope, frame_type)` are only used at all when that telescope has
    **zero** filename-recognised frames of that type. Otherwise they are
    listed as unrecognised with the reason "shadowed by
    filename-recognised frames of this type", never merged into the same
    index entry. This keeps a filename-RAW_LOCAL telescope's real masters
    from silently absorbing an unrelated header-only frame the moment the
    flag is on.
- **Telescope attribution**:
  - (a) A strict `^T\d+$` folder.
  - (b) The nearest ancestor directory name, up to and including the
    root, containing `(?<![A-Za-z0-9])T(\d{1,3})(?![A-Za-z0-9])`,
    normalised to `T%02d` (M31's real bias filenames say `T5`, not `T05`
    — this normalisation is load-bearing there, §2.1). If that name
    contains more than one distinct token (M51's root `… T24 & T21 …`),
    the frame is unrecognised.
  - (c) The root's raw lights (bare **or** zip-peeked) come from exactly
    one telescope.
  - Then cross-check `INSTRUME` + `NAXIS1/2` against at least one
    readable **bare raw** light of that telescope at that binning.
    Otherwise the frame is unrecognised with the reason "no readable raw
    light to cross-check".
- **Mode decoupling (G5; default proposal, Q2)**:
  - `CalibrationFrame` gains `source: str = "filename"`. Fallback frames
    get `"header"`.
  - `infer_calibration_mode` counts only `source == "filename"` bias and
    darks, until a future decision (§5) lifts that. Reads `source` via
    `getattr(f, "source", "filename")` so the ~20 existing test fakes that
    return plain strings, not `CalibrationFrame` objects (§1.1), keep
    working unchanged (R3-8).
  - Fallback frames still populate `calibration_index()`/`flat_index()`
    (subject to the shadowing rule above), so they are visible in the
    interview and **usable under an explicit `calibration_mode`
    override** (added in Step 5).
  - Consequence: with the flag on, T20, T68 and T05 **stay
    PRECALIBRATED** by default.
  - With the flag on **and** an explicit override to RAW_LOCAL:
    - T68 still raises `KeyError` in `build_osc` (I1; §5);
    - T05 uses the −15 °C darks (G15; documented, not fixed).
  - **Scope of the "no persisted value changes" claim**: this holds for
    the seven gate-(D) projects checked here. A **different** real
    project — a filename-RAW_LOCAL telescope that also happens to have
    header-only flats for a filter it didn't have filename-recognised
    flats for — would see `infer_flat_policy` flip SKIP→REQUIRE and its
    flat hash change. None of today's seven projects hits this (verified
    per-project); it is called out, not silently assumed away.
- **Tests**:
  - Mocked: every rule, including the M51-style multi-token root, the
    exptime rules, `source`, the `getattr` fallback against a plain-string
    fake, the shadowing rule, and "fallback frames don't flip the mode".
  - Real, gate (D) flag-on:
    - counts per §2.1: M42 T20 bias 50/49, darks 10 + 20, flats 10 × 6 +
      9; IC 1396 bias 48, darks 50, flats 88; M31 15/16/40 × 3;
    - `infer_calibration_mode` unchanged for **every** telescope in all
      seven projects;
    - M51 and NGC 3628 indexes unchanged.
- **Behaviour change**: none by default, and none in mode even with the
  flag on.
- **Gates**: D, P.

### Step 5: thread the flag; add an explicit override CLI flag; record both

- **Change**:
  - Add a `calibration_header_fallback: bool = False` parameter to
    `run_lrgb`/`LRGBOrchestrator`, `run_narrowband` and
    `skill/run_narrowband_boost.py`.
  - Add it to `skill/interview.py` **`main` only**; `render` already
    takes a report.
  - Each passes it to its scan (`lrgb_orchestrator.py:263`,
    `narrowband_orchestrator.py:114`, `run_narrowband_boost.py:90`,
    `interview.py:215`).
  - **The CLI override shape must match each entry point's actual
    parameter type (R4-3) — it is not one shape for all three:**
    - `run_stage.py` gets `--calibration-mode TEL=raw_local|precalibrated`
      (repeatable, one `TEL=value` pair per occurrence), parsed into the
      `dict[str, CalibrationMode]` that `run_lrgb`/`LRGBOrchestrator`
      already accept (`lrgb_orchestrator.py:209`, `:1029`) — a genuine
      multi-telescope override, since one LRGB run can have a different
      Luminance telescope and colour telescope.
    - `run_narrowband.py` gets a **plain**
      `--calibration-mode raw_local|precalibrated`, with **no** `TEL=`
      prefix, mapped directly to `run_narrowband`'s single
      `calibration_mode: CalibrationMode | None` parameter
      (`narrowband_orchestrator.py:51`) — that entry point already pins
      exactly one `--telescope` (`skill/run_narrowband.py:28`), so a
      per-telescope dict has nothing to key on; giving it the same
      `TEL=value` syntax as `run_stage.py` would be undefined the moment
      the given `TEL=` didn't match `--telescope`, or none was given.
    - `run_narrowband_boost.py`: `build_single_filter_master`, which it
      calls, also takes a single `calibration_mode: CalibrationMode |
      None` (`master_builder.py:328`), but the script's own `main()` does
      not pass that parameter **at all** today
      (`skill/run_narrowband_boost.py:93-96`) — it always resolves via
      `infer_calibration_mode` inside `build_single_filter_master`
      (`:340`). This step adds `--calibration-mode
      raw_local|precalibrated` there too, threaded into the existing call,
      the same shape as `run_narrowband.py`'s. (`run_narrowband_boost.py`
      still gains only `calibration_header_fallback`, not a separate flag
      shape decision — mirroring `run_narrowband.py` costs nothing extra
      once the plain single-value flag exists.)
  - Without any of this, recognised local flats on T20, T68 or T05 stay
    reachable only from Python, not from any command — closing this gap
    is what makes the core plan's recognition work (Step 4) actually
    exercisable end to end, not just visible in the interview.
  - `RunSignature` gains a **top-level** diagnostic
    `calibration_header_fallback: bool = False`. It is deliberately
    **not** placed on `ContributorSignature`, where dict equality would
    cascade (R2-12).
  - `diff_invalidation` ignores it explicitly.
  - **Same `to_dict()`/`from_dict()` gap as Step 2, lower severity (round
    6, major)**: `RunSignature.to_dict()`/`from_dict()`
    (`run_signature.py:194-201,206-214`) are the same hand-written,
    field-by-field kind as `ContributorSignature`'s. `to_dict()` gains
    `"calibration_header_fallback": self.calibration_header_fallback,`;
    `from_dict()` gains `calibration_header_fallback=d.get(
    "calibration_header_fallback", False),`. Lower severity than Step
    2's gap because this field is diagnostic-only and
    `diff_invalidation` already ignores it, so an unfixed omission here
    doesn't break invalidation the way Step 2's would — it just means the
    field silently never actually gets recorded in `run_signature.json`
    despite existing to do exactly that.
- **Tests**:
  - Call-site tests per entry point.
  - **Round-trip test, mirroring Step 2's**: a `RunSignature` with
    `calibration_header_fallback=True`, through `to_dict()`/`from_dict()`,
    must read back `True` — not just "old JSON without the key defaults
    to `False`" (which the existing "Old JSON loads" test bullet below
    only covers in the other direction).
  - `run_stage.py`: `--calibration-mode T20=raw_local` (repeated for a
    second telescope) parses into the `dict[str, CalibrationMode]`
    `run_lrgb` expects.
  - `run_narrowband.py`: a **separate** parser test —
    `--calibration-mode raw_local` parses into the single
    `CalibrationMode.RAW_LOCAL` value `run_narrowband` expects, with no
    `TEL=` involved. (R4-3: the round-4-rev plan's own test list
    previously covered only the `run_stage.py` shape; this is the missing
    coverage.)
  - `run_narrowband_boost.py`: the same plain-value parser test, and a
    call-site test that `build_single_filter_master` actually receives
    the parsed value instead of always falling through to
    `infer_calibration_mode`'s default.
  - Old JSON loads.
  - Toggling only the flag gives `diff_invalidation(old, new) == set()`.
- **Hash contract**: with Step 4b's mode decoupling and Step 2's
  mode-aware helper, toggling the flag changes no persisted hash or
  recipe on any of the **seven gate-(D) projects** (scope stated
  explicitly per R3-7 — not "any project" universally).
- **Gates**: D, P; (I) only after E.

### Step 6: interview and run visibility (G6)

- **6a**:
  - (i) An "Unrecognized frames" section grouped by reason and
    `IMAGETYP`.
  - (ii) Consequence annotations for the existing flat-gap lines, in a
    separate section; `_FLAT_RE` wording is untouched.
  - (iii) `flat: N frames (K copies, C collisions)` — using a **cheap
    header-identity** signal (`DATE-OBS`, `EXPTIME`, file size), **not**
    SHA-256 (R3-10): the interview makes no copy of the flats the way
    `run_calibration` does, so re-hashing T21's 330 flats (or T68's 88
    large frames) on every interview run would be needlessly expensive.
    The SHA-256-based mechanism stays where it already is, in Step 3a's
    `run_calibration` path, which copies the files anyway.
- **6b**: a `master_builder` note, "`<group>`: no flat applied
  (policy=…)".
- **Tests**:
  - Mocked; update any exact-`notes` assertions.
  - Real: IC 1396 and M51 interview output.
- **Behaviour change**: presentation and `notes` text only.

### Step 7: flat sanity notes (G9), corrected measurement basis

- **Change** (warning-only, on a **sample of raw flat frames**, not the
  Siril-normalised master — the master's data is a 32-bit float in
  `[0, 1]`, verified directly (§2.3), so any "fraction of 65535" check
  applied to it is nonsensical and would fire on every real master
  including T21's correctly-behaved one):
  - **Level**: median ADU of a sample of ≤ 3 raw flats (first, middle,
    last of the matched set) after `BZERO`, as a fraction of 65535; warn
    outside 10–85% (floor lowered from an initial 15%, since T21's own
    real flats sit at about 35% but a naive spread-based reading of
    §1.3.6 could be misread as lower — stated explicitly as a heuristic,
    not sourced from a spec).
  - **Saturation**: fraction ≥ 0.95 × 65535, same ≤ 3-frame sample.
  - **Exposure**: `EXPTIME` min/max, and within-set spread per filter.
  - **Age**: flat date span and gap to the lights; `DATE-OBS=1970` →
    "unknown".
  - Thresholds are **heuristics**, marked as such in code and docs.
  - No CMOS-specific exposure floor: no header reliably identifies CMOS
    (`INSTRUME='ASI Camera (1)'` is not a floor's worth of signal).
- **Tests**: mocked, with synthetic raw-frame arrays; real notes for T21.
- The dark-current/dark-flat-necessity measurement originally scoped here
  is **moved to Future work** (§5): it is informational only, has no
  production-code consequence in this plan, and the M42 Green-vs-Red flat
  exposure anomaly it would have explained is simply recorded in §2.1.

### Step 8: documentation and CLI (G12, G13)

- **SKILL.md**:
  - `:17` "no flats" scope line.
  - `:10` `pipeline.py`.
  - `:160-164` "IC 1396, RAW_LOCAL … NO local dark": false today (T68 is
    PRECALIBRATED; the bias is unrecognised; RAW_LOCAL OSC `KeyError`).
  - A new "Flats" section: discovery, names and folders, fallback flag,
    `--calibration-mode` override, REQUIRE/SKIP, collision reporting.
- **ROADMAP**: 2.2 (`flat_index()`, v3 absent, 20-frame finding, Q1
  outcome, E's recorded hashes), 4.1, 4.3; and **§5, the T24 `CALSTAT=BDF`
  note decided at Q8** — jmwill's `calibrated-` T24 BIN1 copies were
  already flat-corrected server-side by iTelescope, documented as a fact
  about the existing delivery, not a reopening of the binding "no T24
  flats" decision.
- **Code/test docstrings claiming T68 has no darks or is RAW_LOCAL**:
  - `tests/conftest.py:145-152`;
  - `tests/test_calibration.py:397`;
  - `calibration.py:425,635,775`;
  - `colour_contributor.py:355`;
  - `master_builder.py:188-199`;
  - `lrgb_orchestrator.py:331-336` (the T73 claim).
- **`calibration-frame-counts.md`**: add T20/T05/T68 rows.
- **CLI**: `--flat-policy` and `--calibration-header-fallback` on
  `run_stage.py`, `run_narrowband.py`, `run_narrowband_boost.py` and
  `interview.py` (`--calibration-mode` already added in Step 5). Parser
  tests.
- **State the cuts plainly, per the decisions in §6**: SKILL.md's "Flats"
  section says explicitly that OSC flats (Q5, deferred) and narrowband
  master invalidation on flat change (Q6, staying on `--force` only) are
  not shipped here, and that no behaviour change to M42/IC 1396/M31 has
  happened in this plan (Q9) — the per-target measurement is the decided
  next step, and reprocessing itself happens only where a target's own
  measurement later justifies it, never blind — so a reader does not
  assume more happened than did.

### Deliberately out of scope (not deferred to §5 — genuinely not planned)

- `Master_Flat*`/`Master_Bias*` use.
- BIN1→BIN2 flat binning.
- Cross-telescope RGB.
- T24 flats (binding).
- G15 temperature check.
- I4 (existing over-invalidation behaviour).

---

## 5. Future work (deferred out of the core plan)

Per round-3 review, these are real gaps but not load-bearing for "figure
out what's missing and build it" against the code that exists today.
Kaveh has now decided the disposition of every item below (§6) — most
stay deferred and **not committed to**; one (the per-target measurement)
is decided as the genuine **next step** after Steps 0–8 ship, not an
indefinitely-skippable option. Each remaining item would still be its
own small plan, informed by what Steps 0–8 above actually ship.

- **OSC flats (old G7/I1/I2)**: `-cfa`/`-equalize_cfa` on the calibrate
  command, the `build_osc` `cal_index={}` fix, and OSC flat lookup. Real
  and needed for IC 1396, but unreachable under the default "Keep" mode
  policy without an explicit override, and Step 4b/5 already let an
  override reach the `KeyError` cleanly enough to know it's there.
  **Decided (§6, Q5): deferred, not committed to.** Full OSC
  integration is not being built now; it is only worth revisiting if
  IC 1396's own eventual per-target measurement (below) shows a real
  benefit — and that measurement is itself lower-priority than M42's or
  M31's, given IC 1396's corrupted 1970 calibration timestamps (§2.1)
  making even the measurement harder to trust. **Also required at that
  point (F1, round 5)**: `lrgb_orchestrator.py:618`'s OSC colour-loop call
  to `contributor_stale` still passes a hardcoded literal `""` for
  `flat_frame_hash`, never routed through Step 2's
  `calibration_recipe_parts()` helper — inert today only because OSC's
  flat set is always empty and every real OSC contributor is
  PRECALIBRATED. Whoever lifts the `KeyError` here must also wire a real
  `calibration_recipe` (and `flat_frame_hash`) into that call site, or an
  OSC flat/recipe change (including this bullet's own `-cfa`/
  `-equalize_cfa` work) will silently fail to invalidate an OSC
  contributor's master.
- **Flat↔light geometry/`INSTRUME` compatibility check (old G8)**: a
  `FlatMismatchError` inside `run_calibration`. No real project today
  co-scans mismatched flats and lights in one `scan_session` call (T68's
  2021 flats and 2023 lights live in different project folders), so there
  is nothing in the current data to exercise this defensively. Worth
  adding once a real mismatch is possible, not before.
- **Narrowband/boost master invalidation (old G11)**: `--force` should
  delete the per-filter group masters, or narrowband should get real
  signature tracking. Real, but not flat-specific — it is a pre-existing
  resumability gap independent of anything in this plan. **Decided (§6,
  Q6): leave narrowband on `--force` only, for now** — no signature
  tracking is being built.
- **Dark-current / dark-flat-necessity measurement (old Step 7 half)**:
  estimate e⁻/s from the T20/T68 darks and compare against flat signal at
  the longest flat exposures, to decide whether dark-flats are ever
  needed. Informational; no production code depends on the answer today.
- **Per-target local-flats-vs-iTelescope measurement, and the Keep/Lift
  mode-policy decision it might inform per target (old Steps 10–11).
  Decided (§6, Q3/Q9): do this — it is the explicit next step after Steps
  0–8 ship**, not a bundled step in this plan's own list and not something
  to skip indefinitely. Scope, per Kaveh's decision:
  - **Per target, not a single M42-only pass**: M42/T20, IC 1396/T68 and
    M31/T05 each get their own measurement, using the same
    corner/centre-ratio (or half-split) method T21's own §2.3 measurement
    already used, not a single shared verdict across all three.
  - **Mechanics** (as scoped by round-3 review, still valid): build
    RAW_LOCAL masters with and without a flat, and PRECALIBRATED masters,
    and compare masked background uniformity and dust-residual metrics.
    Round-3 flagged the originally-drafted version as expensive (≈360
    register+stack runs across 20 half-splits × 3 arms × 3 masters) and
    confounded on background alone (darks, cosmetic correction and
    pedestal differ between RAW_LOCAL and PRECALIBRATED, not just the
    flat) — when this is actually executed:
    - build each arm's calibrated master **once**, then split only
      register+stack for the noise floor (cuts the cost roughly in half);
    - a half-stack's noise is about √2 worse than the full stack being
      compared — account for that factor or use it only as a relative
      comparison across arms, not an absolute bound;
    - isolate the flat's effect specifically via RAW_LOCAL-no-flat vs
      RAW_LOCAL-with-flat (same darks), and report RAW_LOCAL vs
      PRECALIBRATED separately, without auto-deciding from it;
    - it needs only Steps 4b (recognition) and the Step-2 mechanism; it
      does not need Steps 3b/6/7 or the CLI flags.
  - **Decision rule per target (Q9): measure first, reprocess only where
    the measurement shows a genuine, quantified improvement.** Never
    commit to reprocessing all three blind the way M51/T21 was fixed on
    direct, already-measured evidence:
    - **M42/T20**: real risk the measurement itself argues *against*
      reprocessing before even running it — its flat library is 21
      months older than its light sessions (§2.1), a real dust/
      vignetting-drift risk that could make a "corrected" result worse,
      not better, than iTelescope's own (presumably fresher) server-side
      flats.
    - **IC 1396/T68**: lowest priority. Its calibration frames carry
      corrupted `DATE-OBS=1970` timestamps (§2.1, camera clock unset) and
      there is no RAW_LOCAL OSC code path at all yet (Q5, deferred
      above) — measuring here needs both a data-quality caveat and OSC
      work that isn't committed to.
    - **M31/T05**: the **most promising** candidate — its flats are only
      19 days old (§2.1), the freshest of the three — but its darks are
      a real, unfixed mismatch (G15: −15 °C darks vs −10 °C lights), which
      could itself introduce artefacts into the very measurement meant to
      justify reprocessing. **Fix G15 first**, before or as part of
      measuring M31, or the measurement's own result is unreliable.

---

## 6. Decisions (Kaveh, 2026-09-23)

Every item below was an open question through rev 8 (converged after
round 8's review, verdict CONVERGED, no new findings). Kaveh has now
answered all of them. This section is a **decisions record**, not a
question list — the scope/sequencing text elsewhere in this plan (TL;DR,
§4, §5) has been updated to match, and none of these are being
relitigated by a future review round.

1. **The T21 master flat**: 20 of 30 frames, default normalisation (G1,
   G2).

   **Decided: fix now.** Step 3b's `calibration_recipe` bump ships
   unconditionally. M51 regenerates once, at E, batched with the deletion
   that regenerates it anyway — so this is the single-regeneration
   outcome the plan was already structured around (option (a) of the
   three originally offered), not a compromise.
2. **Header-recognised bias/darks and the calibration mode**: must they
   **not** flip `infer_calibration_mode` (Step 4b's default, "Keep")?

   **Decided: Keep, permanently as the default** — not just for this
   round. Recognised header-sourced bias/darks do **not** auto-flip the
   calibration mode for M42/T20, IC 1396/T68 or M31/T05; PRECALIBRATED
   stays the default everywhere it is today. "Lift" (auto-flipping the
   mode) is not being built; the option to override per telescope via
   `--calibration-mode` (Step 5, Q11) remains available, but nothing
   flips automatically.

   (The originally-proposed third option, "always prefer iTelescope's
   calibrated lights over local bias/dark," was **dropped** during
   review, before this decision: mode is resolved per telescope with no
   binning dimension at all, so it would have flipped **all** of T24 —
   Luminance from 21 raw subs to 9 calibrated, and the BIN2 colour
   contributor would disappear entirely, since it has no calibrated
   copies. That is a real behaviour change to M51's delivered image and
   conflicts with the binding "no T24 flats" decision, so it was removed
   rather than fixed to exclude M51 by name, which would itself
   contradict this codebase's own "never hardcode by telescope name"
   principle. It was never a live option by the time Q2 was decided.)
3. **The Future-work measurement (§5)**: done at all, and if so, now or
   later?

   **Decided: do it — reframed as an explicit NEXT step after Steps 0–8
   ship**, not bundled into this plan's own step list and not something
   to skip indefinitely. Per target (M42/T20, IC 1396/T68, M31/T05 each
   get their own measurement), using the same corner/centre-ratio or
   half-split method T21's own §2.3 measurement already used. See §5's
   measurement bullet and Q9 below for the per-target risk reasoning and
   decision rule.
4. **Migration if the mode ever flips**: a documented one-time `force`,
   or a master-provenance check?

   **Decided: not applicable while Q2 = Keep.** The mode-flip migration
   mechanism is deferred entirely — there is nothing to migrate if the
   mode never auto-flips. Revisit only if a specific target's own
   measurement (Q3) leads to a per-target "Lift" decision later; this is
   not being designed speculatively ahead of that.
5. **OSC (IC 1396)**: worth doing at all, and if so on what timeline?

   **Decided: defer.** Full OSC integration (the `build_osc` `KeyError`
   fix, `-cfa`/`-equalize_cfa`, OSC flat lookup) is not committed to now.
   Revisit only if IC 1396's eventual measurement (Q3) — itself
   lower-priority given its corrupted 1970 calibration timestamps —
   shows a real, quantified benefit. Not a "do this next" item the way
   Q1/Q3 are.
6. **Narrowband staleness**: `--force` deletes the shared masters, or
   real signature tracking?

   **Decided: leave narrowband on `--force` (no signature tracking), for
   now.** No new resumability mechanism is being built for narrowband.
7. **I6** (bias/dark `-nonorm`): the no-code measurement in Step 3 will
   report numbers before E runs. Given those numbers, change it (batched
   into E) or leave it?

   **Decided: conditional, not a blind yes/no.** Fix `-nonorm` on
   bias/dark masters **only if** Step 3's measurement shows a meaningful
   effect; otherwise leave it as-is. The measurement happens regardless
   (it is already scheduled, no-code, before E); what changes is that
   its result is now the deciding factor by design, not merely
   informative.
8. **T24 (binding, not relitigated)**: note in ROADMAP §5 that jmwill's
   `calibrated-` T24 BIN1 copies are `CALSTAT=BDF`, or leave it?

   **Decided: yes.** Add the ROADMAP note. This does not reopen the
   binding "no T24 flats" decision itself — it is documentation of a
   fact about the existing delivery, not a scope change.
9. **Scope**: "honest, correct, and safely able to recognise new
   calibration data without changing today's telescopes" (Steps 0–8), or
   also "actually reprocess M42/IC 1396/M31 with local flats" (§5)?

   **Decided (resolved after a follow-up conversation — this supersedes
   the plan's original either/or framing above):**
   - **Ship Steps 0–8 now** as the core, immediate deliverable: the T21
     fix, ingest recognition of calibration data at three other
     telescopes, Siril silent-failure detection, the `--calibration-mode`
     CLI override, and the signature/serialization fixes found in
     review. **No behaviour change to M42/IC 1396/M31 in this plan.**
   - **Separately, explicitly recommend AGAINST blind full reprocessing**
     of M42/IC 1396/M31. There is a real risk that stale or mismatched
     calibration data makes results **worse**, not better, than what
     iTelescope's own server-side calibration already produced:
     - M42's real flat library is 21 months older than its light
       sessions — real dust/vignetting-drift risk;
     - IC 1396 has corrupted 1970 calibration timestamps and no
       RAW_LOCAL OSC code path yet (Q5, deferred);
     - M31 has fresher flats (19 days old) but a real, unfixed
       dark-temperature mismatch (−15 °C darks vs −10 °C lights, G15)
       that could itself introduce artefacts.
   - **The decision rule**: measure each target first (Q3), reprocess
     only where that target's own measurement shows a genuine, quantified
     improvement. M31 is the most promising of the three, given its
     flat freshness — **contingent on first fixing G15's dark-temperature
     issue**, or the measurement meant to justify reprocessing M31 is
     itself unreliable. Never commit to reprocessing all three blind, the
     way M51/T21 was fixed on direct, already-measured evidence — that
     precedent does not transfer to targets whose calibration data hasn't
     been measured yet.
10. **The M51 deletion**: already decided, no change from earlier rounds.
    Confirmed sequencing — the folders are deleted immediately in Step 0;
    it is running a real M51 **pipeline** run (not the deletion) that
    waits for event E, so M51 regenerates exactly once.
11. **The `--calibration-mode` CLI flag** (Step 5): expose the existing
    Python-only override via the CLI, or is a Python-only override
    sufficient?

    **Decided: yes, keep it** — as specified in Step 5 (the three
    distinct CLI shapes for `run_stage.py`, `run_narrowband.py` and
    `run_narrowband_boost.py`).

---

## Review log

### Round 1 (`docs/plan-flats-v4-review-round1.md`): each finding re-verified before acceptance

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| F1 | crit | **Accepted.** Verified: 12 session folders; L 20+10; 10 duplicate names; staged 20; `nb_images=20`. | G1, failure mode 5, invariant "20 unique", Step 3a/3b, Q1. |
| F2 | major | **Accepted.** Verified: Siril help text; bundled `.ssf` use `-norm=mul`. | G2; in 3b. |
| F3 | major | **Accepted** (count corrected in round 2: 6 FLAT, 6 OFFSET, 5 DARK). | Step 1b uses a prefix regex and checks the flat calibrate too. |
| F4 | major | **Accepted.** Verified 4 `scan_session` call sites. | Step 5. The mode-flip consequence was fully addressed only in round 2 (R2-1). |
| F5 | major | **Accepted.** | Step 8a before 8b (now Future work). |
| F6 | major | **Accepted.** | Arms A/B/C; noise floor reworked in round 2, cost reworked in round 3 (now Future work). |
| F7 | major | **Accepted.** Verified early return at `ingest.py:505-513`. | Step 4b attribution and precedence. |
| F8 | major | **Accepted.** | Gate (D) projects and a mode-aware helper (Step 2). |
| F9 | major | **Partially accepted.** The "≤ 46 s" claim is sourced (M42 SII 43.2–46.0 s). | §2.1. |
| F10 | minor | **Accepted.** | I3 explained. |
| F11 | minor | **Accepted.** | Failure modes 1–2; Step 6a(ii) annotates. |
| F12 | minor | **Accepted.** | `FlatMismatchError(RuntimeError)`; now Future work per R3-14. |
| F13 | minor | **Accepted.** | Deferred to Future work along with the rest of OSC. |
| F14 | minor | **Accepted.** | Deferred to Future work. |
| F15 | minor | **Accepted.** | Step 0. |
| F16 | minor | **Accepted** (`master_builder.py:156`, not 157). | Cited correctly. |
| F17 | cosm | **Accepted.** | §2.1. |

### Round 2 (`docs/plan-flats-v4-review-round2.md`): each finding re-verified before acceptance

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| R2-1 | crit | **Accepted.** Verified against the `calibration_policy.py:66-73` rule. | G5. Step 4b adds `CalibrationFrame.source`; `infer_calibration_mode` ignores header-sourced frames. Further refined in round 3 (R3-1, R3-4, R3-7). |
| R2-2 | major | **Accepted.** Verified `test_pipeline.py:1386-1405` compares before/after a resume; no stored hash. | Gates (P/I/D). Further refined in round 3 (R3-2, R3-12). |
| R2-3 | major | **Accepted.** Verified duplicate folders exist; hash math. | §1.5. Further refined in round 3 (R3-2's T24 impact). |
| R2-4 | major | **Accepted.** | 3a content-hash collision reporting; 3b dedupe+uniquify. |
| R2-5 | major | **Accepted.** | Single helper; split into its own step in round 3 (R3-3). |
| R2-6 | major | **Accepted.** | Arms A/B/C; noise floor; further cost-reduced and moved to Future work in round 3 (R3-11). |
| R2-7 | major | **Accepted** (surface only). | I6; measurement scheduled explicitly in round 3 (R3-15). |
| R2-8 | major | **Accepted.** | Event E step 5 (was step 4). |
| R2-9 | minor | **Accepted.** | Pure `classify_tree`; split into 4a/4b in round 3 (R3-9). |
| R2-10 | minor | **Partially accepted; the sub-claim was rejected with evidence** (44 T68-2023 lights are parsed raw, not unparsed). | §2.1 row corrected; conclusion (only `NAXIS` discriminates) kept. |
| R2-11 | minor | **Accepted.** | Step 4b exptime rules. |
| R2-12 | minor | **Accepted.** | Step 5: top-level `RunSignature` field. |
| R2-13 | minor | **Accepted.** | Corrected counts. |
| R2-14 | minor | **Accepted.** | Step 8 doc list. |
| R2-15 | minor | **Accepted.** | Step split into 1a/1b. |
| R2-16 | minor | **Accepted.** | Step 6a pre-flight; run_calibration backstop deferred (R3-14). |
| R2-17 | minor | **Accepted.** | Step 7; further corrected in round 3 (R3-13). |
| R2-18 | minor | **Accepted.** | Q1(c) kept. |
| R2-19 | cosm | **Accepted.** | Wording fixes applied. |

### Round 3 (`docs/plan-flats-v4-review-round3.md`): each finding independently re-verified before acceptance

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| R3-1 | major | **Accepted.** Independently recounted the current (pre-deletion) M51 tree: T24 raw = 190, T21 raw = 4, T24 L bin1 = 42 — exactly matching the reviewer's figures and confirming the three pre-existing ingest tests are red today, before this plan changes anything. | New explicit real-data suite protocol in §4.0 (`m51_pipeline` marker, deselected until E); §1.5 and Step 0 now state the pre-existing-red-test fact plainly, as an independent finding, not something this plan fixes. |
| R3-2 | major | **Accepted.** Independently recomputed: T24 L bin1 hash on the current tree is `d2f430e6232fd176` (equals the persisted value); post-deletion it is `de55083822298fad` — both exactly matching the reviewer's numbers. | §1.5 rewritten with the verified table. Event E (§4.0) now includes a snapshot/backup step, records `STACKCNT`/`colour_reference` in addition to FWHM, and adds the "free" T24 pixel-identity check the byte-identical staged sets make possible. |
| R3-3 | major | **Accepted.** Verified Step 5's old "hash contract" language cited a helper that only existed inside the Q1-gated 3b. | New Step 2: the mode-aware `calibration_recipe` helper (fixing G10) now ships unconditionally, before and independent of Q1, verified inert on every real persisted signature today. Step 3b only adds the recipe-string bump. |
| R3-4 | major | **Accepted; the "Prefer iTelescope" option is dropped, not merely rescoped.** Verified `infer_calibration_mode` takes no binning argument (`calibration_policy.py:66-73`), so the option would flip all of T24 (Luminance 21→9 subs, BIN2 colour contributor gone entirely), not just BIN1 as originally written — and excluding M51 by telescope name would itself violate this codebase's stated "never hardcode by name" principle. | Removed from Step 11/Q2 entirely, with the evidence recorded in Q2 itself rather than silently deleted. Step 11 (old numbering) folded into Future work (§5) as a Keep-vs-Lift-only decision. |
| R3-5 | major | **Accepted.** Verified `run_lrgb`/`LRGBOrchestrator` already accept a `calibration_mode` dict override in Python (`lrgb_orchestrator.py:209,1029`), but no CLI script exposes it. | Added `--calibration-mode TEL=raw_local\|precalibrated` to Step 5, so the core plan's recognition work is actually reachable end to end, not just visible in the interview. |
| R3-6 | major | **Accepted.** Verified `ContributorSignature` has no field for bias/dark composition or the calibrate-command shape (`run_signature.py:118-140`), and I6/old-8a would both need one. | Field renamed `flat_recipe` → `calibration_recipe`, with a namespaced string format (`"flat:v2:…"`) so I6 and any future OSC work bump the same mechanism instead of adding new schema. |
| R3-7 | minor | **Accepted.** Verified `calibration_index()`/`select_dark()`/`build_group_master`'s bias lookup all merge header- and filename-sourced frames without regard to `source`, and that `missing_calibration_warnings`/`dark_scaling_notes` would treat header frames as present under an explicit override. | Step 4b's mode decoupling is now paired with an explicit shadowing rule (header frames of a type used only when no filename frames of that type exist for the telescope), with a mocked test. Step 5's "no persisted value changes" claim is scoped to the seven gate-(D) projects, not asserted universally. |
| R3-8 | minor | **Accepted.** Verified test fakes in `test_pipeline.py`/`test_run_signature.py`/`test_skill_interview.py` return plain strings from `calibration_index()`, not `CalibrationFrame` objects. | Step 4b reads `source` via `getattr(f, "source", "filename")`, with a mocked test against a plain-string fake. |
| R3-9 | minor | **Accepted.** Verified `scan_session` appends zip-extracted lights separately from bare-file classification, with no existing "peek then replace" logic to build on. | Step 4 split into 4a (pure `classify_tree` extraction with a full-equality gate against today's `scan_session` output) and 4b (fallback rules). 4a explicitly specifies replace-not-append for zip-peeked vs. extracted lights, with a dedicated mocked test. |
| R3-10 | minor | **Accepted.** | Step 6a(iii) now uses header identity (`DATE-OBS`/`EXPTIME`/size), not SHA-256, for the interview's own preview; the SHA-256 mechanism stays inside `run_calibration` (Step 3a), which already copies the files. |
| R3-11 | minor | **Accepted.** | The full statistical measurement (old Step 10) moved to Future work (§5) with the cost reduction (calibrate once, split only register+stack), the √2 half-stack-noise caveat, and the corrected (lighter) prerequisite list stated explicitly. |
| R3-12 | minor | **Accepted.** | Step 1b's determinism gate now uses ≥ 3 tmp-vs-tmp runs plus one tmp-vs-persisted comparison, with the bound set at 2× the max observed tmp-vs-tmp delta and a loud failure if tmp-vs-persisted exceeds it. |
| R3-13 | minor | **Accepted, and the root cause corrected.** Directly measured the persisted T21 master flat: it is a Siril-normalised 32-bit float in `[0, 1]` (median 0.354), not raw ADU — so the old "median as a fraction of 65535" check applied to the **master** is nonsensical for every telescope, not specifically wrong about T21's "13%" (which is a session-to-session spread figure, not a level). | §2.3 records the direct measurement. Step 7's level/saturation checks now run on a **sample of raw flat frames** (pre-Siril, still in native ADU), never on the master. |
| R3-14 | minor | **Accepted.** | Old Steps 2 (compatibility backstop), 8 (OSC), 9 (narrowband invalidation) and the heavy half of old Step 7 (dark-current measurement) moved to a new §5 "Future work" section; the renumbered core plan is Steps 0, 1a, 1b, 2, 3a/3b, 4a/4b, 5, 6, 7 (lightened), 8 (docs). |
| R3-15 | minor | **Accepted.** | The I6 measurement is now an explicit no-code sub-bullet inside Step 3, using the same `tmp_path` harness as gate (P), scheduled to run before E so Q7 has numbers in hand. |
| R3-16 | cosm | **Accepted.** | E's hash recording now goes in a follow-up ROADMAP commit, not an amended commit message; E uses a scratch script rather than editing tracked `scripts/run_m51.py`; the stale `_index.json` is mentioned in E; Q10's wording now says the deletion itself does not wait, only a real pipeline run does; §2.1's M31 row states the literal `T5` bias-filename token explicitly. |

### Round 4 (`docs/plan-flats-v4-review-round4.md`): each finding independently re-verified before acceptance

The reviewer independently re-ran `ingest.scan_session`/
`instrument_groups()`/`flat_index()`/`frame_identity_hash()` against the
still-undeleted real M51 tree and confirmed every round-1/3 number exactly
(T24 raw 190, T21 raw 4, T24 L bin1 42→21, T21 L bin1 4→2, T21 flat
30/20-unique). No numeric re-check was needed this round; this round's
findings are about specification completeness and document consistency,
not facts. All four majors and both minors were verified directly against
the current source before being accepted.

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| R4-1 | major | **Accepted.** Verified `RunSignature.contributor_stale` (`run_signature.py:214-249`) today returns `existing.frame_hash != frame_hash or existing.flat_frame_hash != flat_frame_hash`, with no mention of any third field — and that Step 2's prior text said `calibration_recipe` was "fed to `contributor_stale`" without ever touching that `return` line, which is exactly the failure mode this same file's own `flat_frame_hash` docstring already warns about for a first field. Also verified `tests/test_run_signature.py:215-229`'s `test_contributor_stale_true_on_flat_frame_hash_change_direct_call` as the exact test pattern missing for `calibration_recipe`. | Step 2 now spells out the literal `return` line change (`... or existing.calibration_recipe != calibration_recipe`), names the exact two real call sites that must pass the new parameter explicitly, and adds the missing direct-call and defaulting tests mirroring the existing `flat_frame_hash` ones. |
| R4-2 | major | **Accepted.** Confirmed via `grep -n "§6"` that all 17 hits were in the document body before §5's own heading (none inside §5 or §6 itself, apart from one unrelated "ROADMAP §5" cross-document reference already correct) — a clean, mechanically safe find-and-replace. | Fixed all 17 occurrences (`§6` → `§5`) across the TL;DR, §1.2, §2.1/2.2, the Gaps table (G7/G8/G11/G14), Step 2 and Step 4b, and the "Deliberately out of scope" heading's own self-reference. Re-grepped after the fix: zero remaining `§6` anywhere in the document. |
| R4-3 | major | **Accepted.** Verified `run_narrowband`'s `calibration_mode: CalibrationMode \| None` (`narrowband_orchestrator.py:51`) and `build_single_filter_master`'s identical single-value shape (`master_builder.py:328`) against `run_lrgb`'s `dict[str, CalibrationMode]` (`lrgb_orchestrator.py:209,1029`) — confirmed the same repeatable `TEL=value` syntax cannot fit `run_narrowband.py`, which already pins one `--telescope`. Also verified `run_narrowband_boost.py`'s `main()` does not pass `calibration_mode` to `build_single_filter_master` at all today (`skill/run_narrowband_boost.py:93-96`). | Step 5 now specifies three distinct CLI shapes: `run_stage.py` keeps the repeatable `TEL=value` dict form; `run_narrowband.py` and `run_narrowband_boost.py` both get a plain `--calibration-mode raw_local\|precalibrated` with no `TEL=` prefix, mapped to the single-value parameter each already has (or, for boost, newly threads through). Added the missing parser test for the plain-value form, distinct from the dict-form test. |
| R4-4 | minor | **Accepted.** Verified `build_master_flat`/`stage_frames` are the same functions `run_narrowband`/boost call (`narrowband_orchestrator.py:118,131-134`; `master_builder.py:366-372`), so Step 3b's staging/normalisation change is exactly as reachable from narrowband as from LRGB, and narrowband has no staleness tracking at all to catch a silent behaviour pickup. | Added an explicit "Narrowband/boost inertness" callout to Step 3b, making the same "verified inert today, for the same reason as G10" argument already made for the colour hash, and naming the concrete future risk (a real narrowband RAW_LOCAL flat set silently picking up `-norm=mul` with no rebuild trigger) rather than leaving it purely implicit in the already-deferred G11. |
| R4-5 | minor | **Accepted.** Confirmed Q4 (migration planning) is triggered by Q2's answer ("Lift"), not by whether Q3's measurement itself happens. | Retagged Q4 "(Future work, **Q2**-dependent)" with a one-line explanation of why Q3 is not the real dependency. |
| R4-6 | cosm | **Accepted, both parts.** | Dropped the stray "§" before "old Step 7" in §2.3. Renamed the Step 2 helper from `calibration_recipe()` to `calibration_recipe_parts()` throughout (Step 2 and Step 3b), so the function is no longer named identically to one field of its own two-element return value. |

### Round 5 (`docs/plan-flats-v4-review-round5.md`): fresh top-to-bottom pass; both findings independently re-verified before acceptance

The reviewer re-verified all of round 4's fixes directly against source
(confirmed correct as described) and did a full fresh pass — step
atomicity, dependency order, whether all ~11 open questions are genuinely
Kaveh's to answer, and a survey of every real project folder under
`D:\Raw Photo Backups\iTelescope\` not itemised in §2.1, to check none of
them is hiding an eighth real flat-bearing project. Nothing beyond the two
findings below rose above cosmetic.

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| F1 | major | **Accepted.** Verified directly: `grep -n "contributor_stale(" src/astro_pipeline/lrgb_orchestrator.py` gives three real call sites (lines 363, 543, 618), not two. Line 618 is the OSC colour loop, which passes a hardcoded literal `""` (matching line 614's `colour_flat_frame_hashes[contributor_key] = ""  # OSC never consults flats`), never through `_flat_frame_hash`/`calibration_recipe_parts`. Step 2's prior text said "both real call sites," which was simply false. | Step 2 now enumerates all three call sites, explicitly extends the same "verified inert today, for the same reason as G10/R4-4" treatment to the OSC one (inert because `calibration_recipe_parts` on an empty flat set, and separately on PRECALIBRATED, always returns `("", "")` regardless — matching the untouched default), and adds a one-line dependency note both here and on §5's own OSC bullet: lifting the `build_osc` `KeyError` later must also wire a real `calibration_recipe` into line 618, or a future OSC flat-recipe change will silently fail to invalidate an OSC master. |
| F2 | major | **Accepted.** Verified `pyproject.toml`'s `[tool.pytest.ini_options]` is exactly `testpaths = ["tests"]` — no `addopts`, no `markers`. Confirmed the real-data protocol as previously written relied on typing `pytest -m "not m51_pipeline"` correctly across roughly nine separate step invocations between Step 0 and event E, with no config-level enforcement and no plan-provided safety net (E's own snapshot doesn't exist until E is deliberately triggered) if a bare `pytest` slipped through and ran the four real tests early. | Step 0 now adds both `markers` registration and `addopts = ["-m", "not m51_pipeline"]` to `pyproject.toml`, so the exclusion is the default for a plain `pytest` call, not something anyone has to remember. Event E's own procedure (step 8) now explicitly overrides the default for its one real run and removes the `addopts` line (keeping the `markers` registration) in the same commit, so ordinary runs resume covering those four tests immediately afterward. |

### Round 6 (`docs/plan-flats-v4-review-round6.md`): fresh top-to-bottom pass; the one finding independently re-verified before acceptance

The reviewer re-verified both of round 5's fixes directly against source
(the three `contributor_stale` call sites and the OSC one's documented
inertness; `pyproject.toml`'s current, pre-Step-0 state being consistent
with what Step 0 is specified to add) and did a fresh top-to-bottom read
(mode inference, `calibration_index`/`flat_index` key-vs-value indexing
for Step 4b's `source`-filtering, mode-aware vs. mode-blind flat-hash call
sites, the dirty-tree diff) — nothing else rose above cosmetic.

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| 1 | major | **Accepted.** Verified directly: `ContributorSignature.to_dict()`/`from_dict()` (`run_signature.py:142-149,153-161`) and `RunSignature.to_dict()`/`from_dict()` (`:194-201,206-214`) are hand-written, enumerating every field by name — not `dataclasses.asdict()` — so a new dataclass field does not automatically appear in either. Step 2's prior text specified the new `calibration_recipe` field and `contributor_stale`'s wiring in detail but never mentioned `to_dict`/`from_dict` at all, and its only serialization-adjacent test ("old JSON loads and reads back `''`") only exercises the missing-key-on-load direction, not a non-empty value actually surviving a save/load round-trip. Confirmed the concrete consequence: once Step 3b makes T21's recipe non-empty, an unfixed `to_dict()` would silently drop it, so every run after the first would see a fresh non-empty value permanently mismatch a persisted `""`, forcing a full real-Siril T21 Luminance rebuild on every run — not the one-time rebuild Step 3b explicitly promises. | Step 2 now specifies the exact one-line additions to both `to_dict()` and `from_dict()`, states the failure mode explicitly, and adds a round-trip test with a non-empty `calibration_recipe` value (the missing half of the existing missing-key test). Step 5 gets the identical treatment for `RunSignature.calibration_header_fallback`, noted as lower severity since that field is diagnostic-only and `diff_invalidation` already ignores it. |

### Round 7 (`docs/plan-flats-v4-review-round7.md`): fresh pass weighted toward untouched sections; both findings independently re-verified before acceptance

The reviewer re-verified round 6's fix directly against
`run_signature.py` (both `to_dict`/`from_dict` additions and both
round-trip tests confirmed correct and non-trivial — using non-default
values that would actually fail if the fix regressed), then deliberately
weighted a fresh top-to-bottom read toward Steps 1a/6/7/8 and the §3 Gaps
table, sections rounds 3–6 hadn't focused on, plus a proportionality
check against the original ask. Nothing else rose above cosmetic; a
consistent off-by-one line-citation convention (omitting the pure
closing-brace/paren line) was noted as harmless and not fixed, per the
reviewer's own instruction not to chase it.

| # | Sev | Verdict | How addressed |
|---|---|---|---|
| 1 | major | **Accepted.** Verified directly: Event E's trigger list explicitly includes I6 ("only if Q7 = change"), I6 is a global, not per-telescope, change to `build_master` that the plan's own Gaps table already says affects "the selected T24 Luminance and both colour contributors," and I6's code would be merged at procedure step 3 — yet procedure step 6's pixel-identity check said any T24 pixel difference "must be investigated," with no branch for the case where I6 caused a deliberate, already-measured, already-approved difference. As written, adopting Q7 = change would make E's own safety check fire a false alarm on its very first real use. | Procedure step 6 now branches explicitly: pixel-identical (the original expectation) if I6 was not part of this run of E; if it was, the expected magnitude is the Step-3 I6 measurement's own already-recorded pixel difference (plus the Step-1b determinism bound as slack), and only a difference beyond that combined envelope is treated as a fault. |
| 2 | minor | **Accepted.** Verified directly: §5 (Future work) has exactly five bullets (OSC flats, flat↔light geometry, narrowband/boost invalidation, dark-current/dark-flat measurement, and the statistical local-vs-iTelescope measurement) — none of them mention BIN1→BIN2 flat binning or `Master_Flat*`/`Master_Bias*` use, both of which the "Deliberately out of scope (not deferred to §5)" list names explicitly. G14's row had said "Deferred to §5" for all three of its sub-items (including these two), directly contradicting that later, more-authoritative list — a real leftover inconsistency, not a nitpick, given Step 8's own stated principle of not letting a reader assume more was deferred than actually was. | G14's row now splits its disposition: dark-flats alone is deferred to §5 (matching its "dark-current/dark-flat-necessity measurement" bullet); BIN1→BIN2 binning and `Master_Flat*`/`Master_Bias*` use are corrected to "genuinely out of scope," pointing at the actual out-of-scope list. |

### Round 8 (`docs/plan-flats-v4-review-round8.md`): CONVERGED — no new findings

Re-verified both of round 7's fixes directly against the plan's own
current text (both correct as described) and did a fresh cross-cutting
pass specifically checking whether the accumulated fixes across all seven
prior rounds still *compose* — read `run_signature.py` in full against
Step 2's planned edits, the OSC call site the plan deliberately leaves
untouched, `calibration_policy.py`'s mode-inference logic behind Step 5's
hash-contract claim (traced specifically through M31/T05, the gate-(D)
project with the least data behind it), `ingest.py`/`calibration.py`
citations, the real uncommitted dirty-tree diff, and the `tests/conftest.py`
fixture list against Step 1a's stated gap. No inconsistency, seam, or
stale citation found anywhere.

**No findings table for this round — none were raised.**

**VERDICT: CONVERGED**

---

## Finalization (Kaveh, 2026-09-23)

Rev 9 resolves every item in §6 with Kaveh's actual decisions, in place of
the open-questions list rounds 1–8 accumulated and then converged on.
This was a finalization pass, not a new adversarial round: no new
gap-hunting was done, and none of the eight prior rounds' findings or
decisions were relitigated. The only substantive content change from rev
8 is §6 itself (now a decisions record) and the scope/sequencing text in
the TL;DR, §4 (Step 3's Q1-conditional wording resolved to "decided:
ships"), and §5 (each deferred item tagged with its actual disposition;
the per-target measurement reframed from "skippable" to "the explicit
next step," with Q9's full per-target risk reasoning against blind
reprocessing folded in). Steps 0–8's own technical content — the actual
code changes, tests, and gates — is unchanged from rev 8; this revision
does not touch how they are built, only what happens after they ship.
