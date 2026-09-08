"""Run-signature tracking: what `usable()` (pipeline.py) cannot see.

`usable()` answers "does this stage's output file exist, parse, and look
non-degenerate" -- and nothing about WHY that file has the content it
does. That gap is real, not hypothetical: an orphaned
`T24-kaveh096-M51-Luminance-bin1` group directory sits on disk today
(dated 2026-09-05) from before the multi-user Luminance merge (commit
04eac54) landed -- `usable()` would happily resume from that single-user
master forever, with zero way to know a second user's data should now be
merged in, because nothing about a master FITS file records which lights
built it. Slice 3's combine logic compounds the gap: the gain-fit/
weighting reference contributor is picked dynamically each run (whichever
colour contributor has the highest STACKCNT, see pipeline.run_lrgb) --
that choice was never persisted anywhere, so a resumed run whose STACKCNT
values shifted enough to flip the reference would silently keep serving a
`rgb_reconciled.fit` gain-matched against the WRONG reference, with
`usable()` seeing only "the file exists" and having no opinion.

A `RunSignature` records exactly the inputs `usable()` can't see:
per-contributor light identity (so a sub silently replaced without the
sub COUNT changing is caught -- file existence alone cannot), STACKCNT
per contributor, which Luminance source was selected, which colour
contributor is the reconciliation reference, each colour contributor's
resolved SPCC profile, and the two previously-unguarded parameters that
change a run's OUTPUT without changing any FILE's existence or count
(`stretch_method`, `pedestal`). Diffing the newly-computed signature
against whatever was persisted last time says exactly which downstream
stage outputs are now stale -- pipeline.py then deletes those specific
files so `usable()`'s existing skip-if-present logic naturally
regenerates them, rather than this module inventing a second, parallel
gating mechanism.

Dependency order, used throughout this module and pipeline.py's
`stop_after`/`force` vocabulary (Slice 4.3):

    masters -> reconciled -> final

Invalidating a stage invalidates every stage after it: a Luminance-
affecting or colour-contributor-affecting change invalidates from
"masters" onward (deletes `lum_bg.fits`, `rgb_reconciled.fit`,
`lrgb_final.fit`); a colour-contributor-only change (including a
reference-contributor flip, or an SPCC profile change) invalidates from
"reconciled" onward (`rgb_reconciled.fit`, `lrgb_final.fit`); a
`stretch_method`-only change invalidates "final" alone (`lrgb_final.fit`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

STAGE_ORDER = ("masters", "reconciled", "final")


def cascade_from(stage: str) -> set[str]:
    """Every stage from `stage` onward in the masters -> reconciled ->
    final dependency order. Both the signature-mismatch diff below and
    pipeline.py's `force` parameter (Slice 4.3) go through this, so
    "invalidating/forcing an earlier stage also invalidates everything
    after it" is enforced in exactly one place.
    """
    if stage not in STAGE_ORDER:
        raise ValueError(f"{stage!r} is not one of {STAGE_ORDER}")
    idx = STAGE_ORDER.index(stage)
    return set(STAGE_ORDER[idx:])


def frame_identity_hash(names: list[str]) -> str:
    """Stable identity for a set of light frames, from their FILENAMES.

    plan-rev4.md's Slice 4.1 text says to hash sorted `(name, DATE-OBS)`
    -- checked against the real code before implementing (this project's
    own "measure, don't theorize" convention) and that's not quite what
    exists: `ingest.LightFrame` has no `DATE-OBS` field at all (FITS'
    DATE-OBS is never parsed into it), and its `date`+`time` fields --
    along with `user` and the per-exposure `sequence` number that
    actually disambiguates frames sharing a date/time -- are already
    embedded in the filename itself (see ingest.py's `_LIGHT_RE`).
    Hashing the sorted filename set is therefore both simpler than
    reconstructing an equivalent tuple by hand AND strictly at least as
    specific (it additionally catches a sub being renamed/re-sequenced
    without any of date/time/user changing).
    """
    payload = "\n".join(sorted(names))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ContributorSignature:
    """One Luminance or colour contributor's identity, as of the run that
    produced whatever is currently persisted to `run_signature.json`.

    `spcc_profile` is None for a Luminance contributor (SPCC never runs
    on Luminance) and the resolved `(mono_sensor, red_filter,
    green_filter, blue_filter)` tuple for a colour contributor -- Slice
    3.1's `resolve_instrument_profile` result, recorded here so a change
    to `INSTRUMENT_PROFILES` (a new/corrected sensor entry) is caught
    even though it touches no light frame and no sub count.
    """

    key: str
    stackcnt: int
    frame_hash: str
    spcc_profile: tuple[str, str, str, str] | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "stackcnt": self.stackcnt,
            "frame_hash": self.frame_hash,
            "spcc_profile": list(self.spcc_profile) if self.spcc_profile else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ContributorSignature:
        profile = d.get("spcc_profile")
        return cls(
            key=d["key"],
            stackcnt=int(d["stackcnt"]),
            frame_hash=d["frame_hash"],
            spcc_profile=tuple(profile) if profile else None,
        )


@dataclass(frozen=True)
class RunSignature:
    """Everything about a `run_lrgb` call that `usable()`'s file-existence
    checks cannot see on their own -- see module docstring.

    `quality_filter_policy` is intentionally a fixed, informational
    string rather than a per-contributor field: `build_master`'s
    FWHM/roundness percentile cut (`90.0 if n >= 10 else None`) is a pure
    function of a contributor's own light COUNT, which `frame_hash`
    already captures (two contributors can only differ in whether the
    90%-cut applies if their light sets differ, and a light-set
    difference already changes `frame_hash`) -- so there is no scenario
    where this policy's EFFECT changes without an accompanying
    `frame_hash` change already catching it. Recorded here anyway so the
    actual thresholds are visible in `run_signature.json` and a future
    code change to the formula itself (not modelled as a `run_lrgb`
    parameter -- see plan-rev4.md Slice 4.1, which leaves this a
    judgement call) would at least be documented in every signature
    written from here on.
    """

    stretch_method: str
    pedestal: float
    luminance_selected: str
    luminance: dict[str, ContributorSignature] = field(default_factory=dict)
    colour_reference: str = ""
    colour: dict[str, ContributorSignature] = field(default_factory=dict)
    quality_filter_policy: str = "filter_fwhm_pct=filter_round_pct=90.0 if n>=10 else None"

    def to_dict(self) -> dict:
        return {
            "stretch_method": self.stretch_method,
            "pedestal": self.pedestal,
            "luminance_selected": self.luminance_selected,
            "luminance": {k: v.to_dict() for k, v in self.luminance.items()},
            "colour_reference": self.colour_reference,
            "colour": {k: v.to_dict() for k, v in self.colour.items()},
            "quality_filter_policy": self.quality_filter_policy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RunSignature:
        return cls(
            stretch_method=d["stretch_method"],
            pedestal=float(d["pedestal"]),
            luminance_selected=d["luminance_selected"],
            luminance={k: ContributorSignature.from_dict(v) for k, v in d.get("luminance", {}).items()},
            colour_reference=d.get("colour_reference", ""),
            colour={k: ContributorSignature.from_dict(v) for k, v in d.get("colour", {}).items()},
            quality_filter_policy=d.get("quality_filter_policy", ""),
        )

    def contributor_stale(self, section: str, key: str, frame_hash: str, pedestal: float) -> bool:
        """Does contributor `key` in `section` ("luminance" or "colour")
        need its raw master(s) rebuilt via Siril, given a freshly computed
        `frame_hash` and the current call's `pedestal`?

        True if the light set changed (added/removed/replaced -- a
        different `frame_hash`), if the contributor is new (not present
        in this persisted signature at all), or if `pedestal` itself
        changed (it is baked into every calibrated light BEFORE
        registration/stacking -- see calibration.py -- so it affects
        every master, Luminance and colour alike, regardless of whether
        any light set changed).
        """
        if self.pedestal != pedestal:
            return True
        existing = getattr(self, section).get(key)
        return existing is None or existing.frame_hash != frame_hash


def load_run_signature(path: str | Path) -> RunSignature | None:
    """None on a first run (no file yet) or if the file is unreadable/
    corrupt -- either way, "nothing to invalidate against" is the correct
    degraded behaviour (matches this project's usable()/checkpoint()
    convention of degrading rather than raising on a corrupt-but-optional
    input)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return RunSignature.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        return None


def save_run_signature(signature: RunSignature, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(signature.to_dict(), indent=2), encoding="utf-8")


def diff_invalidation(old: RunSignature | None, new: RunSignature) -> set[str]:
    """Which of {"masters", "reconciled", "final"} the transition from
    `old` (persisted; None on a first run, when nothing is invalidated --
    there is nothing to disagree with yet) to `new` (just computed for
    the current call) invalidates. Already expanded through
    `cascade_from`, so a caller never has to reason about the dependency
    order itself -- e.g. a Luminance change returns
    {"masters", "reconciled", "final"}, not just {"masters"}.

    This is the authoritative, POST-build check (both L and colour
    contributors' STACKCNT are only known after Siril has actually built
    their masters) -- it is what catches the reference-contributor-flip
    case a pre-build, frame-identity-only comparison structurally cannot:
    two runs can have byte-identical light sets and still pick a
    different gain-fit/weighting reference if STACKCNT-affecting quality
    filtering rejected a different number of subs. See
    pipeline.run_lrgb's own pre-build per-contributor checks (using
    `RunSignature.contributor_stale`) for the earlier, frame-identity-only
    pass that decides whether a raw master needs rebuilding via Siril at
    all -- that pass necessarily runs before STACKCNT exists, so it
    cannot see the reference-flip case; this function is what does.
    """
    if old is None:
        return set()

    stages: set[str] = set()

    # "masters"-tier: anything that changes what a Luminance master's own
    # pixels are (pedestal is baked into every calibrated light before
    # stacking), or which Luminance source is selected, or any
    # Luminance contributor's identity/STACKCNT.
    if old.pedestal != new.pedestal:
        stages |= cascade_from("masters")
    if old.luminance_selected != new.luminance_selected:
        stages |= cascade_from("masters")
    if old.luminance != new.luminance:
        stages |= cascade_from("masters")

    # "reconciled"-tier: a colour contributor's identity/STACKCNT/SPCC
    # profile, or which contributor is the gain-fit/weighting reference
    # (the STACKCNT-flip nuance flagged in plan-rev4.md's Slice 4.1).
    if old.colour != new.colour:
        stages |= cascade_from("reconciled")
    if old.colour_reference != new.colour_reference:
        stages |= cascade_from("reconciled")

    # "final"-tier: stretch_method alone changes only the rendered
    # composite -- not any master, not the reconciled colour.
    if old.stretch_method != new.stretch_method:
        stages |= cascade_from("final")

    return stages
