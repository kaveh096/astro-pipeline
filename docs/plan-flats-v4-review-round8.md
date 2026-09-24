# plan-flats-v4 — Round 8 adversarial review (fresh eyes, likely final)

Reviewed `docs/plan-flats-v4.md` rev 8 against `main` @ `22cdf19` (confirmed
current `HEAD` via `git rev-parse`). No plan text, code, or data was
edited. Findings only.

## 1. Direct re-verification of round 7's two claimed fixes

**Round 7 finding 1 (major — Event E step 6 had no branch for I6).**
Read `docs/plan-flats-v4.md` lines 452–480 directly. Procedure step 6 now
reads:

> "**with an explicit branch for whether I6 rode along on this run of E**"

and gives two mutually exclusive cases:

- I6 **not** part of this run of E → masters expected pixel-identical to
  the Step-1 snapshot; a mismatch is a fault to investigate.
- I6 **was** part of this run of E → T24's bias/dark masters and
  everything built from them are *expected* to differ, by the magnitude
  the Step-3 I6 no-code measurement already recorded, plus the Step-1b
  determinism bound as slack; only a difference beyond that combined
  envelope is flagged.

This is a correct, complete fix: it covers both truth values of "was I6
merged at procedure step 3", references a number that is actually
produced earlier in the plan (the Step-3 I6 measurement, itself scheduled
specifically so this number exists before E runs), and does not
re-introduce a false-alarm risk on the first real use of Q7 = change.
**Verified correct.**

**Round 7 finding 2 (minor — G14 contradicted the out-of-scope list).**
Read the G14 row (line 304) and the "Deliberately out of scope" list
(lines 1054–1061), and separately re-read all five §5 Future-work bullets
(lines 1065–1123) to confirm neither BIN1→BIN2 binning nor
`Master_Flat*`/`Master_Bias*` use appears there. Current state:

- G14: dark-flats **only** → deferred to §5 (matches §5's "Dark-current /
  dark-flat-necessity measurement" bullet, which is the only §5 item that
  could plausibly cover it).
- G14: BIN1→BIN2 flat binning and `Master_Flat*` use → "genuinely out of
  scope... see the 'Deliberately out of scope' list", and both terms
  literally appear in that list, verbatim.

No remaining contradiction. **Verified correct.**

## 2. Fresh cross-cutting pass (this round's actual mandate)

Per the task brief, since every section has now been touched by at least
one of rounds 1–7, this round targeted whether the accumulated fixes still
*compose* — checked by reading the real, currently-unmodified source
(the plan's own stated "before" state) side by side with the plan's
description of it, rather than re-reading the plan in isolation:

- **`run_signature.py`** (`ContributorSignature`/`RunSignature`,
  `to_dict`/`from_dict`, `contributor_stale`): read in full
  (lines 90–331). Every claim Step 2 makes about the *current* code —
  hand-written field-by-field `to_dict`/`from_dict` at lines 142–161 and
  194–215, `contributor_stale`'s exact current `return` line at line 255,
  its own docstring already warning that a keyword-default alone isn't
  enough (lines 240–248) — matches the live file exactly, including line
  numbers. Step 2's planned edits (add `calibration_recipe` to both
  dataclasses' `to_dict`/`from_dict`, change the `return` line, not just
  the signature) are therefore grounded in the actual code, not a stale
  read of it.
- **`lrgb_orchestrator.py:614-618`** (the OSC colour loop's
  `contributor_stale` call): read directly. Confirmed it passes a literal
  `""` positionally for `flat_frame_hash` today, exactly as Step 2/§5
  describe, and that leaving this call site untouched after Step 2 adds a
  6th `calibration_recipe=""` parameter is correctly inert (an unpassed
  parameter stays at its default) — consistent with the plan's claim that
  this is deliberate, not an oversight.
- **`calibration_policy.py`**: read in full. `infer_flat_policy` and
  `infer_calibration_mode` match the plan's §1.1/Step 4b description
  exactly, including the "no calibrated lights → falls through to
  RAW_LOCAL and fails loudly" branch the plan relies on for its "no
  persisted hash changes on any of the seven gate-(D) projects" claim
  (Step 5's hash contract) — traced this specifically for M31/T05, which
  has no calibrated-lights entry in §2.1's table, and confirmed the
  fallback-flag-on path still leaves its mode computation unchanged
  because header-sourced frames are excluded from the `has_bias`/
  `has_dark` count and lights are never header-classified.
- **`ingest.py`/`calibration.py`** line citations (`_FLAT_RE_SKYFLAT`,
  the `UnrecognizedFrame` early-return block, `build_master`/
  `build_master_flat`): spot-checked against source; line numbers line
  up with what's cited in §1.1.
- **Uncommitted dirty-tree diff** (`git diff` on `docs/ROADMAP.md`,
  `tests/conftest.py`, `tests/local_paths.py.example`): read in full.
  Confirmed it is exactly the `DESKTOP_DIR`→`PROJECT_DIR_BASE` rename the
  plan's front matter describes, plus a ROADMAP note that Task 5's Step
  17 (an unrelated, already-completed real-data verification, dated
  2026-09-21) passed against the real, still-duplicated M51 tree before
  this plan's own base commit — consistent with, and not in conflict
  with, the plan's own hash math in §1.5 (which assumes the pre-deletion
  tree matches the persisted signature).
- **`tests/conftest.py`** fixture list: confirmed directly (`grep`) that
  only `PROJECT_DIR` (M51), `NGC3628_PROJECT_DIR`, `ABELL6_PROJECT_DIR`
  and `IC1396_PROJECT_DIR` exist today — no Abell 31, M42 or M31 fixture —
  exactly matching Step 1a's stated gap ("Add fixtures for Abell 31, M42
  and M31... like IC 1396").
- **Current `HEAD`**: `git rev-parse HEAD` returns `22cdf19...`, matching
  the plan's stated base commit exactly.

No inconsistency, seam, or stale citation turned up anywhere in this pass.

## 3. Verdict

Nothing rose above cosmetic. What was checked to be confident: both of
round 7's fixes (re-verified against the plan's own current text, not
just trusted from the "Review log" self-report); the actual current state
of every source file the plan's central invalidation mechanism (Step 2)
depends on, confirming the plan's "before" description is accurate and
its planned edits are therefore well-founded; the one call site (OSC,
line 618) that the plan deliberately leaves untouched; the calibration-mode
inference logic behind Step 5's hash-contract claim, traced through the one
gate-(D) project (M31) with the least data behind it; and the real,
currently-uncommitted dirty-tree diff, which matches the plan's front
matter with no surprises.

No new findings this round.

**VERDICT: CONVERGED**
