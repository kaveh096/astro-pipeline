# Round 6 review of plan-flats-v4.md (fresh eyes, likely final)

**Scope**: (1) directly re-verify round 5's two claimed fixes against
current source, not the builder's self-report; (2) a genuinely fresh
top-to-bottom read of the whole plan, hunting for a structural issue
that five increasingly narrow rounds might have missed by converging on
polishing previously-flagged spots.

---

## Part 1: round 5's two fixes, re-verified directly

### R5-F1 (contributor_stale has 3 real call sites, not 2) — confirmed landed correctly

- `grep -n "contributor_stale(" src/astro_pipeline/lrgb_orchestrator.py`
  gives exactly three hits today: lines 363 (Luminance), 543 (mono-RGB
  colour), 618 (OSC colour) — matches the plan's claim exactly.
- Read line 590-619 directly: the OSC call site
  (`self.old_signature.contributor_stale("colour", contributor_key,
  frame_hash, self.pedestal, "")`) passes a hard-coded `""` for
  `flat_frame_hash`, matching line 614's comment ("OSC never consults
  flats"). The plan's Step 2 text describes this exact call site,
  states it is left at the `calibration_recipe` default deliberately,
  and gives the reasoning (`calibration_recipe_parts` returns `("", "")`
  on an empty flat set and separately on PRECALIBRATED, so the
  freshly-computed value would match the default anyway). This is
  accurate against source. **Confirmed correct, no rehash needed.**

### R5-F2 (pyproject.toml has no addopts/markers backstop) — plan's premise still holds; no regression

- `pyproject.toml`'s `[tool.pytest.ini_options]` is exactly `testpaths =
  ["tests"]` today — no `markers`, no `addopts`. This is expected: the
  plan's own header states "no code has been changed" (it's rev 6 of a
  planning document; Step 0 hasn't executed yet). The plan's §4.0/Step 0
  text describing the `markers`/`addopts` block to be *added* is
  internally consistent and matches what round 5 accepted. **Confirmed:
  nothing to add here, the fix is correctly specified for future
  execution.**

Both round-5 fixes check out against current source exactly as claimed.

---

## Part 2: fresh top-to-bottom finding

### Finding 1 (MAJOR): Step 2 never mentions updating `ContributorSignature.to_dict()`/`from_dict()` for the new `calibration_recipe` field — without it, the field never survives a save/load cycle

**Evidence**:

`ContributorSignature` and `RunSignature` in `src/astro_pipeline/run_signature.py`
are `@dataclass(frozen=True)` with **hand-written** `to_dict()`/`from_dict()`
methods — not `dataclasses.asdict()` — and each one explicitly enumerates
every field by name:

```python
def to_dict(self) -> dict:
    return {
        "key": self.key,
        "stackcnt": self.stackcnt,
        "frame_hash": self.frame_hash,
        "spcc_profile": list(self.spcc_profile) if self.spcc_profile else None,
        "flat_frame_hash": self.flat_frame_hash,
        "calibration_mode": self.calibration_mode,
    }

@classmethod
def from_dict(cls, d: dict) -> ContributorSignature:
    profile = d.get("spcc_profile")
    return cls(
        key=d["key"],
        stackcnt=int(d["stackcnt"]),
        frame_hash=d["frame_hash"],
        spcc_profile=tuple(profile) if profile else None,
        flat_frame_hash=d.get("flat_frame_hash", ""),
        calibration_mode=d.get("calibration_mode", "raw_local"),
    )
