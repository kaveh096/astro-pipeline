# Adversarial review, round 2: `docs/task5-step12-substeps.md`

Reviewer note: this is the second, independent adversarial pass on this
document, following round 1 (`docs/task5-step12-review-round1.md`), whose
corrections are already merged inline into the reviewed document. Every
claim below was re-derived directly from `src/astro_pipeline/pipeline.py`
(full file, lines 1-1200, both `run_lrgb` 195-1038 and `run_narrowband`
1041-1200 read in full), `src/astro_pipeline/__init__.py`,
`src/astro_pipeline/master_builder.py` and `colour_contributor.py`'s own
import blocks (to check precedent), and `tests/test_pipeline.py` /
`tests/test_run_signature.py` (full grep of every
`from astro_pipeline.pipeline import` site and every
`monkeypatch.setattr(pipeline_module, ...)` site). Nothing here is taken on
the reviewed document's, or round 1's, authority. No source file was
modified to produce this review.

Overall assessment: round 1 was right that Section D needed the
three-way-split correction, and that correction is now internally
consistent for the "who calls what inside `run_lrgb`/`run_narrowband`"
question it asked. But **that question was too narrow**, and this round
finds two real problems Section D's per-function-usage framing structurally
could not catch, because neither is about which function *calls* a name —
both are about a name's *cross-module import surface* independent of
either function's body. One of the two is a genuine circular-import bug
that would fail on the very first command in 12a's own checklist. Given
the established pattern here (parent plan round 1, parent plan round 2,
this document's own re-derivation, and this document's round 1 each found
something the previous pass missed), this round continues that pattern
rather than closing it out.

---

## Finding 1 (CRITICAL — breaks `python -c "import astro_pipeline.pipeline"`, the very first command in 12a's own checklist)

**`PipelineResult` is never classified anywhere in Section D, and the
omission creates a real Python circular import between `pipeline.py` and
`lrgb_orchestrator.py`.**

Facts, verified directly against `pipeline.py`:
- `PipelineResult` is defined in `pipeline.py` (line 180-186) and, per the
  parent plan's Section 1, is explicitly meant to stay there ("small enough
  to leave here rather than spin out a one-class module").
- `run_lrgb` **instantiates it at runtime**, not just as a type hint:
  `result = PipelineResult()` at line 301, `return result` at line 1038,
  plus the `-> PipelineResult` return annotation at line 210. Instantiation
  is a real name lookup at call time — `from __future__ import annotations`
  (which the module does use, line 101) only defers *annotation*
  evaluation; it does nothing for `PipelineResult()` the constructor call.
- Section D's "Stays ONLY in `pipeline.py`" list discusses `PipelineResult`
  only in passing, as the reason `Checkpoint` (the type) stays — "needed
  for `PipelineResult`'s own `list[Checkpoint]` field hint" — but never
  asks the actual question this step raises: once `run_lrgb`'s body moves
  to `lrgb_orchestrator.py`, where does `lrgb_orchestrator.py` get
  `PipelineResult` from? The document's three lists (moves fully / needed
  by both / stays) have no entry for it at all.

