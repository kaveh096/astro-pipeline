# Task 5, Step 12 substep plan — round-3 adversarial review

Reviewer: fresh pass, 2026-09-17, verified directly against the real
`src/astro_pipeline/pipeline.py` (1201 lines), `checkpoints.py`,
`export_image.py`, `tests/test_pipeline.py`, `tests/test_run_signature.py`,
`skill/run_narrowband_boost.py`, and `scripts/run_m51.py`. No source or plan
file was modified to produce this review.

## Verdict

**No substantive (non-cosmetic) defects found. The document is
execution-ready.** This round's pattern breaks: rounds 1 and 2 each found a
real defect that would have blocked a green suite (Section D's import split,
the `PipelineResult` cycle, the test-import-retargeting gap); this round
found only citation-precision nitpicks and one inconsequential import-hygiene
gap, none of which would cause a test failure, an import cycle, or a
behavior change if 12a-0/12a/12b/12c/12d are executed exactly as written.

## What was specifically re-verified (per the assigned focus areas)

**1. `pipeline_result.py` (12a-0) does not recreate a cycle.** Read
`checkpoints.py` (404 lines) and `export_image.py` (import block, lines
1-38) in full: neither imports anything from `pipeline.py`, `astro_pipeline`
top-level, or any orchestration-layer module — `checkpoints.py` only imports
`.export_image.export`; `export_image.py` imports only stdlib/numpy/tifffile/
astropy/PIL. `pipeline_result.py` importing `Checkpoint` from `.checkpoints`
and `ExportResult` from `.export_image` is safe — both are true leaf modules
with respect to this cycle. Confirmed no source module under `src/astro_pipeline/`
imports back from `.pipeline` (grepped the whole tree — zero hits), so
`PipelineResult` was and remains the only such case.

**2. The 6-name test-import-retargeting list in 12a is complete and
correct.** `tests/test_pipeline.py:11-27` actually imports 15 names from
`astro_pipeline.pipeline`, not 6 — but cross-checking each of the 15 against
Section D's three-way split: 6 (`build_group_master`, `contributor_dir`,
`contributor_fwhm_arcsec`, `discover_luminance_contributors`, `resolve_lights`,
`select_luminance_source`) are in the "moves fully" list and would break
without retargeting; the other 9 either stay in `pipeline.py` (narrowband-only
names, or "needed by both" names re-imported independently) or are re-exported
(`run_lrgb` via the new `from .lrgb_orchestrator import run_lrgb` line). The
local import at line 585 (`resolve_lights`) is already covered by the same 6.
Grepped the entire repo for `from astro_pipeline.pipeline import` /
`astro_pipeline.pipeline.`: only 7 files reference it at all
(`scripts/run_m51.py`, `skill/run_stage.py`, `skill/run_narrowband_boost.py`,
`skill/run_narrowband.py`, `skill/interview.py`, `tests/test_pipeline.py`,
`tests/test_run_signature.py`) and none besides `test_pipeline.py` imports
any of the 6 names. No 7th name, no missed file. `build_single_filter_master`
(imported into `pipeline.py` but never called in its body — verified by grep,
exactly one hit, the import line itself) correctly stays classified as
"stay only in pipeline.py."

**3. No second `PipelineResult`-shaped gap.** Confirmed by reading the full
file: `pipeline.py`'s only top-level definitions are `PipelineResult`,
`run_lrgb`, and `run_narrowband` — no other class/function is *defined* (as
opposed to imported) in the module. Every other name on Section D's "needed
by both" list (`scan_session`, `_delete_if_exists`, `stretch_rgb`, `usable`,
`pipeline_dir`, `infer_calibration_mode`/`infer_flat_policy`,
`ColourContributorBuilder`, `CalibrationMode`/`FlatPolicy`, `DEFAULT_PEDESTAL`,
`checkpoint`/`save_checkpoints`) is defined in an already-extracted leaf
module, not in `pipeline.py` itself, so none of them can produce a
`pipeline.py`-vs-`lrgb_orchestrator.py` two-way cycle the way a
locally-*defined* dataclass could. `PipelineResult` really was the only one.

**4. 12d's explicit signature is byte-for-byte correct.** Diffed the real
`def run_lrgb(...)` (pipeline.py:195-209) against the substep doc's proposed
wrapper (lines 531-546): all 14 parameters, in the same order, with the same
types and the same defaults (`lum_binning: int = 1`, `rgb_binning: int = 2`,
`stretch_method: str = "autostretch"`, `lum_source: tuple[str, int] | None =
None`, `pedestal: float = DEFAULT_PEDESTAL`, `stop_after: str | None = None`,
`force: set[str] | None = None`, `flat_policy: FlatPolicy | None = None`,
`calibration_mode: dict[str, CalibrationMode] | None = None`) match exactly.

