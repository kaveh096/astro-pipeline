# Adversarial review, round 1: `docs/task5-step12-substeps.md`

Reviewer note: every claim below was re-derived directly from
`src/astro_pipeline/pipeline.py` (full file, lines 1-1200), `tests/test_pipeline.py`
(full `def test_` inventory + grep of every `monkeypatch.setattr(pipeline_module, ...)`
call site), and `tests/test_run_signature.py` (same grep). Nothing here is taken on
the reviewed document's authority. No source file was modified to produce this
review.

Overall assessment: the reviewed document is **very accurate** on the two areas
it was most careful about — the checkpoint-label/on-disk-filename inventory
(Section B) and the monkeypatch retargeting inventory (Section C), including
exact test-function-name attribution for all 28 sites. Both were verified
line-by-line against the real files and found correct. The one genuinely
serious, previously-uncaught problem is in a part of the document that reads
as "obviously fine, mechanical bookkeeping" and was under-scrutinized as a
result: **Section D's import-migration split**.

---

## Finding 1 (HIGH — breaks Step 12a as literally written, not silent but not caught until execution)

**Section D's "Stays in `pipeline.py`" list is wrong in its own stated
rationale for ~11 of its ~13 entries.** The list is introduced as "still used
by `run_narrowband` and/or `PipelineResult`" and Section E's 12a instructions
say explicitly:

> Move the imports identified in Section D's "moves fully" list into the new
> file's own import block; remove them from `pipeline.py`'s import block
> (Section D's "stays" list remains in `pipeline.py`, needed by `run_narrowband`).

This instructs the implementer to leave every "stays" name **only** in
`pipeline.py`. But grep-verified against the real file, the following names
are called or referenced directly inside `run_lrgb`'s own body (lines
195-1038), not just inside `run_narrowband` (1041-1200):

| Name | Used inside `run_lrgb` at | Doc's "stays" citation (implies narrowband-only) |
|---|---|---|
| `pipeline_dir` | 298, 415 | "(1106)" |
| `checkpoint` | 792, 803, 833, 962, 1006 | not cited for run_lrgb at all |
| `save_checkpoints` | 799, 810, 840, 969, 1013 | not cited for run_lrgb at all |
| `usable` | 826, 854, 986 | "(1158/1175)" |
| `ColourContributorBuilder` | 614, 686 | "(1131)" |
| `CalibrationMode` (runtime enum compare, not just annotation) | 384, 562 | "(type hints/params)" only |
| `FlatPolicy` (runtime enum compare/value) | 208 default, 386, 561 | "(type hints/params)" only |
| `DEFAULT_PEDESTAL` | 205 (parameter **default value**) | "(default param)" — correctly named as used, but not flagged as needing dual-import |
| `infer_calibration_mode` | 343, 558 | "(1119/1120)" only |
| `infer_flat_policy` | 392, 563 | "(1119/1120)" only |
| `stretch_rgb` | **991** (the `is_rgb_only` finalize branch) | "(1177)" only |

`stretch_rgb` at line 991 is the clearest smoking gun: it is `run_lrgb`'s own
RGB-only stretch call (`compose = stretch_rgb(rgb_in, final,
output_stem=composite_name, method=stretch_method)`), completely separate
from `run_narrowband`'s own call to the same function at line 1177. The
document cites only the narrowband call site and concludes the name "stays"
in `pipeline.py`, missing its own function's use of the same name entirely.

By contrast, Section C.3 gets this exact class of problem right for
`scan_session`/`_delete_if_exists` — it explicitly says
`lrgb_orchestrator.run_lrgb`'s own `from .ingest import scan_session` /
`from .contributor_staleness import _delete_if_exists` "resolve
independently" of `pipeline.py`'s copy, i.e. it correctly anticipates that
`lrgb_orchestrator.py` needs its **own** import of these two names alongside
`pipeline.py` keeping its own for `run_narrowband`. Section D just never
extends that same reasoning to the other 11 names above, despite them
following the identical pattern (both functions independently need the same
name from the same source module).