Section E's 12a instructions say to add `from .lrgb_orchestrator import
run_lrgb` to `pipeline.py` "so every existing external caller... keeps
working." Every other extraction in this refactor (Steps 1-11, confirmed by
reading `master_builder.py`'s and `colour_contributor.py`'s own import
blocks — neither imports anything from `pipeline.py`) has been a clean,
one-directional DAG edge: new module imports from an existing leaf module,
`pipeline.py` imports back from the new module for re-export, done. Step 12
is the **first** step where the new module also needs something
(`PipelineResult`) that only exists in `pipeline.py` itself — the exact
shape Risk 2 warns against ("never from `pipeline.py` itself, since
`pipeline.py` will import *from* them, for its re-exports"), but Risk 2's
own diagram, and this document's Section D built on it, never noticed
`PipelineResult` is such a case, because it was implicitly treated like
`Checkpoint`/`ColourContributor` (type-annotation-only, hence harmless)
when it is not — it is constructed at runtime.

Concretely, given the natural placement (top-of-file import block, matching
every prior step's own convention — nothing in this document says to place
the new import anywhere else): the first time anything imports
`astro_pipeline.pipeline` (including 12a's own literal first checklist
command, `python -c "import astro_pipeline.pipeline"`), Python begins
executing `pipeline.py` top to bottom, reaches `from .lrgb_orchestrator
import run_lrgb` (in the top import block, i.e. *before* `class
PipelineResult` is defined at line 180), and starts loading
`lrgb_orchestrator.py` for the first time. `lrgb_orchestrator.py` in turn
does `from .pipeline import PipelineResult` — but `astro_pipeline.pipeline`
is already registered in `sys.modules` (partially initialized, execution
paused inside its own import block, before `PipelineResult` exists as an
attribute) — raising `ImportError: cannot import name 'PipelineResult' from
partially initialized module 'astro_pipeline.pipeline' (most likely due to
a circular import)`. I checked whether moving the cross-import to the
*bottom* of `pipeline.py` (after `PipelineResult` is defined) fixes it: it
does for "something imports `pipeline` first," but breaks the mirror case
("something imports `lrgb_orchestrator` first," which Section E's own
Section C retargeting explicitly introduces via
`import astro_pipeline.lrgb_orchestrator as lrgb_orchestrator_module` in
28 test sites) — whichever module loads first, the other's cross-reference
resolves against a partially-initialized module and fails. There is no
placement of a plain top-level `import` statement in either file that
avoids this; it requires either genuinely breaking the cycle or deferring
one side of it.

**Recommendation:** extract `PipelineResult` into its own tiny module
(e.g. `pipeline_result.py`, mirroring `filter_constants.py`'s own reason
for existing per Section 1 of the parent plan: "so lower-level new modules
don't have to import from `pipeline.py`"), imported by both `pipeline.py`
and `lrgb_orchestrator.py`. This is a small, mechanical, low-risk addition,
but it is not in either plan document today and must be added before 12a
starts — not discovered by trial and error during execution. Note this
also pre-empts an identical problem at **Step 13**
(`narrowband_orchestrator.py`), since `run_narrowband` constructs
`PipelineResult()` too (line 1109) and will hit the exact same cycle when
it moves out of `pipeline.py`.

---

## Finding 2 (HIGH — breaks collection of `tests/test_pipeline.py` in its entirety, not just tests that call `run_lrgb`)

**Section D's "moves fully to `lrgb_orchestrator.py`" list includes six
names that `tests/test_pipeline.py` imports directly from
`astro_pipeline.pipeline` at module level, independent of anything
`run_lrgb`/`run_narrowband` do internally — and 12a's plan never accounts
for this.**

Verified directly: `tests/test_pipeline.py` lines 11-27 do:
```python
from astro_pipeline.pipeline import (
    NARROWBAND_PALETTES,
    _NarrowbandNormalizingReport,
    build_group_master,
    build_single_filter_master,
    contributor_dir,
    contributor_fwhm_arcsec,
    discover_luminance_contributors,
    equalize_narrowband_channels,
    infer_calibration_mode,
    infer_flat_policy,
    normalize_narrowband_filter_name,
    resolve_lights,
    run_lrgb,
    run_narrowband,
    select_luminance_source,
)
```
Six of these — `build_group_master`, `contributor_dir`,
`contributor_fwhm_arcsec`, `discover_luminance_contributors`,
`resolve_lights`, `select_luminance_source` — are on Section D's "moves
fully... removed from `pipeline.py` entirely" list. (Line 585 also does a
local `from astro_pipeline.pipeline import resolve_lights` inside one test
function — same problem, second instance.) These six are legitimately
"used by `run_lrgb` only" as Section D's per-function analysis correctly
determined (confirmed: none appear in `run_narrowband`'s body) — but that
was the wrong question to ask for this list, because `pipeline.py`
currently re-exports them *not because `run_lrgb` needs them there*, but as
a side effect of already having imported them from `master_builder.py`
(`build_group_master`, `contributor_fwhm_arcsec`, `resolve_lights`),
`luminance_selection.py` (`discover_luminance_contributors`,
`select_luminance_source`), and `workspace.py` (`contributor_dir`) for
`run_lrgb`'s own use. Grep-confirmed these six, and only these six, are the
overlap — I checked every other name in test_pipeline.py's import block
against Section D's three lists and found no further collisions, and
confirmed (via grep of the whole `tests/` directory) that no other test
file does `from astro_pipeline.pipeline import ...` at all, and that
`test_run_signature.py` correctly imports `RunSignature`/`ContributorSignature`/etc.
straight from `astro_pipeline.run_signature`, not `pipeline` — so Section
D's "moves fully" categorization for the RunSignature-family names and the
reconciliation-family names has no equivalent test-import problem.

Section E's 12a plan only says to add `from .lrgb_orchestrator import
run_lrgb` to `pipeline.py` — it re-exports exactly one of the seven names
`tests/test_pipeline.py` imports from `pipeline` that are moving. If
executed literally, `pipeline.py` stops importing the other six (Section
D's own instruction: "remove them from `pipeline.py`'s import block"), and
`tests/test_pipeline.py`'s own module-level import statement raises
`ImportError: cannot import name 'build_group_master' from
'astro_pipeline.pipeline'` at collection time. This is not a NameError
inside one test body (which is what round 1's Finding 1 and this
document's own Finding 1 above produce) — it is a collection-time failure
for the *entire file*, meaning every one of `test_pipeline.py`'s ~68 tests
(including all of Section 4b's new masters/reconciled/final coverage, all
28 monkeypatch sites from Section C.1) would report as a collection error,
not "284 passed." This directly answers the review brief's question of
whether 12a "as now specified" reaches a green full-suite run: no, for this
reason independent of Finding 1.

**Recommendation:** add an explicit 12a bullet updating
`tests/test_pipeline.py`'s import block (lines 11-27) and its local import
(line 585) to pull these six names from their real current homes —
`from astro_pipeline.master_builder import build_group_master,
contributor_fwhm_arcsec, resolve_lights`,
`from astro_pipeline.luminance_selection import
discover_luminance_contributors, select_luminance_source`,
`from astro_pipeline.workspace import contributor_dir` — matching the
precedent already set for `resolve_instrument_profile`/
`UnknownInstrumentError` (already updated to import from
`background_color` directly, confirmed at `test_pipeline.py:7`, per the
parent plan's Step 6). This is the same "update call sites, don't add a
permanent facade" approach the parent plan already commits to elsewhere.

---

## Finding 3 (LOW/MEDIUM — contradicts the "byte-for-byte identical signature" commitment, though no current caller is affected)

12d's proposed collapsed wrapper:
```python
def run_lrgb(project_dir, telescope, target, ra_hours, dec_deg, **kwargs) -> PipelineResult:
    return LRGBOrchestrator(project_dir, telescope, target, ra_hours, dec_deg, **kwargs).run()
