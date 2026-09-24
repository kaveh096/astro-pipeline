# plan-flats-v4 review, round 7 (fresh eyes, likely final)

Scope of this pass: (1) direct verification of round 6's fix
(`to_dict`/`from_dict` gap), (2) a genuinely fresh top-to-bottom read
weighted toward steps/sections untouched by rounds 3-6 per the Review
log (Step 1a, Step 6, Step 7, Step 8, the §3 Gaps table, §1.1-1.4, §2.2,
§4.0's Event E internals), (3) a proportionality sanity-check against
Kaveh's original ask.

## Round 6 fix verification (task 1)

Read `src/astro_pipeline/run_signature.py` directly (current source has
no `calibration_recipe`/`calibration_header_fallback` field yet — correct,
since "No code has been changed" per the plan's own header).

- `ContributorSignature.to_dict()` is lines 142-150, `from_dict()` is
  153-162 (plan cites `142-149,153-161` — off by exactly one line on both
  ends, consistently omitting the pure closing-brace/paren line; this is
  the same citation convention used throughout the whole document, not a
  new defect).
- Step 2's specified additions — `to_dict()` gains
  `"calibration_recipe": self.calibration_recipe,`; `from_dict()` gains
  `calibration_recipe=d.get("calibration_recipe", ""),` — are syntactically
  correct against the real dict-literal/`cls(...)` shape at those lines.
- `RunSignature.to_dict()` is 194-203, `from_dict()` is 206-215 (plan cites
  `194-201,206-214`, same one-line convention as above). Step 5's
  specified additions for `calibration_header_fallback` are likewise
  correct against the real shape.
- The round-trip tests specified in Step 2 (non-empty
  `"flat:v2:dedup+uniq+mul"` through `to_dict`/`from_dict`, asserting the
  reconstructed value equals the original) and Step 5 (`True` through the
  same round-trip) both use a **non-default** value on the field they
  exercise, so a `to_dict`/`from_dict` that omits the field (reverting to
  round 6's actual bug) would make either test fail. Confirmed these are
  the missing-half tests they claim to be: the existing "old JSON without
  the key defaults to X" tests only exercise the load side of a
  *missing* key, not a present, non-default value surviving a full
  round-trip.

**Round 6's fix is correctly and completely specified in rev 7.** No
further action needed on it.

## Fresh full-document findings

### Finding 1 (major): Event E's "free" T24 pixel-identity check does not
account for I6, one of E's own three listed triggers

§4.0's Event E lists three triggers, batched into one regeneration,
explicitly including:

> - I6 (only if Q7 = change; the no-code measurement in Step 3 answers
>   this before E, whichever way it comes out).

I6 (bias/dark master stacked with `-nonorm` instead of Siril's default
"additive with scale") is a **global** code change to `build_master`
(`calibration.py:451-486`), not per-telescope — it would change every
bias/dark master's pixels, including T24's. The plan's own Gaps-table
entry for I6 says exactly this: "These masters feed M51's **delivered**
image: the selected T24 Luminance and both colour contributors."

Yet Event E's procedure step 6 — the "Free pixel-identity check (R3-2)" —
says:

> They are expected to be pixel-identical, because the staged T24 file
> sets are byte-identical before and after the deletion (§1.5). A
> mismatch here means something **other** than the deletion changed T24's
> pixels, and **must be investigated before proceeding.**

This reasoning only accounts for the deletion trigger. If Q7 is answered
"change" (I6 adopted), I6 is explicitly batched into the *same* E run
(procedure step 3, "Apply the triggers: code merged, folders deleted" —
I6's code change would be merged here too), so T24's rebuilt masters
would legitimately, deliberately differ from the Step-1 snapshot — for a
reason the plan itself already knows and has already measured (I6's own
no-code measurement in Step 3, run before E specifically so "Q7 can be
answered with numbers before E runs"). As written, step 6 would flag this
expected difference as a fault requiring investigation, with no branch
for "unless I6 was adopted, in which case T24 is expected to move too, by
about the amount I6's own measurement already reported." Falling back to
"the Step-1b determinism bound" (the other escape clause in that
paragraph) doesn't rescue this either — I6's whole reason for existing is
that its effect can be larger than Siril's own run-to-run noise; that is
the number Q7 is deciding on.

Practical consequence: whoever runs E, if Q7 = change, hits a self-authored
false alarm at the one point in this plan explicitly designed as a
stop-and-check safety gate on real, once-only data regeneration — at best
wasted investigation time re-deriving a number the plan already has, at
worst a decision to distrust or roll back a deliberate, wanted change
because the runbook told them a T24 pixel change means something broke.

**Fix**: make step 6's expectation conditional — "pixel-identical if I6
was not part of this run of E; if I6 was adopted (Q7 = change), expect a
difference of the magnitude already recorded by the Step-3 measurement,
and treat anything beyond *that* magnitude (plus the Step-1b determinism
bound) as the actual fault signal." This preserves the check's value
instead of retiring it the one time it would fire for a real reason.

### Finding 2 (minor): G14's row contradicts the "Deliberately out of
scope" section it sits three pages above

§3's Gaps table:

> G14 | LOW/defer | BIN1→BIN2 flat binning; `Master_Flat*` use; dark-flats
> | code + data | — | **Deferred to §5.**

§4's "Deliberately out of scope (not deferred to §5 — genuinely not
planned)" list — whose own heading explicitly contrasts itself with
deferral:

> - `Master_Flat*`/`Master_Bias*` use.
> - BIN1→BIN2 flat binning.
> - Cross-telescope RGB.
> - T24 flats (binding).
> - G15 temperature check.
> - I4 (existing over-invalidation behaviour).

Two of G14's three items (`Master_Flat*`/`Master_Bias*` use, BIN1→BIN2
binning) are listed **verbatim** in the "genuinely not planned" list, with
the opposite disposition from G14's own "Deferred to §5" label. Checked
§5 (Future work) directly: neither item appears anywhere in it (its five
bullets are OSC flats, flat↔light geometry, narrowband/boost
invalidation, dark-current/dark-flat measurement, and the local-vs-
iTelescope statistical measurement). So "Deferred to §5" is simply false
for those two sub-items — the "Deliberately out of scope" list is what
actually happened. G14's third item, "dark-flats," *is* correctly covered
by §5's "Dark-current / dark-flat-necessity measurement" bullet, so only
two of the row's three clauses are wrong, not all three.

This is a leftover from some earlier revision (G14 was presumably once
entirely §5-bound and later partly reclassified as genuinely out of
scope, without the Gaps-table row being edited to match) — it's real
enough to be worth catching, since Step 8's own stated principle is
"state the cuts plainly... so a reader does not assume more happened than
did," and this is exactly a spot where a reader consulting §3 alone would
wrongly conclude BIN1→BIN2 binning and `Master_Flat*` use are on a future
roadmap, when the document's own more-authoritative section says they are
not.

**Fix**: split G14 into two rows, or edit its "Deferred to §5" cell to
read "dark-flats: deferred to §5; BIN1→BIN2 binning and `Master_Flat*`
use: genuinely out of scope, see §4's list" — or simply delete the
"Deferred to §5" claim for the two mismatched items.

## What else was checked and found clean

Fresh, line-level verification against current source (not re-trusting
prior rounds' citations) of a deliberately wide, previously-lighter-touch
sample: `ingest.py`'s `_FLAT_RE_SKYFLAT`/`classify_frame` early-return
(505-513, exact), `calibration_policy.py`'s `infer_flat_policy`/
`infer_calibration_mode` (confirmed: telescope-only, no binning argument,
matches G5's claim), `calibration.py`'s `select_dark` (124-188, exact),
`_check_calibration_log` (196-222, confirmed it checks only `NOT USING
DARK`/negative-pixels, not FLAT/OFFSET, and has exactly one call site at
line 720 — `build_master_flat` at line 489 never calls it, confirming
G3), `build_master_flat`'s stack command (line 550, confirmed no
`-norm=mul`), `master_builder.py`'s OSC `KeyError` line (176, exact) and
flat-logging lines (206, 219-224, exact), `pyproject.toml` (confirmed
exactly `testpaths = ["tests"]`, no `addopts`/`markers`, matching the
plan's claim precisely), and `tests/test_pipeline.py` (confirmed exactly
four `_run_m51()` call sites / `@requires(FINAL_DIR / "lrgb_final.fit")`
decorators — matching the plan's "four real-M51-pipeline tests" claim
exactly, by name and count, not by trust) and its plain-string
`calibration_index()` fakes (lines 448-450 and 1469-1475, confirmed
verbatim). Also spot-checked `skill/SKILL.md:17` and `:160-164` against
current text — both citations and the plan's "false today" claims about
T68 being PRECALIBRATED (not RAW_LOCAL) checked out.

No discrepancy found in any of the above; every citation was either exact
or off by the same harmless one-line convention noted above, consistently
applied throughout the whole document.

## Proportionality (task 3)

Kaveh's original ask was narrow: "figure out the missing flat-related
capability and do a plan for building the gaps." After 7 rounds of
scope-cutting, the core plan (Steps 0, 1a, 1b, 2, 3a/3b, 4a/4b, 5, 6, 7,
8) is: fix the one real flat defect (T21, behind a Kaveh gate), detect
Siril's silent calibration failures, let ingest *see* calibration data at
three other telescopes without changing any of their behavior by default,
expose the existing Python-only override via CLI, and update docs/notes
to match. OSC, narrowband invalidation, the geometry backstop, and the
statistical local-vs-iTelescope measurement are all in §5, explicitly not
built here. This is proportionate to what was actually found (15 real
gaps, 6 incidental findings, one real live defect) — it is not a rebuild
of the calibration subsystem, and every step still maps to a concrete,
evidenced gap rather than speculative hardening. No change recommended
here.

## Verdict

Two findings, neither cosmetic: one major (Event E's own pixel-identity
oracle contradicts one of E's own listed triggers), one minor (a Gaps-table
row contradicts the document's later, more authoritative
"deliberately out of scope" section for two of its three items).

**VERDICT: REVISE**