**Consequence if 12a is executed as written:** `DEFAULT_PEDESTAL` is
evaluated at `def run_lrgb(...)` statement execution time (default values are
*not* covered by `from __future__ import annotations`, unlike type hints,
which the module does use at line 101) — so a missing `DEFAULT_PEDESTAL`
import would fail immediately on `python -c "import
astro_pipeline.lrgb_orchestrator"`, i.e. the very first check Section E's own
12a checklist runs. The rest (`pipeline_dir`, `checkpoint`,
`save_checkpoints`, `usable`, `ColourContributorBuilder`,
`infer_calibration_mode`, `infer_flat_policy`, `stretch_rgb`, plus the
runtime `CalibrationMode`/`FlatPolicy` enum comparisons at 384/386/561/562)
are ordinary function-body statements, not annotations, so they would raise
`NameError` the moment any test actually calls the moved `run_lrgb` —
i.e. on the very next command in 12a's own checklist ("full suite green").
This is a loud, self-diagnosing failure, not a silent behavior change, so it
would not survive past the first test run — but it means the document's
Section D/E, read and followed literally, do **not** actually get 12a to a
green suite on the first attempt, contradicting the document's own claimed
outcome. Fix: state explicitly (as C.3 already models) that every name in
the "stays" list which `run_lrgb` also uses directly must be imported
independently into *both* `pipeline.py` and `lrgb_orchestrator.py` — which,
after re-deriving it here, is all of the "stays" list except `Checkpoint`
(the type, only needed for `PipelineResult`'s field annotation, itself
deferred-evaluated) and `build_single_filter_master` (confirmed by grep:
zero call sites in either `run_lrgb` or `run_narrowband`'s bodies, exists
purely as a re-export for `skill/run_narrowband_boost.py` — the document is
correct about this one specifically).

---

## Finding 2 (LOW/cosmetic — an incompleteness in Section D's own claim of exhaustiveness)

Section D never mentions `INSTRUMENT_PROFILES`, `OSC_INSTRUMENT_PROFILES`,
`OSCInstrumentProfile` (imported at `pipeline.py:111-113` from
`background_color`) or `ColourContributor` (the dataclass, imported at
`pipeline.py:125` alongside `ColourContributorBuilder`, distinct from it).

- `INSTRUMENT_PROFILES`/`OSC_INSTRUMENT_PROFILES`/`OSCInstrumentProfile`:
  grep-confirmed these three names appear **nowhere else** in
  `pipeline.py` outside their own import lines — not in `run_lrgb`, not in
  `run_narrowband`, not re-exported to any test or skill script (verified:
  `tests/test_pipeline.py` imports `resolve_instrument_profile`/
  `UnknownInstrumentError` directly from `astro_pipeline.background_color`,
  not from `astro_pipeline.pipeline`). These are pre-existing dead imports,
  unrelated to this refactor's correctness either way — they can simply be
  dropped during the import-block edit, or ignored (they don't move, since
  nothing needs them to). Section D's "every name either belongs wholly to
  one file's needs or the other's" claim is technically inaccurate (these
  belong to neither), but this has zero behavior impact.
- `ColourContributor` (the dataclass): used once, as a type annotation only,
  at `pipeline.py:566` (`contributors: list[ColourContributor] = []`) inside
  `run_lrgb`; not used by `run_narrowband`. Because `from __future__ import
  annotations` is active (line 101) *and* this is a local-variable
  annotation (which CPython never evaluates at runtime regardless of that
  future-import, unlike module/class-level annotations), a missing import
  here would **not** raise at runtime — this is a real gap in Section D's
  bookkeeping but has no execution consequence, only a static-type-checker
  (mypy/pyright) consequence if one is run against the new file. Should
  still be added to the "moves fully" list for completeness/correctness
  under a type checker.

---

## Finding 3 (COSMETIC — citation-only errors, conclusions unaffected)

Section A.2's `self.` state table is otherwise excellent and its central
finding (`rgb_reconciled` genuinely crosses reconciled→final and was missed
by both prior rounds) is **confirmed correct** — verified directly:
`pipeline.py:853` sets `rgb_reconciled = final / "rgb_reconciled.fit"` inside
the reconciled body, and it is read again in `_finalize()`'s own territory at
`pipeline.py:989` and `pipeline.py:996` (both branches of the
`is_rgb_only`/else split). This is a real, valid, previously-uncaught
finding and the recommendation to store it as `self.rgb_reconciled` is
correct.

Two minor citation slips found while re-deriving the table, neither changing
any conclusion:

- Row 6 (`final`, the output-directory Path) cites line 415 as a read site.
  Actual `pipeline.py:415` reads `pipeline_dir(project_dir) / lum_group_name
  / "lights" / f"master_{LUMINANCE_FILTER.lower()}.fit"` — it does not
  reference `final` at all (it recomputes the pipeline dir independently for
  this one stale-master-deletion path). `final` is still correctly
  established as crossing all three phases by the table's many other,
  verified-correct citations (300, 571, 657, 777-785, 801, 825, 848, 853,
  883, 908, 909, 916, 933, 984, 985, 988, 989, 993, 994, 1028 all check out).