```
is not literally byte-for-byte identical to the original 14-parameter
signature (`lum_binning`, `rgb_binning`, `stretch_method`, `lum_source`,
`pedestal`, `stop_after`, `force`, `flat_policy`, `calibration_mode` all
disappear from the visible signature, collapsing into `**kwargs`) —
`inspect.signature(run_lrgb)`, `help(run_lrgb)`, and the exact exception
raised for a misspelled keyword (`LRGBOrchestrator() got an unexpected
keyword argument` vs. today's `run_lrgb() got an unexpected keyword
argument`) all change. I checked every real call site of `run_lrgb`
(`scripts/run_m51.py`, `skill/run_stage.py`, and all 15 call sites across
`tests/test_pipeline.py`/`tests/test_run_signature.py`): every single one
passes `telescope`/`target`/`ra_hours`/`dec_deg` and everything after as
keywords, never positionally past the first argument, and no test inspects
`run_lrgb`'s signature or checks the exact wording of an
unexpected-keyword `TypeError`. So this does not break anything in the
current suite — but it is a real, avoidable deviation from the parent
plan's explicit "byte-for-byte identical public signature" commitment.
**Recommendation:** write `run_lrgb`'s wrapper with the full explicit
14-parameter signature (mirroring `LRGBOrchestrator.__init__`'s own),
rather than `**kwargs` — no more code, strictly more faithful to the
stated contract.

---

## What this round re-confirms as correct (checked directly, not re-trusted)

- **Section A's `self.` state inventory (27 attributes) — no further
  missed cross-phase variable found.** I independently re-grepped every
  constructor parameter (`project_dir`, `telescope`, `ra_hours`, `dec_deg`,
  `lum_binning`, `rgb_binning`, `lum_source`, `flat_policy`,
  `calibration_mode`, `pedestal`) across the full file and confirmed each
  is read for the last time well before line 818 (masters-only), matching
  Section A.1's claims exactly. I also re-verified `report`,
  `old_signature`, `new_signature`, `run_signature_path` (Section A.3's
  masters-internal demotion) have no read at or after line 820, and
  additionally checked `out` (line 298) — not on any list in the document,
  but confined to lines 298-306 (masters setup only) and already correctly
  folded into Section E's `__init__` bullet (`self.out`) regardless, so
  this is a non-issue, not a new finding. `rgb_reconciled` (line 853, read
  at 989/996) and `reference_pos` (line 706, read at 930/934/952) both
  re-verified byte-exact against the real file.
- **Section C's monkeypatch inventory** — spot-checked 6 of 28 sites
  directly against the real test files (`test_pipeline.py:1482`
  `scan_session`, `:2011` `crop_to_common_coverage`, `:2020`
  `match_gain_offset`, `:2031` `combine_same_grid`, `:2185`
  `_delete_if_exists`; `test_run_signature.py:436` `scan_session`, `:443`
  `build_group_master`) — every line number and target name matches
  exactly. Consistent with round 1's full-inventory confirmation; no
  further spot-check turned up a discrepancy.
- **Section E's phase-boundary line ranges** — re-verified against the
  real file: masters body 309-810 (excludes 812-818's `stop_after`
  check), reconciled body 820-969 (excludes 971-977), final body 979-1036
  (excludes the bare 1038 `return result`). All exactly correct.
- **Section D's per-function usage claims for the "needed by both" list**
  (`scan_session`, `_delete_if_exists`, `stretch_rgb`, `usable`,
  `pipeline_dir`, `infer_calibration_mode`/`infer_flat_policy`,
  `ColourContributorBuilder`, `CalibrationMode`/`FlatPolicy`,
  `DEFAULT_PEDESTAL`, `checkpoint`/`save_checkpoints`) — re-verified every
  one is genuinely called in both `run_lrgb` and `run_narrowband`'s own
  bodies, at the line numbers cited. Correct as corrected by round 1.
- **`shutil`/`astropy.io.fits`** — re-verified by grepping every
  `shutil.`/`fits.` call site in the file: all 8 occurrences are inside
  `run_lrgb` (lines 428-996), none in `run_narrowband`. Section D's "moves
  fully, not referenced anywhere else" claim for these two is correct.
  (Cosmetic, zero-impact aside: `import numpy as np` at line 107 is dead —
  `np.` appears nowhere in the file at all, not just outside these two
  functions — same harmless-dead-import class round 1 already flagged for
  `INSTRUMENT_PROFILES`/`OSC_INSTRUMENT_PROFILES`/`OSCInstrumentProfile`;
  not worth its own action item.)

---

## Bottom line

Two real, previously-uncaught problems, both severe enough that **12a as
currently written does not reach a green full suite run** — one
(`PipelineResult`, Finding 1) fails immediately and unconditionally on
12a's own first checklist command regardless of test content; the other
(Finding 2) fails test collection for the entire `test_pipeline.py` file.
Both are loud/self-diagnosing, not silent behavior changes (same character
as round 1's finding), but both should be fixed in the document — Finding
1 by adding a `pipeline_result.py` extraction ahead of or as part of 12a,
Finding 2 by adding an explicit test-import-update bullet to 12a — before
execution starts, rather than discovered by running it. Finding 3 is a
real but non-blocking deviation from the "byte-for-byte identical
signature" commitment, worth a one-line fix. Everything else re-checked in
this round (Section A's inventory, Section C's monkeypatch targets, Section
E's line ranges, Section D's needed-by-both list) held up under independent
re-verification against the real code — this is not a case of the document
being generally unreliable, but of two specific, narrow gaps in Section D's
analytical method (it asked "does `run_lrgb`/`run_narrowband` call this
name" and never asked "does anything else import this name from
`pipeline.py`'s namespace, or does the new module need something back from
`pipeline.py`") that neither the parent plan's two rounds nor this
document's own round-1 review happened to probe.
