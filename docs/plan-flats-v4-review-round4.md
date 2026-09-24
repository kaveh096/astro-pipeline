# plan-flats-v4: adversarial review, round 4

Reviewer scope: `docs/plan-flats-v4.md` rev 4, checked against `main` @
`22cdf19`, the round-1/2/3 reviews and the review log, and the read-only
data under `D:\Raw Photo Backups\iTelescope\`.

- **Not run**: Siril, GraXpert, the pipeline, `scan_session` as a subprocess
  test, or the full test suite.
- **Evidence**: direct execution of this repo's own `ingest.scan_session` /
  `IngestReport.instrument_groups()` / `IngestReport.flat_index()` /
  `run_signature.frame_identity_hash()` against the real M51 tree (still
  undeleted — see below), source reads of `calibration.py`,
  `calibration_policy.py`, `run_signature.py`, `contributor_staleness.py`,
  `lrgb_orchestrator.py`, `narrowband_orchestrator.py`, `master_builder.py`,
  `skill/run_narrowband.py`, `skill/run_narrowband_boost.py`,
  `tests/test_pipeline.py`, `tests/test_run_signature.py`, and the persisted
  `run_signature.json`.

**Pre-check**: the two duplicate M51 folders (`Uncalibrated Lights - Jan
2025/`, `calibrated Lights - T24 - Feb 2025/`) **still exist on disk**.
Step 0's deletion has not happened. The plan's present-tense claims about
the duplicated tree are therefore accurate as current state, not stale
history.

**Checked and correct (focus area 1 — independently re-verified from
scratch, not from the plan's or round 3's numbers)**:
- `scan_session` + `instrument_groups()` on the real tree today: T24 raw
  total = 190, T21 raw total = 4, T24 Luminance BIN1 = 42. Matches the
  plan's §1.5/§1a claims exactly.
- `frame_identity_hash` of the 42 current T24 L BIN1 basenames =
  `d2f430e6232fd176`; of the 21 unique names (post-dedup) =
  `de55083822298fad`. The first string is present in the persisted
  `run_signature.json`; the second is not. Matches the plan's table.
- T21 L BIN1: 4 names (`[a,b,a,b]`) → `83bc8bee12b82922` (persisted,
  present in the JSON); 2 unique names → `c2b76b333110b4a6` (not
  persisted). Matches.
- T21 L BIN1 flat matching: `flat_index()[("T21",1,"Luminance")]` has 30
  entries, 20 unique basenames, `frame_identity_hash` over all 30 =
  `349469062e57762c`. Matches the plan's Step 1a invariant and §2.3
  exactly.
- The `n >= 10` quality-filter-threshold claim (42→21, 28→14, 24→12 all
  stay ≥ 10 both sides) checks out against the real per-group counts
  (`instrument_groups()` gives Red BIN1=28, Green BIN1=24, Blue BIN1=24,
  Red/Green/Blue BIN2=24 each on the duplicated tree — all even multiples
  of the plan's claimed unique counts).
- `_check_calibration_log` (`calibration.py:196-222`) today already uses a
  wildcard/prefix regex for DARK (`NOT USING DARK:.*`), not an enumerated
  list — consistent with the plan's fix being "add FLAT/OFFSET prefix
  regexes," not "convert DARK from enumerated to prefix." It is called
  only inside the light-calibration path (line 720), never inside
  `build_master_flat` (starts line 489) — confirmed, matches G3.
- `infer_calibration_mode` (`calibration_policy.py:66-73`) today reads only
  `calibration_index()` **keys**, never frame values — confirmed this is
  why Step 4b's `source`-filtering requires the `getattr(f, "source",
  "filename")` fallback (R3-8's fix), and confirmed `test_pipeline.py`'s
  `_FakeCalReport` fakes really do use plain-string list values
  (`{("T24","Bias",1,0.0): ["b"]}`).
- No leftover references to the dropped "prefer iTelescope" option or the
  old `flat_recipe` field name anywhere in the current plan body (only in
  the Review-log table, which is historical and correctly labelled as
  such).

---

## Findings

### R4-1 (major): Step 2's `calibration_recipe` field is "fed to `contributor_stale`" but the method's comparison logic is never told to change

- **Evidence**: `RunSignature.contributor_stale` (`run_signature.py:214-249`)
  currently returns `existing.frame_hash != frame_hash or
  existing.flat_frame_hash != flat_frame_hash`. Step 2's own change list
  says only: "`ContributorSignature` gains `calibration_recipe: str = ""`,
  fed to `contributor_stale` at both call sites, alongside the existing
  `flat_frame_hash` parameter." It never says the `return` line itself
  gains `or existing.calibration_recipe != calibration_recipe`.
  - This is not a pedantic nit: the existing `flat_frame_hash`
    docstring in this exact file states, almost word for word, that this
    is "the one place a keyword-default is NOT enough on its own" and
    that BOTH call sites must pass the freshly-computed value explicitly
    "or this parameter's default silently means the comparison below can
    never actually fire on a genuine flat-set change" — i.e. this
    codebase has already been bitten by precisely this failure mode once
    (plan-flats-v3, round 2) for `flat_frame_hash`, and the plan is about
    to reintroduce the same ambiguity for `calibration_recipe`.
  - Consequence if under-specified this way and the implementer only
    threads the new field through call sites without also updating the
    boolean expression: Step 3b's recipe bump
    (`("", "flat:v2:dedup+uniq+mul")`) — described as "the **single**
    trigger for the T21_bin1 rebuild" — silently does nothing.
    `contributor_stale` would keep returning `False` for T21_bin1
    (frame_hash and flat_frame_hash both unchanged by 3b alone), the
    stale master is never deleted, and Kaveh's actual T21 flat/
    normalisation fix from Q1 never takes effect on disk, with no error,
    no test failure, and no visible symptom until someone notices the
    pixels haven't changed.
  - Corroborating gap: `tests/test_run_signature.py:215-229` has
    `test_contributor_stale_true_on_flat_frame_hash_change_direct_call`,
    the exact test pattern this mechanism needs for the new field. Step
    2's own "Tests" list has no analogous
    "`contributor_stale` is True when only `calibration_recipe` changes
    (`frame_hash`/`flat_frame_hash` unchanged)" test, and Step 3b's Tests
    list doesn't have one either — it only tests that
    `calibration_recipe()` (the pure function) returns the bumped tuple,
    never that the bump actually invalidates anything.
- **Fix**: add to Step 2's Change list: "`contributor_stale` gains a
  `calibration_recipe: str = ""` parameter and its `return` expression
  becomes `existing.frame_hash != frame_hash or existing.flat_frame_hash
  != flat_frame_hash or existing.calibration_recipe != calibration_recipe`."
  Add the missing direct-call test mirroring
  `test_contributor_stale_true_on_flat_frame_hash_change_direct_call`, and
  add an end-to-end test in Step 3b asserting that bumping the recipe
  string alone (same frame/flat hashes) makes T21_bin1 stale.

### R4-2 (major): pervasive `§6` vs `§5` cross-reference bug — "Future work" is §5, but ~15 in-body references throughout §0–§4 point to §6 ("Open questions for Kaveh") instead

- **Evidence**: the document's actual section headings are `## 5. Future
  work` (line 838) and `## 6. Open questions for Kaveh` (line 890). Every
  reference to deferred work **inside §0 TL;DR, §1.2, §2.1, §2.2, §3
  (Gaps table), and §4 (Plan)** says `§6`, not `§5`:
  - Line 29, 65, 83: "in **Future work** (§6)".
  - Line 116: OSC "Deferred (§6)".
  - Line 240, 246: T68 geometry mismatch / compatibility checks "(§4.0/§6)"
    / "(§6)".
  - Line 297, 298, 301, 304: gaps table, G7/G8/G11/G14 all "**Deferred to
    §6**".
  - Line 310, 314: I1/I2 "Deferred to §6" / "part of §6's OSC work".
  - Line 525, 681, 692: Step 2 and Step 4b, "later I6/OSC work in §6",
    "a future decision (§6)", "(I1; §6)".
  - Line 795: "moved to Future work (§6)".
  - Line 827: the section heading itself, **"Deliberately out of scope
    (not deferred to §6 — genuinely not planned)"** — this heading exists
    specifically to distinguish "genuinely not planned" from "deferred to
    Future work," so its own self-referential pointer being wrong is the
    clearest symptom of the bug.

  Only inside **§6 itself** (Open questions, lines 907, 922, 935, 939) do
  the self-references correctly say "Future work (§5)" — i.e. exactly the
  text written *after* the section was renumbered was fixed, and
  everything written *before* it (the bulk of the document, likely
  produced by search-and-replace or copy-forward during round 3's R3-14
  restructuring, which inserted "Future work" as a new section between
  the old Plan and the old Open Questions) was never updated.
  - Net effect: a reader following "G7... Deferred to §6" (line 297) lands
    on "Open questions for Kaveh," not on §5's actual "OSC flats (old
    G7/I1/I2)" writeup. Same for G8, G11, G14, and every OSC/I1/I2
    reference in Steps 2 and 4b.