**5. 27-attribute inventory and monkeypatch inventory spot-checked, both
still accurate on substance.** Grepped both `tests/test_pipeline.py` and
`tests/test_run_signature.py` for every `monkeypatch.setattr(pipeline_module,
"<name>", ...)` site: found exactly 27 in `test_pipeline.py` and exactly 4 in
`test_run_signature.py`. Of `test_pipeline.py`'s 27, exactly 3 (lines 1074,
1112, 1159) are `run_narrowband`-only tests correctly excluded from the
24-site retargeting table (they patch `scan_session`/`stretch_rgb`, both on
the "needed by both, no retarget needed" list) — the remaining 24 match the
document's table exactly, line-for-line and test-function-for-test-function.
`test_run_signature.py`'s 4 sites (436, 443, 650, 658) match exactly. No
other test file imports `pipeline_module` at all (grep confirms only these
two files do). Total 28-site retargeting count is exactly right, and the two
"silent danger" names (`scan_session`, `_delete_if_exists`) are correctly
flagged as needing extra attention since they stay valid attributes of
`pipeline_module` after the move.

## Findings — cosmetic only

1. **(Cosmetic) Row 6 of the Section A.2 table (`final`) cites two lines
   that don't actually reference `final`.** Lines 848 and 916
   (`lum_for_compose_path = lum_bg` and `shutil.copy2(cropped[0],
   lum_for_compose_path)`) are listed among the "reconciled" reads of
   `final`, but neither line contains the token `final` — they're reads of
   `lum_for_compose_path`. The row's actual conclusion (that `final` crosses
   all three phase boundaries) is unaffected: lines 850, 853, 883, 909, and
   933 in the same citation list do genuinely read `final`, and the
   masters/final columns' citations are accurate. This is the same class of
   slip round 1 already found and fixed once in this exact row (the
   "line 415 dropped" correction) — a second, smaller instance of it
   survived that fix.

2. **(Cosmetic) One off-by-one line citation for `previous`.** Row 7 cites
   line 793 as a read site; the actual `previous=previous` keyword argument
   is on line 794 (line 793 is the preceding `selected_path, f"01_master_..."`
   argument in the same multi-line `checkpoint(...)` call). No effect on the
   conclusion — the surrounding "804-808" range citation for the second
   checkpoint's read/write cycle is a real range that does contain both the
   true read (805) and write (808) lines.

3. **(Cosmetic / minor doc-completeness gap, not a functional defect) 12a-0
   doesn't mention dropping the now-also-unused `ExportResult` import
   alongside `Checkpoint`.** `pipeline.py:135` currently reads
   `from .export_image import ExportResult, export`; `ExportResult` is used
   only as the `PipelineResult.export_result` field's type annotation
   (`pipeline.py:184`), exactly parallel to how `Checkpoint` is used only for
   the `checkpoints: list[Checkpoint]` field (which 12a-0 correctly identifies
   and drops). Once `PipelineResult` moves to `pipeline_result.py`, `pipeline.py`
   no longer needs `ExportResult` either — `export` (the function) is still
   needed for `run_narrowband`'s own `export(...)` call at line 1192, so only
   the `ExportResult` half of that import line should go. Left as originally
   written, this produces one unused import in `pipeline.py` post-12a-0 — no
   test failure, no cycle, no behavior change, just a lint-flaggable dead
   import. Trivial to fix in passing during 12a-0's own edit to that import
   line; not worth a separate sub-step.

4. **(Cosmetic wording ambiguity, no functional effect) 12b's `_build_masters()`
   line range overlaps the lines it says move to `__init__`.** The bullet
   list says lines 789-790 (`previous = None` / `previous_linear: bool | None
   = None`) move into `__init__`, then the next bullet says `_build_masters()`
   moves "lines 309-810" verbatim — a range that literally includes 789-790.
   Read charitably this is shorthand for "the overall span this logic came
   from," not a literal instruction to duplicate those two assignments in
   both places; an implementer who reads both bullets together (as intended)
   won't create a bug. Worth a one-line wording tweak (e.g., "lines 309-788
   and 791-810, excluding the previous/previous_linear initialization already
   moved to `__init__`") but not worth blocking on.

## Convergence assessment

Per the stated criterion ("stop when a round finds only cosmetic issues"):
this round qualifies. Everything substantive that was in scope for this pass
— the `pipeline_result.py` cycle-avoidance design, the 6-name test-import
list's completeness, the search for a second `PipelineResult`-shaped gap,
12d's signature fidelity, and a spot-check of the 27-attribute/monkeypatch
inventories — checked out correct against the real code. Recommend treating
`docs/task5-step12-substeps.md` as execution-ready for 12a-0 through 12d
without a further review round; the 4 cosmetic items above can be folded in
opportunistically (or ignored) without gating the start of implementation.
