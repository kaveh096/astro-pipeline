# plan-flats-v4 review, round 5 (fresh eyes)

Reviewed `docs/plan-flats-v4.md` rev 5, after independently verifying round 4's
fixes against source and re-reading the whole document top to bottom. Two
non-cosmetic findings survived four rounds of review; both are currently
behaviourally inert on real data, which is likely why they were not caught
earlier, but both are genuine gaps in the plan's own stated completeness
claims. Round-4's own fixes (R4-1 through R4-6) were re-verified directly
against source and are all correct as described — see "Round-4 fixes
re-verified" below.

## Findings

### F1 (major): Step 2's "both real call sites" claim is false — a third `contributor_stale` call site exists and is silently left out

**Evidence.** `grep -rn "contributor_stale(" src/` finds exactly three real
call sites in `lrgb_orchestrator.py`, not two:

- line 363 — the Luminance loop, passes `lum_flat_frame_hash` (computed via
  `_flat_frame_hash`).
- line 543 — the mono-RGB colour loop, passes `colour_flat_frame_hash`
  (computed via `_colour_contributor_flat_frame_hash`). Step 2 names this one
  and the Luminance loop as "**Both** real call sites."
- **line 618 — the OSC colour loop** (RGB-only/OSC plan): `
  self.old_signature.contributor_stale("colour", contributor_key, frame_hash,
  self.pedestal, "")` — a literal `""` for `flat_frame_hash`, not routed
  through `_flat_frame_hash` or Step 2's new `calibration_recipe_parts`
  helper at all. Step 2 never mentions this line.

Step 2's text is explicit and absolute: "Both real call sites... pass the
freshly-computed `calibration_recipe` value explicitly, exactly as they
already do for `flat_frame_hash` — a defaulted parameter alone would compile
and pass every existing test while never actually invalidating anything on a
real recipe bump." That is exactly the failure mode line 618 is left in: it
will call `contributor_stale` with the new `calibration_recipe` parameter
left at its keyword default (`""`), never wired through, after Step 2 ships.

**Why this doesn't break anything today.** `colour_flat_frame_hashes[key] =
""` at line 614 is hardcoded because "OSC never consults flats" (line-614
comment) — `build_group_master` for OSC always passes `flat_frames=[]`. Per
Step 2's own spec, `calibration_recipe_parts(mode, flats=[])` returns `("",
"")` for *any* mode when the flat set is empty, and separately `("", "")` for
PRECALIBRATED regardless of flats. Every real OSC contributor today is
PRECALIBRATED (`contributor_staleness.py`'s own docstring: "every real OSC
contributor is PRECALIBRATED"), and RAW_LOCAL OSC is structurally unreachable
today (`build_osc`'s `cal_index={}` → `KeyError`, G7/I1, deferred to §5). So
the freshly-computed `calibration_recipe` for this call site, if it were
computed, would also be `""`, matching the untouched default — currently
inert, by the same logic the plan already uses to declare G10's colour-hash
bug and R4-4's narrowband inertness "verified inert." The difference is that
those two are *documented* as inert; this one isn't mentioned anywhere.

**Why it still matters.** (1) The plan's own claim of exhaustive call-site
coverage is factually wrong — there are three real call sites, not two,
and this round found it only by grepping independently rather than trusting
the plan's count. (2) It leaves a latent trap for `§5`'s own deferred OSC
work: the day someone lifts the `build_osc` `KeyError` and RAW_LOCAL OSC
becomes reachable with a real matched flat set, this call site needs
`calibration_recipe` wired through exactly like the other two, or a future
flat-recipe bump (e.g. `-cfa`/`-equalize_cfa`, listed as future OSC work in
§5) will silently fail to invalidate an OSC contributor's master, which is
precisely the bug class Step 2 exists to prevent. Nothing in the core plan or
§5 flags this dependency, so whoever picks up §5's OSC work later has no
signal that this line needs touching too.

**Fix.** In Step 2, replace "Both real call sites (the Luminance loop... and
the colour loop...)" with an accurate enumeration of all three, and add the
same "verified inert" treatment already given to G10/R4-4: state that the
OSC call site (`lrgb_orchestrator.py:618`) is left at `contributor_stale`'s
default `calibration_recipe=""` deliberately, because `calibration_recipe_
parts` on an empty flat set (OSC's permanent, structural state) always
returns `("", "")` regardless of mode, and because RAW_LOCAL OSC is
unreachable while G7/I1 stand (§5) — then add one line to §5's OSC bullet
noting that lifting the `KeyError` also requires wiring `calibration_recipe`
into this call site, not just fixing `cal_index`/`-cfa`.

### F2 (major): the real-data test protocol is enforced by memory, not by config — one missed `-m` flag prematurely triggers exactly the regeneration event E exists to control

**Evidence.** §4.0's "Real-data suite protocol" requires every step from
Step 0 through the step that triggers E to run `pytest -m "not
m51_pipeline"` — a flag that must be typed correctly on (at minimum) nine
separate step invocations (0, 1a, 1b, 2, 3a, 3b, 4a, 4b, 5). `pyproject.toml`
has no `addopts` and no `markers` registration:

```
[tool.pytest.ini_options]
testpaths = ["tests"]
```

A bare `pytest` (no `-m`) at any point between Step 0 (deletion happens
immediately) and event E would run the four real `_run_m51()` tests
against live Siril, on the real, now-post-deletion M51 tree, under
whatever partial code state that step left behind — exactly the
"regenerates M51 before the batched, snapshotted, Kaveh-gated event E"
outcome §4.0 is built to prevent. Event E's own snapshot/backup procedure
(§4.0 step 1) does not exist yet at any point before E is deliberately
triggered, so there is no plan-provided safety net if this happens by
accident — only whatever backup Kaveh has outside this plan.