- **Severity**: major, not cosmetic — it is wrong in ~15 places
  throughout the document body (not one typo), it breaks the document's
  own internal navigation for exactly the deferred-scope claims this
  round's focus area 6/7 asks about, and it undermines confidence that
  the R3-14 renumbering pass was actually completed rather than partially
  applied.
- **Fix**: global find-and-replace `§6` → `§5` everywhere outside the
  Review-log table and outside §6 itself (which correctly already says
  §5 when referring to Future work). Re-grep for `§6` after the fix and
  confirm every remaining hit is either inside §6's own text or refers to
  a different document's section (e.g. "ROADMAP §5" is a different doc
  and is unaffected).

### R4-3 (major): the `--calibration-mode` CLI flag's shape doesn't match `run_narrowband`'s parameter type, and the plan's own test list never exercises the mismatch

- **Evidence**:
  - `LRGBOrchestrator.__init__`/`run_lrgb` (`lrgb_orchestrator.py:209,
    1029`) takes `calibration_mode: dict[str, CalibrationMode] | None`
    — a **multi-telescope** override, because one LRGB run can involve a
    different Luminance telescope and colour telescope.
  - `run_narrowband` (`narrowband_orchestrator.py:51`) takes
    `calibration_mode: CalibrationMode | None` — a **single value**,
    because `run_narrowband.py`'s CLI (`skill/run_narrowband.py:26`)
    already requires exactly one `--telescope`.
  - `build_single_filter_master` (`master_builder.py:317-328`, the
    function `run_narrowband_boost.py` calls) also takes a single
    `calibration_mode: CalibrationMode | None`, and
    `skill/run_narrowband_boost.py`'s `main()` does not pass it at all
    today.
  - Step 5 says to add "**`--calibration-mode TEL=raw_local|precalibrated`**
    (repeatable) on `run_stage.py` **and** `run_narrowband.py`" — the same
    `TEL=value` repeatable syntax for both — but only `run_stage.py`
    (backed by `run_lrgb`'s dict) actually wants a `dict[str,
    CalibrationMode]`. `run_narrowband.py` already knows its one
    telescope from `--telescope`; a `TEL=value` flag on it either (a)
    needs to be parsed into a dict and then have exactly the
    `args.telescope` entry pulled out (undefined behaviour if the user
    passes a `TEL=` that doesn't match `--telescope`, or passes none),
    or (b) should just be `--calibration-mode raw_local|precalibrated`
    with no `TEL=` prefix — the plan specifies neither.
  - Step 5's own "Tests" bullet — "`--calibration-mode T20=raw_local`
    parses into the dict `run_lrgb` expects" — only tests the
    `run_stage.py`/`run_lrgb` path. Nothing tests `run_narrowband.py`'s
    parsing at all, so this mismatch would not be caught by the plan's
    own test list.
  - Separately, `run_narrowband_boost.py` is listed as needing
    `calibration_header_fallback` threaded through (Step 5's Change list)
    but gets **no** `--calibration-mode` override at all — so after Step
    4b/5, narrowband boost can see header-recognised flats/darks in the
    interview but has no CLI-reachable way to actually use them under an
    override, unlike `run_stage.py`/`run_narrowband.py`. The plan doesn't
    state this asymmetry as a deliberate scope cut (contrast with how
    carefully OSC's non-reachability is stated in the TL;DR and §5).
- **Fix**: specify the CLI shape per entry point explicitly:
  `run_stage.py` gets the repeatable `TEL=value` dict form (matches
  `run_lrgb`); `run_narrowband.py` gets a plain
  `--calibration-mode raw_local|precalibrated` (no `TEL=`, since the
  telescope is already `--telescope`), mapped directly to the single
  `CalibrationMode` parameter. Add a parser test for
  `run_narrowband.py`'s form specifically. Either add the same flag to
  `run_narrowband_boost.py` (mirroring its `calibration_header_fallback`
  addition) or state explicitly that boost stays override-less in this
  round, as a scope decision, not an oversight.

### R4-4 (minor): Step 3b's flat-stacking/staging change is verified inert for the 7 gate-D projects, but the plan never states it's also inert for narrowband, which shares the same code path

- **Evidence**: `build_master_flat` and `stage_frames` (`calibration.py`)
  are called by `master_builder.py` regardless of whether the caller is
  LRGB, colour, or narrowband (`narrowband_orchestrator.py:118,131-134`,
  `master_builder.py:366-372` for boost). Step 3b's `-norm=mul` and
  dedupe/uniquify staging change therefore applies to **any** RAW_LOCAL
  flat consumer, not just T21 Luminance — but narrowband/boost have "No
  staleness tracking" (§1.2) and are explicitly deferred (old G11, now
  §5). Today this is inert only because no real narrowband project has
  recognised RAW_LOCAL flats (M42/T20's Ha/OIII/SII flats are
  unrecognised pending Step 4b + an explicit override) — the same
  "verified inert on real data today" argument the plan already makes
  explicitly for G10 (colour hash) — but the plan never makes this
  argument for narrowband, even though Step 3b's code change is exactly
  as reachable from that path as from the LRGB path once Step 4b/5 land.
- **Fix**: add one sentence to Step 3b (or the G11/§5 entry) stating that
  the flat-stacking change is inert for narrowband today for the same
  reason it's inert for colour, and flag that the day narrowband gets a
  real recognised RAW_LOCAL flat set (via an explicit override), it will
  silently pick up the new `-norm=mul` behaviour with **no** invalidation
  mechanism to force a rebuild of an already-built narrowband master —
  which is a second, more concrete manifestation of the already-deferred
  G11 gap, worth naming explicitly rather than leaving purely implicit.

### R4-5 (minor): Q4's cross-reference to "Q3-dependent" is loose

- **Evidence**: Q4 ("Migration if the mode ever flips") is tagged
  "(Future work, Q2/Q3-dependent)". Q3 is "do you want the Future-work
  *measurement* (§5) done at all" — migration planning is about what
  happens if Q2 ("Lift") is chosen and a telescope's mode actually flips;
  it doesn't obviously depend on whether the statistical measurement (Q3)
  happens at all. The real dependency looks like Q2 (and arguably Q9,
  "actually reprocess"), not Q3.
- **Fix**: retag Q4 as "(Future work, Q2-dependent)" or explain the Q3
  link if one is intended.

### R4-6 (cosmetic)

- Line 279: "(§ old Step 7, now revised, R3-13)" — the "§" before "old
  Step 7" is stray (steps aren't "§"-numbered elsewhere in this doc;
  only top-level sections are). Drop the "§".
- The `calibration_recipe()` helper (Step 2) and the `calibration_recipe`
  field it feeds share the exact same name, but the helper returns a
  2-tuple `(hash, recipe)` where only the second element is the field's
  own value (the first element continues to populate the *existing*
  `flat_frame_hash` field). Naming the function identically to one half
  of its own return tuple, which lands in a differently-named field for
  the other half, is a little confusing to a future reader; consider
  naming the helper something like `calibration_recipe_parts()` or
  documenting the hash/recipe split more prominently at the call site.

---

## Round-3 fixes: spot-checked, held

- R3-1/R3-2/R3-12: independently re-verified from real data and real code
  in this round (see "Checked and correct" above) — hold, with matching
  numbers computed from scratch rather than trusted.
- R3-3: the split (Step 2 unconditional, Step 3b gated) is structurally
  present and verified inert on real data — but see R4-1: the split's own
  wiring inside `contributor_stale` is under-specified, so R3-3 is only
  **partially** held; the shape is right, the mechanism it enables is not
  fully specified.
- R3-4: "Prefer iTelescope" is confirmed gone from Q2/Step 11's
  replacement text, with no leftover references — holds.
- R3-5: `--calibration-mode` was added to Step 5 — holds structurally, but
  see R4-3: it is under-specified for `run_narrowband.py`'s actual
  parameter shape.
- R3-6/R3-7/R3-8/R3-9: all verified against current source in this round
  (`calibration_policy.py`, `contributor_staleness.py`,
  `ingest.py`/test fakes) — hold.
- R3-13: verified independently that the persisted T21 master flat is a
  32-bit float in [0,1] is consistent with Step 7's revised text — not
  re-measured pixel-by-pixel this round, but no contradicting evidence
  found.

---

## Summary

4 major findings, 2 minor, 2 cosmetic. None of the majors are about the
plan's factual/numerical claims (those independently re-verify as
correct); all four are about specification completeness and document
consistency: (1) Step 2's `calibration_recipe` field is added to
`ContributorSignature` and "fed to" `contributor_stale`, but the method's
actual comparison logic is never told to use it — silently defeating
Step 3b's entire T21 rebuild trigger if implemented literally as
written, with no test to catch it; (2) a systematic `§6`→should-be-`§5`
cross-reference bug spanning roughly 15 locations across §0–§4, including
the "Deliberately out of scope" heading's own self-reference; (3) the new
`--calibration-mode` CLI flag is specified with one repeatable `TEL=value`
shape for both `run_stage.py` (correct, matches `run_lrgb`'s dict) and
`run_narrowband.py` (wrong — that entry point's underlying parameter is a
single `CalibrationMode`, and its CLI already pins one `--telescope`),
with no test covering the mismatch, and `run_narrowband_boost.py` gets no
override flag at all despite gaining the fallback flag; (4) (minor,
folded into the count for completeness) Step 3b's shared-code flat
normalisation/staging change is verified inert for the 7 gate-D LRGB/
colour projects but the plan never makes the same "verified inert"
argument for narrowband, which shares the exact same code path.

VERDICT: REVISE