```

Adding a new dataclass field (`calibration_recipe: str = ""`) does **not**
automatically make it appear in either method — both are ordinary
Python code that must be edited by hand, one line each, or the field is
silently dropped on every save and silently defaulted on every load.

Step 2's "Change" list says only "`ContributorSignature` gains
`calibration_recipe: str = ""`" and then goes on, in exhaustive detail
(more detail than almost any other paragraph in this plan), to specify
the exact `return`-line change in `contributor_stale`, the exact
call-site wiring for all three sites, and even a `RECIPE_VERSION`
constant placement rule — but **never mentions `to_dict`/`from_dict` at
all**, for either `ContributorSignature` or (in Step 5) `RunSignature`'s
own `calibration_header_fallback` field, which has the identical
hand-rolled-serialization shape.

Step 2's own test list doesn't catch the omission either:
- "An old JSON (no `calibration_recipe` field) loads and reads back
  `''`" only exercises the **missing-key-defaults-to-empty** path
  (`from_dict`'s `d.get(...)` branch) — this is the mirror-image gap
  from the one that actually matters.
- None of the listed tests write a `ContributorSignature` with a
  **non-empty** `calibration_recipe` through `to_dict()` and read it
  back through `from_dict()` to confirm the value round-trips. Every
  other listed test (direct-call `contributor_stale` tests, call-site
  tests) constructs `ContributorSignature` objects directly in memory
  and never touches JSON serialization, so none of them would fail if
  `to_dict`/`from_dict` were never updated.

**Why this matters, concretely**: this is exactly the failure class the
plan is otherwise paranoid about (the `flat_frame_hash` docstring's own
warning, quoted and re-quoted through rounds 2-5, about "the one place a
keyword-default is NOT enough on its own" — R4-1's whole finding was
this same principle applied to `contributor_stale`'s return line). If
`to_dict`/`from_dict` are left untouched:

- After Step 3b ships and `calibration_recipe_parts()` starts returning
  `"flat:v2:dedup+uniq+mul"` for T21's Luminance contributor,
  `contributor_stale` correctly detects the change on the first run
  after the code change and rebuilds — but when the new signature is
  persisted, `to_dict()` omits `calibration_recipe` from the JSON
  entirely. On the *next* run, `from_dict()` loads `calibration_recipe`
  back as `""` (via its own default, or via `.get(..., "")` if only
  that half is added) regardless of what was actually just computed and
  saved. The freshly-computed value (`"flat:v2:..."`) now permanently
  differs from the persisted value (always `""`), so `contributor_stale`
  reports staleness on **every subsequent run, forever** — a full T21
  Luminance master rebuild (real Siril work) on every `run_lrgb`
  invocation from Step 3b onward, not the "single trigger... rebuild"
  Step 3b explicitly promises.
- This is silent: nothing raises, no test written in the plan's own
  list would go red, and gate (D)'s "every persisted real signature's
  hash/recipe pair is unchanged by this step" check (Step 2, real gate
  D) would still pass at the moment it's run, because Step 2 itself
  never changes any real recipe away from `("", "")` — the bug only
  bites once Step 3b's non-empty recipe is actually persisted and
  reloaded, one step later.

**Same gap, lower severity, for Step 5**: `RunSignature` gains a
top-level `calibration_header_fallback: bool = False`, with the same
hand-rolled `to_dict`/`from_dict` shape at the `RunSignature` level.
Step 5 states this field is "deliberately not placed on
`ContributorSignature`... diagnostic," and `diff_invalidation` ignores
it, so an omitted `to_dict`/`from_dict` update here doesn't break
invalidation — but it does mean the field never actually gets recorded
in `run_signature.json` as intended (silently defeating the stated
diagnostic purpose), and Step 5's own "Old JSON loads" test only covers
the missing-key-on-load direction, same blind spot as above.

**Concrete fix**: Step 2 should add an explicit line item: "`to_dict()`
gains `\"calibration_recipe\": self.calibration_recipe`; `from_dict()`
gains `calibration_recipe=d.get(\"calibration_recipe\", \"\")`" — and
add one test that actually round-trips a **non-empty**
`calibration_recipe` through `to_dict()` → JSON → `from_dict()` and
asserts the value survives, mirroring the round-trip coverage the
`flat_frame_hash` field already effectively has via the existing
pipeline-level persisted-signature tests. Step 5 needs the identical
one-line addition and round-trip test for `calibration_header_fallback`
on `RunSignature.to_dict()`/`from_dict()`.

---

## Everything else: no new findings

I re-read every section (TL;DR, §1 current state, §2 real-data inventory,
§3 gaps, §4 plan steps 0 through 8, §5 future work, §6 open questions,
review log) against current source with fresh eyes, specifically
distrusting anything that five rounds of narrowing review might have
polished past rather than re-derived. Spot-checked and confirmed exactly
as described: `infer_calibration_mode`'s no-binning-argument rule
(`calibration_policy.py:66-73`), the Luminance loop's existing
mode-aware flat lookup vs. the colour loop's mode-blind
`_colour_contributor_flat_frame_hash` call (`lrgb_orchestrator.py:337-348`
vs. `:527`), `calibration_index()`/`flat_index()`'s key-only indexing
(confirming Step 4b's `source`-filtering change requires iterating frame
**values**, not just keys — the plan's `getattr(f, ...)` phrasing is
consistent with this, not a gap), and the conftest.py/local_paths.py.example
dirty-tree diff (an unrelated `DESKTOP_DIR`→`PROJECT_DIR_BASE` rename,
correctly described as pending in the plan's intro). Found nothing else
rising above cosmetic.

---

## Summary

| # | Sev | Finding |
|---|---|---|
| 1 | **Major** | Step 2 (and, less severely, Step 5) never instructs updating `ContributorSignature.to_dict()`/`from_dict()` (resp. `RunSignature`'s) to include the new field — both are hand-written, not auto-serializing — so the new field silently fails to persist across process runs. For `calibration_recipe`, this breaks the staleness mechanism itself once Step 3b makes the recipe non-empty: every run after that would see a fresh non-empty value fail to match a persisted value that's always "" (never written), forcing a full real-Siril rebuild of T21's Luminance master on **every** run, not once. No test in Step 2's list would catch this since all listed tests construct signatures in memory rather than round-tripping through JSON. |

No critical findings. No other majors. Nothing else surfaced above
cosmetic on this pass.

**VERDICT: REVISE**