**Why four rounds might have missed it.** The marker mechanism itself
(R2-15/R3-1's origin) is sound; what's missing is *enforcement*. Every
prior round checked that the marker exists and is applied to the right
tests, not whether a person could still trivially skip the `-m` flag by
habit (e.g. running plain `pytest` out of muscle memory, or an IDE's
"run tests" button that doesn't carry custom CLI args).

**Fix.** Make the exclusion the default via config, not memory: in Step 0,
add `markers = ["m51_pipeline: real Siril run against the live M51
project; excluded by default, see docs/plan-flats-v4.md §4.0"]` and
`addopts = ["-m", "not m51_pipeline"]` to `pyproject.toml`'s
`[tool.pytest.ini_options]`. A plain `pytest` then safely excludes the four
tests by construction from Step 0 through Step 5; event E explicitly
overrides with `pytest -m m51_pipeline` (or `-o addopts=""`) for its own
one-time run, and the `addopts` line is removed (not just the marker) as
part of event E's own follow-up commit, so normal runs resume covering
those tests afterward. This is a one-line addition to Step 0 that removes
an entire class of "forgot the flag" risk for the riskiest window in the
whole plan.

## Round-4 fixes re-verified

- **§6→§5 cross-references**: re-grepped `docs/plan-flats-v4.md` for `§6`
  myself — the only hit left is inside the Round 4 review-log entry
  describing the fix itself. Zero stray `§6` references remain in the live
  plan body. R4-2's fix is complete and correct.
- **`contributor_stale`'s `calibration_recipe` line**: read
  `run_signature.py:217-255` directly. Today's `return` line is exactly
  `existing.frame_hash != frame_hash or existing.flat_frame_hash !=
  flat_frame_hash`, with no third field, and the docstring genuinely does
  contain "the one place a keyword-default is NOT enough on its own"
  (line 247-248) — the plan's Step 2 quote of this is accurate, not
  invented. `tests/test_run_signature.py` genuinely has
  `test_contributor_stale_true_on_flat_frame_hash_change_direct_call`
  (line 215) and `test_contributor_stale_defaults_flat_frame_hash_to_empty_
  string` (line 232) at the cited locations, confirming the "mirroring"
  test-naming claim in Step 2 is checkable, not aspirational. R4-1's fix is
  specified correctly (aside from F1 above, which is a separate, newly
  found gap in the same step's call-site enumeration).
- **Three CLI flag shapes (R4-3)**: verified against source directly.
  `run_lrgb`/`LRGBOrchestrator.__init__` (`lrgb_orchestrator.py:209,1029`)
  take `calibration_mode: dict[str, CalibrationMode] | None` — dict form,
  matches `run_stage.py`'s planned repeatable `TEL=value` flag.
  `run_narrowband` (`narrowband_orchestrator.py:51`) and
  `build_single_filter_master` (`master_builder.py:328`) both take a single
  `calibration_mode: CalibrationMode | None` — matches the planned plain
  `--calibration-mode raw_local|precalibrated` for both
  `run_narrowband.py` and `run_narrowband_boost.py`. Confirmed
  `run_narrowband.py`'s CLI already pins exactly one `--telescope`
  (required, single value), so a per-telescope dict form genuinely has
  nothing to key on, as the plan argues. Confirmed
  `run_narrowband_boost.py`'s `main()` calls `build_single_filter_master`
  today **without** passing `calibration_mode` at all (it always falls
  through to `infer_calibration_mode` inside that function) — matches the
  plan's claim exactly. All three shapes are internally consistent with
  each other and with Step 5's own signature-recording text.

## Fresh top-to-bottom findings (beyond F1/F2)

Nothing else rose above cosmetic. Specific checks that came back clean:

- Every step is still one commit (3b is explicitly conditional and
  self-contained; 6a/6b are small enough to be one commit as scoped;
  nothing in the current text implies a step secretly needs two).
- Dependency order Step 0 → 1a/1b → 2 → 3a/3b → 4a → 4b → 5 → 6 → 7 → 8 is
  consistent end to end; Step 5's CLI genuinely needs Step 4b's mode
  decoupling and Step 2's helper, both of which land first in the text.
- All ~11 open questions are genuine judgement calls (scope, risk
  tolerance, priority, timeline) that no amount of code-reading answers —
  none of them smuggle in a fact that's actually already knowable from the
  repo.
- Spot-checked the M51 root folder name directly
  (`D:\Raw Photo Backups\iTelescope\M51 - Whirlpool galaxy - T24 & T21 -
  Jan 2025`) — it genuinely contains both `T24` and `T21` tokens, confirming
  Step 4b's "multi-token root → unrecognized" rule has a real trigger, as
  claimed.
- Surveyed every other real project folder under
  `D:\Raw Photo Backups\iTelescope\` (M16, M17-20Inch, M17-T26, NGC 2237,
  NGC 55, NGC 7293, M78, Comet Leonard) that §2.1's table doesn't itemize —
  confirmed each is genuinely "no parseable raw lights" (differently-named
  conventions, no flat/bias/dark tokens, no T-number folders), so the "out
  of scope" bucket is not silently hiding an eighth real flat-bearing
  project the plan forgot to inventory.
- §5's cut is consistent in the direction the prompt asked about
  (core plan doesn't secretly lean on anything deferred to §5) — the only
  dependency issue found runs the *other* direction (F1: §5's future OSC
  work will depend on a core-plan call site that the core plan doesn't
  fully wire), which is why it's called out explicitly above rather than
  assumed away.

## Verdict

Two major findings (F1, F2), both real and both missed by four prior
rounds, both currently behaviourally inert but requiring a documented fix
before the plan can be called complete. Not cosmetic-only.

VERDICT: REVISE