- Row 7/8 (`previous`/`previous_linear`) cite only "793, 797" for reads
  inside the masters body, but there are actually **two** full
  read/write cycles in masters, not one: checkpoint 01's cycle (read at
  `794` as the `previous=previous` keyword argument, write at `797`) *and*
  checkpoint 02's cycle (read at `805`, write at `808`, for
  `"02_primary_rgb_colour_calibrated"`). The second cycle is not cited. Same
  non-issue as above — the conclusion ("previous`/`previous_linear` must be
  `self.` and thread across all three phases) is unaffected, since the table
  already independently establishes the crossing via the reconciled/final
  citations.
- Row 5 (`is_rgb_only`)'s reconciled-phase citations (`821, 824, 855, 862,
  868, 974`) include one comment line (821 is prose: "# Skipped entirely for
  `is_rgb_only`") and two lines that are inside an `is_rgb_only`-guarded
  branch but don't contain the literal token (`862`, `868`). Not a
  substantive error — the variable genuinely is read at 824, 855, and 974 in
  that phase, which alone establishes the crossing correctly.
- The document's own line-count header states `pipeline.py` is "1201 lines
  total"; `wc -l` reports 1200. Trivial off-by-one, no effect on any other
  citation (all line numbers checked above against the real file are
  correct in absolute terms).

---

## What the document gets right (confirmed, not just asserted)

- **Section B (checkpoint labels + on-disk filenames):** every single
  literal string and its cited line number was checked against the real
  file and is correct, including the subtle ones (`"rgb_colour_calibrated.fit"`
  at 801 being the *primary*-contributor top-level copy, distinct from each
  contributor's own `contrib_dir/rgb_colour_calibrated.fit` referenced
  elsewhere at line 609).
- **Section C (monkeypatch inventory):** all 24 `test_pipeline.py` sites and
  all 4 `test_run_signature.py` sites were independently re-greped; every
  line number and every test-function attribution (by locating each site's
  enclosing `def test_...`) is exactly correct, including the subtlety that
  `ColourContributorBuilder.build_rgb` monkeypatches target the class object
  directly (no retargeting needed) and that `scan_session`/`_delete_if_exists`
  are a "silently wrong, not loud" trap because `run_narrowband` also owns
  them.
- **Section A.3's demotion of `report`/`run_signature_path`/`old_signature`/
  `new_signature` to masters-internal locals:** independently re-traced every
  read site of all four names; none is read at or after line 820
  (`report`'s last real use is line 694; `run_signature_path`'s is 786;
  `old_signature`'s is through 756; `new_signature`'s is through 786). The
  demotion is correct for the single-method `_build_masters()` design this
  document adopts.
- **Section E's `__init__` validation-before-`final.mkdir()` ordering
  claim:** confirmed — lines 290-295 (the `stop_after`/`force` validation)
  execute before line 300 (`final.mkdir(...)`) in the real file, so the
  12b design note ("a bad `stop_after`/`force` must fail before
  `final.mkdir()` or anything else happens") is accurate to the current
  code, not an assumption.
- **`run_lrgb` span and phase-boundary line numbers** (195-1038; masters
  ends at the `stop_after == "masters"` check at 812-818; reconciled's
  checkpoint-emitting body ends at 969 with the early-return at 971-977;
  final runs 979-1038) are all exactly correct against the real file.

---

## Bottom line

One real, material gap (Finding 1) that would break Step 12a's own stated
success criterion ("full suite green") if executed literally as written —
loudly and immediately, not silently, but still a planning defect that
should be fixed in the document before 12a starts, since discovering it
by trial-and-error during execution defeats the purpose of writing this
sub-step plan out in advance. Recommend Section D be rewritten so that
every name `run_lrgb` uses directly (essentially the entire current "stays"
list, minus `Checkpoint` the type and `build_single_filter_master`) is
explicitly marked as needing its own import statement in **both**
`pipeline.py` and `lrgb_orchestrator.py`, matching the treatment
`scan_session`/`_delete_if_exists` already correctly get in Section C.3.
Findings 2 and 3 are real but do not change any recommendation or risk the
document already correctly reaches. No finding here contradicts the
document's central, already-verified-by-this-review claim that
`rgb_reconciled` is a genuine, previously-missed cross-phase `self.`
attribute.
