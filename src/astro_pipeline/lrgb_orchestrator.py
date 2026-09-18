"""`run_lrgb`: end-to-end LRGB orchestration for a single telescope+target,
combining every user who contributed data on that telescope.

Moved out of `pipeline.py` verbatim (Task 5 Step 12a), then restructured
around an `LRGBOrchestrator` class (Steps 12b-12d) whose three phase methods
match the existing `STAGE_ORDER = ("masters", "reconciled", "final")`
vocabulary -- see `docs/task5-oop-refactor-plan.md` Section 1 and
`docs/task5-step12-substeps.md` for the full design rationale
(multi-user/multi-contributor combining rules, resumability via `usable()`,
and the `RunSignature`-driven staleness cascade). `pipeline.py`'s own module
docstring still carries the detailed narrative explanation of *why* the
pipeline is shaped this way; it is not duplicated here.

`run_lrgb()` itself keeps a byte-for-byte identical public signature and
behavior throughout this restructuring -- it is the only name any external
caller (skill scripts, tests) ever imports.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from astropy.io import fits

from .background_color import (
    UnknownInstrumentError,
    resolve_instrument_profile,
    resolve_osc_instrument_profile,
    run_graxpert_background_extraction,
)
from .calibration import (
    DEFAULT_PEDESTAL,
    CalibrationMode,
    FlatPolicy,
)
from .calibration_policy import infer_calibration_mode, infer_flat_policy
from .colour_contributor import ColourContributor, ColourContributorBuilder
from .contributor_staleness import (
    _clear_colour_contributor_products,
    _clear_osc_contributor_products,
    _colour_contributor_flat_frame_hash,
    _colour_contributor_frame_hash,
    _delete_if_exists,
    _flat_frame_hash,
    _osc_contributor_frame_hash,
)
from .export_image import export
from .filter_constants import LUMINANCE_FILTER, OSC_FILTER, RGB_FILTERS
from .ingest import scan_session
from .checkpoints import checkpoint, save_checkpoints
from .logging_utils import log as _log
from .luminance_selection import (
    LumCandidate,
    discover_luminance_contributors,
    discover_osc_contributors,
    select_luminance_source,
)
from .master_builder import (
    build_group_master,
    contributor_fwhm_arcsec,
    resolve_lights,
)
from .pipeline_result import PipelineResult
from .resume_guard import usable
from .reconciliation import (
    combine_same_grid,
    crop_to_common_coverage,
    match_gain_offset,
    reproject_to_reference,
)
from .registration_stacking import MIN_SEQUENCE_FRAMES
from .run_signature import (
    STAGE_ORDER,
    ContributorSignature,
    RunSignature,
    cascade_from,
    diff_invalidation,
    frame_identity_hash,
    load_run_signature,
    save_run_signature,
)
from .stretch_compose import stretch_and_compose, stretch_rgb
from .workspace import contributor_dir, pipeline_dir

# --- Slice 4.1: run-signature helpers ---------------------------------------
# Factored out of run_lrgb as their own functions for the same reason
# select_luminance_source/resolve_instrument_profile were: directly
# testable without invoking the full calibrate/stack/solve/SPCC chain.


class LRGBOrchestrator:
    """Holds the mutable state `run_lrgb`'s three phases
    (masters/reconciled/final) share, so it doesn't have to be threaded as
    an ever-growing parameter list or returned as an ever-growing tuple
    between free functions. `run_lrgb()` itself is the only public surface
    -- constructs one of these and drives it; nothing outside this module
    ever sees the class.
    """

    def __init__(
        self,
        project_dir: str | Path,
        telescope: str,
        target: str,
        ra_hours: float,
        dec_deg: float,
        lum_binning: int = 1,
        rgb_binning: int = 2,
        stretch_method: str = "autostretch",
        lum_source: tuple[str, int] | None = None,
        pedestal: float = DEFAULT_PEDESTAL,
        stop_after: str | None = None,
        force: set[str] | None = None,
        flat_policy: FlatPolicy | None = None,
        calibration_mode: dict[str, CalibrationMode] | None = None,
    ) -> None:
        if stop_after is not None and stop_after not in STAGE_ORDER:
            raise ValueError(f"stop_after={stop_after!r} is not one of {STAGE_ORDER}")
        force = set(force) if force else set()
        unknown_force = force - set(STAGE_ORDER)
        if unknown_force:
            raise ValueError(f"force={sorted(unknown_force)} names unknown stage(s); valid: {STAGE_ORDER}")

        self.telescope = telescope
        self.target = target
        self.ra_hours = ra_hours
        self.dec_deg = dec_deg
        self.lum_binning = lum_binning
        self.rgb_binning = rgb_binning
        self.stretch_method = stretch_method
        self.lum_source = lum_source
        self.pedestal = pedestal
        self.stop_after = stop_after
        self.force = force
        self.flat_policy = flat_policy
        self.calibration_mode = calibration_mode

        self.project_dir = Path(project_dir)
        self.out = pipeline_dir(self.project_dir)
        self.final = self.out / "final"
        self.final.mkdir(parents=True, exist_ok=True)
        self.result = PipelineResult()
        self.notes = self.result.notes

        self.checkpoint_dir = self.out / "checkpoints"
        self.checkpoints_path = self.checkpoint_dir / "checkpoints.json"
        self.run_signature_path = self.out / "run_signature.json"
        self.old_signature = load_run_signature(self.run_signature_path)

        # Checkpoint delta-chaining state, threaded through every checkpoint
        # call across all three phases -- initialized here (rather than at
        # the top of `_build_masters()`) since nothing reads either before
        # the first checkpoint is emitted, and this keeps all `__init__`-time
        # state assignment in one place.
        self.previous = None
        self.previous_linear: bool | None = None

    def _build_masters(self) -> None:
        """Luminance discovery/build/select, RGB+OSC contributor
        discovery/build, reference-contributor selection, `RunSignature`
        construction + `diff_invalidation` + the `force`-cascade delete,
        masters-stage checkpoints. Sets `self.is_rgb_only`,
        `self.contributors`, `self.reference`, `self.reference_pos` --
        read by `run_lrgb`'s own reconciled/final phases after this
        returns (Steps 12c/12d will move those phases into methods here
        too; until then `run_lrgb` reads these directly off the instance).
        """
        _log(f"=== scanning {self.project_dir.name} ===", self.notes)
        report = scan_session(self.project_dir)

        # --- luminance masters: every (telescope, binning) that has Luminance
        # data for this target gets its own master built here -- NOT scoped to
        # the caller's `telescope`, otherwise a telescope that only ever
        # contributes colour under the user's colour-only rule (T21 today) would
        # never get its own Luminance master built under any slice, including
        # this one. Each user sharing a (telescope, binning) is still merged at
        # the raw-sub level (see module docstring) -- that combining logic is
        # per-contributor, unaffected by there now being more than one
        # contributor.
        #
        # Which contributor actually drives the rendered composite is Slice 2's
        # measured recommendation: every contributor's master gets built (so
        # every one is on disk and inspectable, even the ones not selected --
        # per this project's checkpointed-not-black-box design principle), its
        # median FWHM gets measured and logged, and the sharpest one wins by
        # default. This is deliberately NOT folded into the build loop below --
        # the winner can only be known after every candidate has been measured.
        cal_index = report.calibration_index()
        lum_contributors = discover_luminance_contributors(report, self.target, self.telescope, self.lum_binning)
        force_masters = "masters" in self.force

        lum_candidates: list[LumCandidate] = []
        lum_frame_hashes: dict[str, str] = {}
        lum_flat_frame_hashes: dict[str, str] = {}
        lum_stackcnt: dict[str, int] = {}
        lum_calibration_modes: dict[str, str] = {}
        for lum_telescope, contrib_lum_binning in lum_contributors:
            lum_key = (lum_telescope, contrib_lum_binning)
            lum_label = f"Luminance-{lum_telescope}-bin{contrib_lum_binning}"
            contributor_key = f"{lum_telescope}_bin{contrib_lum_binning}"
            lum_calibration_mode = (self.calibration_mode or {}).get(
                lum_telescope, infer_calibration_mode(report, lum_telescope)
            )
            lum_calibration_modes[contributor_key] = lum_calibration_mode.value
            lum_lights, lum_group_name = resolve_lights(
                report, lum_telescope, self.target, LUMINANCE_FILTER, contrib_lum_binning,
                calibration_mode=lum_calibration_mode,
            )
            if len(lum_lights) < MIN_SEQUENCE_FRAMES:
                # Real finding (T72's single NGC 3628 Luminance test sub, first
                # real precalibrated-path run): Siril refuses to create a
                # sequence from fewer than MIN_SEQUENCE_FRAMES frames, which
                # register_and_stack() cannot recover from (an opaque "No
                # sequence found" crash). A Siril constraint, not specific to
                # this contributor's calibration mode -- skip-and-log here,
                # mirroring the established pattern for a colour contributor
                # missing a filter, rather than crashing the whole run over one
                # under-populated contributor.
                _log(
                    f"[skip] {lum_label}: only {len(lum_lights)} light(s), need at least "
                    f"{MIN_SEQUENCE_FRAMES} to form a Siril sequence -- skipping this "
                    "Luminance contributor rather than crashing the whole run",
                    self.notes,
                )
                continue
            frame_hash = frame_identity_hash([f.path.name for f in lum_lights])
            lum_frame_hashes[contributor_key] = frame_hash

            # Slice 2.2: the matched flat set for this Luminance contributor,
            # looked up here (not inside build_group_master -- see its own docstring)
            # since only the caller has `report` in scope to call flat_index()
            # on. Slice 2.2's per-telescope FlatPolicy: REQUIRE if this
            # telescope ships ANY flat at all (T21's real case), else
            # SKIP_IF_MISSING (T24's), unless `flat_policy` was explicitly
            # passed to override uniformly for the whole run.
            #
            # Precalibrated-path plan: a PRECALIBRATED telescope's flats are
            # never consulted at all (already flat-corrected upstream, see
            # stage_precalibrated_lights()'s docstring) -- calling
            # infer_flat_policy on it would be misleading, since T73 DOES ship
            # (BIN1-only) flats and would report REQUIRE despite this run path
            # never using them.
            if lum_calibration_mode == CalibrationMode.PRECALIBRATED:
                lum_flat_frames: list = []
                lum_flat_policy = FlatPolicy.SKIP_IF_MISSING
            else:
                lum_flat_frames = report.flat_index().get(
                    (lum_telescope, contrib_lum_binning, LUMINANCE_FILTER), []
                )
                lum_flat_policy = (
                    self.flat_policy if self.flat_policy is not None else infer_flat_policy(report, lum_telescope)
                )
            lum_flat_frame_hash = _flat_frame_hash(lum_flat_frames)
            lum_flat_frame_hashes[contributor_key] = lum_flat_frame_hash

            # Slice 4.1: usable() only knows whether master_luminance.fit
            # exists and reads clean -- it has zero notion of which lights
            # produced it, so a sub silently added/removed/replaced (the
            # orphaned pre-merge T24-observer1-M51-Luminance-bin1 group is real,
            # on-disk proof this gap is not hypothetical) would otherwise never
            # trigger a rebuild. Delete the stale master here so usable()'s own
            # skip-if-present gate inside build_group_master naturally regenerates it.
            # Slice 2.3: `flat_frame_hash` is now also passed here -- omitting
            # it (or leaving contributor_stale's own parameter at its default)
            # would silently defeat the whole point of tracking it at all, see
            # ContributorSignature/contributor_stale in run_signature.py.
            stale = force_masters or (
                self.old_signature is not None
                and self.old_signature.contributor_stale(
                    "luminance", contributor_key, frame_hash, self.pedestal, lum_flat_frame_hash
                )
            )
            if stale:
                master_path = pipeline_dir(self.project_dir) / lum_group_name / "lights" / f"master_{LUMINANCE_FILTER.lower()}.fit"
                _delete_if_exists(master_path, f"{lum_label}: run signature changed", self.notes)

            lum_users = sorted({f.user for f in lum_lights})
            if len(lum_users) > 1:
                _log(f"[run ] combining Luminance across users: {', '.join(lum_users)}", self.notes)
            lum_master_path = build_group_master(
                self.project_dir, lum_lights, cal_index, lum_group_name, LUMINANCE_FILTER,
                lum_telescope, contrib_lum_binning, self.ra_hours, self.dec_deg, self.notes,
                flat_frames=lum_flat_frames, flat_policy=lum_flat_policy, pedestal=self.pedestal,
                calibration_mode=lum_calibration_mode,
            )
            self.result.masters[lum_label] = lum_master_path
            lum_stackcnt[contributor_key] = int(fits.getheader(lum_master_path).get("STACKCNT", len(lum_lights)))
            fwhm_arcsec = contributor_fwhm_arcsec(lum_master_path, self.notes, label=lum_label)
            if fwhm_arcsec is not None:
                _log(f"       {lum_label}: median FWHM {fwhm_arcsec:.2f}\"", self.notes)
            lum_candidates.append((lum_key, lum_master_path, fwhm_arcsec))

        # RGB-only mode (2026-09): no Luminance data exists for this target on ANY
        # telescope -- lum_candidates stays empty when discover_luminance_contributors
        # found nothing real to build (the build loop's MIN_SEQUENCE_FRAMES skip above
        # can also leave it empty for a telescope with too few Luminance lights).
        # "Zero Luminance masters could be built for this target, anywhere" is a
        # purely structural, unambiguous fact once the build loop has run -- unlike
        # CalibrationMode's own auto-detection (a judgement call about degraded vs
        # intentional data), there is no real target where "some but deliberately
        # excluded" Luminance would misfire on this: select_luminance_source's own
        # sharpest-wins rule already handles "built but not chosen"; empty only
        # happens when literally nothing could be built. Real case: Abell 6 and
        # HFG1 (T02), a one-shot-colour delivery with no Luminance filter at all.
        self.is_rgb_only = not lum_candidates
        if self.is_rgb_only:
            if self.lum_source is not None:
                raise ValueError(
                    f"lum_source={self.lum_source!r} given but no Luminance data exists for "
                    f"{self.target} on any telescope -- nothing to select from"
                )
            _log(f"[run ] no Luminance data found for {self.target} on any telescope -- RGB-only mode", self.notes)
            selected_key, selected_path, selected_fwhm = None, None, None
        else:
            selected_key, selected_path, selected_fwhm = select_luminance_source(
                lum_candidates, self.lum_source, (self.telescope, self.lum_binning), self.notes
            )
            self.result.masters[LUMINANCE_FILTER] = selected_path

        # --- colour contributors: one per binning that has a full R/G/B set,
        # for this telescope+target. rgb_binning is the PRIMARY contributor and
        # keeps the legacy top-level file paths; anything else discovered is an
        # additional contributor reconciled in at the master level. ------------
        rgb_binnings = sorted(
            {
                key[3]
                for key in report.instrument_groups()
                if key[0] == self.telescope and key[1] == self.target and key[2] in RGB_FILTERS
            }
        )
        if self.rgb_binning not in rgb_binnings:
            rgb_binnings = [self.rgb_binning, *rgb_binnings]

        # RGB-only/OSC plan (2026-09): one-shot-colour (Color/OSC) binnings for
        # this telescope+target, symmetric with the RGB discovery above --
        # deliberately telescope-scoped the same way (discover_osc_contributors
        # itself searches every telescope, mirroring discover_luminance_
        # contributors, but this caller only ever builds its OWN telescope's
        # OSC contributors, matching RGB discovery's existing, documented
        # hard-scoping -- broadening either one is a separate, later concern).
        osc_binnings = sorted(
            {
                b for (osc_telescope, b) in discover_osc_contributors(report, self.target, self.telescope, self.rgb_binning)
                if osc_telescope == self.telescope
            }
        )
        # Mixed OSC+mono-RGB reconciliation is deliberately unsupported (see
        # plan-rgb-only-mode.md ??7): channel-order parity between Siril's
        # -debayer output and rgbcomp's R-then-G-then-B convention has never
        # been verified, and a binning with BOTH real shapes of colour data
        # would produce two ColourContributors with the IDENTICAL `.key` -- a
        # real dict-key collision that would silently corrupt colour_frame_
        # hashes/colour_flat_frame_hashes/new_signature.colour construction
        # below. Caught HERE, at contributor-collection time, before any
        # build or signature construction -- not deferred to reconciliation,
        # which round-2 adversarial review found is already too late to
        # prevent the corruption.
        #
        # Checked against REAL data presence (report.instrument_groups()
        # directly), NOT the padded `rgb_binnings`/`osc_binnings` lists above --
        # both of those unconditionally prepend the caller's own `rgb_binning`
        # even with zero real data of that shape (mirrors discover_luminance_
        # contributors' own "always include caller's combo" convention), so
        # intersecting the padded lists directly would false-positive on
        # *every* real OSC-only target, since there is no separate osc_binning
        # parameter distinguishing the two intentions -- caught during real
        # testing, not by either adversarial review round.
        _real_rgb_binnings = {
            key[3] for key in report.instrument_groups()
            if key[0] == self.telescope and key[1] == self.target and key[2] in RGB_FILTERS
        }
        _real_osc_binnings = {
            key[3] for key in report.instrument_groups()
            if key[0] == self.telescope and key[1] == self.target and key[2] == OSC_FILTER
        }
        _mixed_shape_binnings = _real_rgb_binnings & _real_osc_binnings
        if _mixed_shape_binnings:
            raise NotImplementedError(
                f"{self.telescope}/{self.target} has both full R/G/B and one-shot-colour (Color) data at "
                f"binning(s) {sorted(_mixed_shape_binnings)} -- combining a mono-RGB and an OSC "
                "contributor for the same (telescope, binning) is not supported (unverified "
                "channel-order parity between debayer output and rgbcomp)."
            )

        # RGB-only/OSC plan (2026-09): try mono first, then OSC, before giving
        # up -- a telescope registered ONLY as OSC (T02's real case) must not
        # silently compute profile_tuple=None here while its own
        # _build_osc_colour_contributor persists a real OSC signature later.
        # An earlier version of this only tried the mono resolver, which
        # meant profile_tuple was unconditionally None for T02 every run,
        # while the (not-yet-existing-at-that-point) OSC signature was real --
        # `existing.spcc_profile != profile_tuple` would then always compare
        # true, deleting and rebuilding SPCC's output on every single
        # invocation, silently defeating resume-safety. Caught by round-1
        # adversarial review before this ever shipped.
        try:
            profile = resolve_instrument_profile(self.telescope)
            profile_tuple = (profile.mono_sensor, profile.red_filter, profile.green_filter, profile.blue_filter)
        except UnknownInstrumentError:
            try:
                osc_profile = resolve_osc_instrument_profile(self.telescope)
                profile_tuple = (osc_profile.osc_sensor, osc_profile.osc_filter or "", osc_profile.osc_lpf or "")
            except UnknownInstrumentError:
                # Let _build_colour_contributor/_build_osc_colour_contributor
                # raise this at the right point (inside SPCC, once there is
                # actually a contributor to fail on) rather than aborting
                # discovery over a telescope with no registered profile of
                # either shape; recorded as no profile for signature purposes.
                profile_tuple = None

        # Slice 2.2: colour discovery is hard-scoped to the caller's own
        # `telescope` (see module docstring / plan-flats-v3.md fact 11), so
        # there is exactly one telescope's worth of flat policy (and,
        # precalibrated-path plan, calibration mode) to infer here, computed
        # once rather than per-binning.
        colour_calibration_mode = (self.calibration_mode or {}).get(
            self.telescope, infer_calibration_mode(report, self.telescope)
        )
        colour_flat_policy = (
            FlatPolicy.SKIP_IF_MISSING
            if colour_calibration_mode == CalibrationMode.PRECALIBRATED
            else (self.flat_policy if self.flat_policy is not None else infer_flat_policy(report, self.telescope))
        )

        self.contributors: list[ColourContributor] = []
        colour_frame_hashes: dict[str, str] = {}
        colour_flat_frame_hashes: dict[str, str] = {}
        for binning in rgb_binnings:
            contributor_key = f"{self.telescope}_bin{binning}"
            contrib_dir = contributor_dir(self.final, self.telescope, binning, self.rgb_binning)
            frame_hash = _colour_contributor_frame_hash(report, self.telescope, self.target, binning)
            colour_frame_hashes[contributor_key] = frame_hash
            colour_flat_frame_hash = _colour_contributor_flat_frame_hash(report, self.telescope, binning)
            colour_flat_frame_hashes[contributor_key] = colour_flat_frame_hash

            # Slice 4.1: same gap as the Luminance loop above, plus SPCC
            # profile identity -- neither is visible to usable()'s file-only
            # gate. A frame/pedestal change forces a full rebuild (raw R/G/B
            # masters and every downstream product); an SPCC-profile-only
            # change only needs the colour-calibrated output redone, since the
            # profile has no effect on calibration/stacking. Slice 2.3: the
            # matched flat set is now also part of what "full rebuild" means --
            # contributor_stale's own `flat_frame_hash` parameter is passed
            # explicitly here, not left at its default (see run_signature.py's
            # ContributorSignature/contributor_stale docstrings for exactly why
            # a default alone would silently defeat this).
            needs_full_rebuild = force_masters or (
                self.old_signature is not None
                and self.old_signature.contributor_stale(
                    "colour", contributor_key, frame_hash, self.pedestal, colour_flat_frame_hash
                )
            )
            if needs_full_rebuild:
                deleted = _clear_colour_contributor_products(
                    contrib_dir, self.project_dir, report, self.telescope, self.target, binning,
                    calibration_mode=colour_calibration_mode,
                )
                if deleted:
                    _log(
                        f"[run ] BIN{binning}: run signature changed -- deleted "
                        f"{len(deleted)} stale contributor file(s) to force rebuild",
                        self.notes,
                    )
            elif self.old_signature is not None:
                existing = self.old_signature.colour.get(contributor_key)
                if existing is not None and existing.spcc_profile != profile_tuple:
                    _delete_if_exists(
                        contrib_dir / "rgb_colour_calibrated.fit",
                        f"BIN{binning}: SPCC profile changed",
                        self.notes,
                    )

            builder = ColourContributorBuilder(
                self.project_dir, contrib_dir, report, self.telescope, self.target, binning,
                self.ra_hours, self.dec_deg, self.notes,
            )
            contributor = builder.build_rgb(
                flat_policy=colour_flat_policy, calibration_mode=colour_calibration_mode,
            )
            if contributor is None:
                # Logged inside ColourContributorBuilder.build_rgb already
                # (Slice 3.2: log-and-skip, not abort-the-run).
                continue
            self.contributors.append(contributor)
            if len(rgb_binnings) > 1:
                _log(
                    f"       {contributor.key} contributor: {contributor.stack_total} stacked "
                    "subs across R/G/B (STACKCNT)",
                    self.notes,
                )

        # RGB-only/OSC plan (2026-09): one-shot-colour (Color) contributors,
        # built alongside the mono-RGB ones above into the SAME `contributors`
        # list -- the mixed-shape guard earlier already ruled out any binning
        # collision between the two discovery results.
        #
        # REAL BUG, fixed 2026-09-16 (found while writing Task 5's Step-0 safety
        # net tests): `osc_binnings` is the PADDED list from discover_osc_
        # contributors(), which -- like discover_luminance_contributors() --
        # always includes the caller's own (telescope, rgb_binning) even with
        # ZERO real Color/OSC data for this target. Iterating that padded list
        # unconditionally meant every pure mono-RGB target (M51, NGC 3628,
        # Abell 31, ...) ran this loop once for its own rgb_binning too, which
        # clobbered `colour_frame_hashes[contributor_key]` (the SAME key the
        # mono-RGB loop above just wrote) with an empty-lights OSC hash --
        # poisoning contributor_stale()'s comparison on every subsequent call
        # and forcing a full colour-contributor rebuild (raw masters through
        # SPCC) on EVERY resume, forever, for any target with no real OSC data.
        # Only iterate binnings with REAL Color data (`_real_osc_binnings`,
        # already computed above for the mixed-shape guard) -- a real OSC-only
        # telescope (T02, T68) is unaffected, since its real binning(s) are
        # already members of that set.
        for binning in osc_binnings:
            if binning not in _real_osc_binnings:
                continue
            contributor_key = f"{self.telescope}_bin{binning}"
            contrib_dir = contributor_dir(self.final, self.telescope, binning, self.rgb_binning)
            frame_hash = _osc_contributor_frame_hash(report, self.telescope, self.target, binning, colour_calibration_mode)
            colour_frame_hashes[contributor_key] = frame_hash
            colour_flat_frame_hashes[contributor_key] = ""  # OSC never consults flats -- see build_group_master docstring

            needs_full_rebuild = force_masters or (
                self.old_signature is not None
                and self.old_signature.contributor_stale("colour", contributor_key, frame_hash, self.pedestal, "")
            )
            if needs_full_rebuild:
                deleted = _clear_osc_contributor_products(
                    contrib_dir, self.project_dir, report, self.telescope, self.target, binning,
                    calibration_mode=colour_calibration_mode,
                )
                if deleted:
                    _log(
                        f"[run ] BIN{binning}: run signature changed -- deleted "
                        f"{len(deleted)} stale OSC contributor file(s) to force rebuild",
                        self.notes,
                    )
            elif self.old_signature is not None:
                existing = self.old_signature.colour.get(contributor_key)
                if existing is not None and existing.spcc_profile != profile_tuple:
                    _delete_if_exists(
                        contrib_dir / "rgb_colour_calibrated.fit",
                        f"BIN{binning}: SPCC profile changed",
                        self.notes,
                    )

            builder = ColourContributorBuilder(
                self.project_dir, contrib_dir, report, self.telescope, self.target, binning,
                self.ra_hours, self.dec_deg, self.notes,
            )
            contributor = builder.build_osc(calibration_mode=colour_calibration_mode)
            if contributor is None:
                continue
            self.contributors.append(contributor)

        if not self.contributors:
            raise RuntimeError(
                f"No usable RGB or OSC colour contributor for {self.telescope}/{self.target} -- every "
                f"discovered RGB binning ({rgb_binnings}) was missing at least one of "
                f"Red/Green/Blue, and every discovered OSC binning ({osc_binnings}) had no "
                f"usable Color data either."
            )

        # Slice 3.4/3.5's designated reference -- the contributor with the
        # most STACKCNT -- computed here (not only inside the >1-contributor
        # branch below) so Slice 4.1's signature can record it even for a
        # single-contributor run; max() over one element just returns it.
        self.reference_pos = max(range(len(self.contributors)), key=lambda i: self.contributors[i].stack_total)
        self.reference = self.contributors[self.reference_pos]
        if len(self.contributors) > 1:
            _log(
                f"[run ] gain/offset reference: {self.reference.key} "
                f"(STACKCNT {self.reference.stack_total}, highest)",
                self.notes,
            )

        # --- Slice 4.1: build this run's signature, diff against whatever was
        # persisted last time, and delete exactly the downstream files that
        # diff says are now stale -- BEFORE touching lum_bg.fits/
        # rgb_reconciled.fit/lrgb_final.fit below, so their own usable() gates
        # see the deletion and regenerate naturally (see run_signature.py's
        # module docstring: no second, parallel gating mechanism). This is
        # the ONLY place STACKCNT and the reference-contributor identity are
        # known, which is why the reference-flip nuance (STACKCNT changing
        # enough to pick a different reference with no light frame changing
        # at all) can only be caught here, post-build -- not in the pre-build
        # per-contributor checks above.
        new_signature = RunSignature(
            stretch_method=self.stretch_method,
            pedestal=self.pedestal,
            # RGB-only mode: selected_key is None (no Luminance exists for this
            # target on any telescope) -- "" mirrors this dataclass's own existing
            # empty-string sentinel for "nothing selected" (colour_reference's
            # default, below), and diff_invalidation's real comparison is a plain
            # equality check that handles "" like any other string: a target
            # gaining/losing Luminance data between runs correctly registers as a
            # signature change.
            luminance_selected=f"{selected_key[0]}_bin{selected_key[1]}" if selected_key else "",
            luminance={
                key: ContributorSignature(
                    key=key, stackcnt=lum_stackcnt[key], frame_hash=lum_frame_hashes[key],
                    flat_frame_hash=lum_flat_frame_hashes[key],
                    calibration_mode=lum_calibration_modes[key],
                )
                for key in lum_frame_hashes
            },
            colour_reference=self.reference.key,
            colour={
                c.key: ContributorSignature(
                    key=c.key, stackcnt=c.stack_total, frame_hash=colour_frame_hashes[c.key],
                    flat_frame_hash=colour_flat_frame_hashes[c.key],
                    spcc_profile=profile_tuple,
                    calibration_mode=colour_calibration_mode.value,
                )
                for c in self.contributors
            },
        )
        signature_stages = diff_invalidation(self.old_signature, new_signature)
        stages_to_invalidate = set(signature_stages)
        for stage in self.force:
            stages_to_invalidate |= cascade_from(stage)

        def _reason(stage: str) -> str:
            # Distinguish an automatic signature-mismatch invalidation from an
            # explicit force -- both end up in `stages_to_invalidate`, but the
            # log should say which actually applied here, not always blame the
            # signature (a force={"final"} call has nothing to do with
            # stretch_method, for example).
            via_signature = stage in signature_stages
            via_force = any(stage in cascade_from(f) for f in self.force)
            if via_signature and via_force:
                return "run signature changed, and explicitly forced"
            if via_signature:
                return "run signature changed"
            return "explicitly forced"

        if "masters" in stages_to_invalidate:
            reason = f"{_reason('masters')} (Luminance-affecting)"
            _delete_if_exists(self.final / "lum_bg.fits", reason, self.notes)
            _delete_if_exists(self.final / "rgb_reconciled.fit", reason, self.notes)
            _delete_if_exists(self.final / "lrgb_final.fit", reason, self.notes)
        if "reconciled" in stages_to_invalidate:
            reason = f"{_reason('reconciled')} (colour-affecting)"
            _delete_if_exists(self.final / "rgb_reconciled.fit", reason, self.notes)
            _delete_if_exists(self.final / "lrgb_final.fit", reason, self.notes)
        if "final" in stages_to_invalidate:
            _delete_if_exists(self.final / "lrgb_final.fit", _reason("final"), self.notes)
        save_run_signature(new_signature, self.run_signature_path)

        # --- checkpoints for the "masters" stop_after boundary ----------------
        if not self.is_rgb_only:
            cp = checkpoint(
                selected_path, f"01_master_{LUMINANCE_FILTER.lower()}", output_dir=self.checkpoint_dir,
                linear=True, previous=self.previous, previous_linear=self.previous_linear,
            )
            self.result.checkpoints.append(cp)
            self.previous, self.previous_linear = cp.stats, True
            _log(cp.summary(), self.notes)
            save_checkpoints(self.result.checkpoints, self.checkpoints_path)

        primary_calibrated = self.final / "rgb_colour_calibrated.fit"
        if primary_calibrated.exists():
            cp = checkpoint(
                primary_calibrated, "02_primary_rgb_colour_calibrated", output_dir=self.checkpoint_dir,
                linear=True, previous=self.previous, previous_linear=self.previous_linear,
            )
            self.result.checkpoints.append(cp)
            self.previous, self.previous_linear = cp.stats, True
            _log(cp.summary(), self.notes)
            save_checkpoints(self.result.checkpoints, self.checkpoints_path)


def run_lrgb(
    project_dir: str | Path,
    telescope: str,
    target: str,
    ra_hours: float,
    dec_deg: float,
    lum_binning: int = 1,
    rgb_binning: int = 2,
    stretch_method: str = "autostretch",
    lum_source: tuple[str, int] | None = None,
    pedestal: float = DEFAULT_PEDESTAL,
    stop_after: str | None = None,
    force: set[str] | None = None,
    flat_policy: FlatPolicy | None = None,
    calibration_mode: dict[str, CalibrationMode] | None = None,
) -> PipelineResult:
    """`lum_source`, if given, names an explicit `(telescope, binning)`
    among the discovered Luminance contributors to drive the composite --
    overriding Slice 2's default sharpest-wins measurement. This is
    deliberately a direct parameter, not an automatic override rule: a
    fully-automatic sharpness rule is genuinely underspecified in real
    edge cases (same telescope shot at two binnings; a near-tie), so the
    project's own checkpointed-not-black-box design principle keeps the
    final call available to a human (Slice 4's skill wraps this as a real
    menu choice; here it's just an argument). Raises ValueError if the
    named combo wasn't actually discovered for this target.

    `pedestal` (Slice 4.1) is the constant added to every calibrated
    light before registration/stacking (see calibration.py's module
    docstring for why this exists at all) -- promoted here from a
    calibration.py-internal default to a real, resume-guarded `run_lrgb`
    argument, per plan-rev4.md's Slice 4.1: it changes every master's
    actual pixels, so a caller passing a different value on a resumed run
    must not silently get back a master built under the OLD value just
    because `usable()` sees an existing file.

    `stop_after` (Slice 4.3) ends the call early, honestly, against this
    function's REAL control flow -- not against the 5 checkpoint labels,
    which are not phase boundaries (`02_primary_rgb_colour_calibrated`
    completes only after every colour contributor is built, since RGB
    binnings are processed in sorted() order and the primary is whichever
    binning the caller named, not necessarily processed first). One of:

        "masters"     -- Luminance selected + every colour contributor
                          built. Returns with `result.masters` populated,
                          no reconciliation/stretch/export attempted.
        "reconciled"  -- the above, plus L background extraction and
                          (when there is more than one colour contributor)
                          reprojection/gain-match/combine. A no-op boundary
                          for a single-contributor run -- no combine ever
                          happens for it, so this stops in the same place
                          "masters" would other than L's background
                          extraction.
        "final"       -- (the default, via `stop_after=None`) everything,
                          i.e. today's full run.

    Returns without raising, with no partial/broken files -- whatever
    stages were actually reached get their checkpoints emitted (Slice
    4.2 moved checkpoint emission inline, per-stage, specifically so a
    `stop_after`-terminated call still leaves an inspectable
    `checkpoints.json` behind).

    `force` (Slice 4.3) names stages -- from the same
    `{"masters", "reconciled", "final"}` vocabulary -- whose outputs
    should be deleted/invalidated before running, even if `usable()`
    would otherwise skip them, matching 4.1's dependency order:
    `force={"masters"}` also invalidates "reconciled" and "final" (they
    are derived from masters), not just "masters" itself -- see
    run_signature.cascade_from, which both `force` and the automatic
    run-signature-mismatch check below go through.

    `flat_policy` (Slice 2.2 of plan-flats-v3.md), when given, overrides
    the per-telescope default computed by `infer_flat_policy` (REQUIRE if
    a telescope ships ANY flat at all, else SKIP_IF_MISSING) UNIFORMLY for
    every telescope discovered in this run -- a narrower override than
    per-telescope, but the common case ("force skip everywhere for a
    quick test run") doesn't need per-telescope granularity, and
    `lum_source`-style per-contributor overrides already exist as the
    escape hatch pattern for anything finer. Left as None (the default),
    each telescope gets its own inferred policy: T21 (ships flats for all
    11 filters at BIN1 in this delivery) defaults to REQUIRE, T24 (ships
    none) defaults to SKIP_IF_MISSING.

    `calibration_mode` (precalibrated-path plan, 2026-09), when given, is a
    PER-TELESCOPE override dict (`{telescope: CalibrationMode}`) -- unlike
    `flat_policy`'s single uniform override, precalibrated-vs-raw is
    fundamentally a per-telescope data-availability fact, not a
    quick-test-override convenience. A telescope not present in the dict
    (or when `calibration_mode` is left `None`) gets its own inferred mode
    from `infer_calibration_mode` -- PRECALIBRATED only if this telescope
    has zero local Bias-or-Dark frames of any kind AND has calibrated-
    provenance lights to fall back to (real case: NGC 3628/T73); every
    telescope with real Bias+Dark (T24, T21) is unaffected, unconditionally
    RAW_LOCAL.
    """
    orchestrator = LRGBOrchestrator(
        project_dir, telescope, target, ra_hours, dec_deg,
        lum_binning=lum_binning, rgb_binning=rgb_binning, stretch_method=stretch_method,
        lum_source=lum_source, pedestal=pedestal, stop_after=stop_after, force=force,
        flat_policy=flat_policy, calibration_mode=calibration_mode,
    )
    orchestrator._build_masters()

    result = orchestrator.result
    notes = orchestrator.notes
    final = orchestrator.final
    is_rgb_only = orchestrator.is_rgb_only
    contributors = orchestrator.contributors
    reference = orchestrator.reference
    reference_pos = orchestrator.reference_pos
    checkpoint_dir = orchestrator.checkpoint_dir
    checkpoints_path = orchestrator.checkpoints_path
    previous, previous_linear = orchestrator.previous, orchestrator.previous_linear

    if stop_after == "masters":
        masters_note = (
            "every colour contributor built (RGB-only, no Luminance data found)"
            if is_rgb_only else "Luminance selected and every colour contributor built"
        )
        _log(f"[stop] stop_after='masters' -- {masters_note}; stopping before reconciliation", notes)
        return result

    # --- luminance: background extraction --------------------------------
    # Skipped entirely for is_rgb_only -- there is no L to extract a
    # background from. lum_bg/lum_for_compose_path (both real inputs to the
    # reprojection block below) only exist on the Luminance-driven path.
    if not is_rgb_only:
        lum_bg = final / "lum_bg.fits"
        if not usable(lum_bg, notes):
            shutil.copy2(result.masters[LUMINANCE_FILTER], final / "lum.fit")
            _log("[run ] GraXpert background extraction on L", notes)
            lum_bg = run_graxpert_background_extraction(final / "lum.fit", output_stem="lum_bg")
        else:
            _log("[skip] L background extraction already done", notes)

        cp = checkpoint(
            lum_bg, "03_lum_background_extracted", output_dir=checkpoint_dir,
            linear=True, previous=previous, previous_linear=previous_linear,
        )
        result.checkpoints.append(cp)
        previous, previous_linear = cp.stats, True
        _log(cp.summary(), notes)
        save_checkpoints(result.checkpoints, checkpoints_path)

        # `lum_for_compose_path` is computed unconditionally (not inside the
        # usable() guard below) so a RESUMED run -- which skips rebuilding
        # rgb_reconciled entirely -- still picks the same L file that was
        # actually paired with it. Getting this wrong pairs a cropped
        # rgb_reconciled with the original, larger lum_bg on resume: a shape
        # mismatch feeding into rgbcomp -lum=.
        lum_for_compose_path = lum_bg
        if len(contributors) > 1:
            lum_for_compose_path = final / "lum_bg_cropped.fits"

    # --- reproject every colour contributor onto L's grid, then combine --
    rgb_reconciled = final / "rgb_reconciled.fit"
    if not usable(rgb_reconciled, notes):
        if is_rgb_only:
            # No L to reproject onto -- rgb_reconciled is a direct copy of
            # the single contributor's colour-calibrated output (same
            # pixels, same WCS grid, nothing to reconcile against). More
            # than one RGB-only contributor needs real gain-match/crop
            # logic with no L to crop against -- untested, deferred rather
            # than built blind (see plan-rgb-only-mode.md's non-goals).
            if len(contributors) != 1:
                raise NotImplementedError(
                    f"RGB-only mode with {len(contributors)} colour contributors "
                    f"({[c.key for c in contributors]}) is not supported -- only the "
                    "single-contributor case (no reconciliation needed) is implemented."
                )
            _log("[run ] RGB-only: no Luminance to reproject onto -- using contributor directly", notes)
            shutil.copy2(contributors[0].composite_path, rgb_reconciled)
        elif len(contributors) == 1:
            _log("[run ] reprojecting colour onto L's pixel grid", notes)
            recon = reproject_to_reference(contributors[0].composite_path, lum_bg, rgb_reconciled)
            _log(f"       footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f}", notes)
        else:
            reprojected_paths = []
            for contributor in contributors:
                # Slice 3.3: keyed by (telescope, binning) identity, not
                # loop position -- verified on real data that positional
                # naming (`contrib{i}` over sorted(rgb_binnings)) put
                # BIN1's contributor at index 0 even though BIN2 is the
                # PRIMARY one, a real footgun for anyone reading these
                # files by number expecting index 0 = primary.
                out_path = final / f"rgb_reconciled_contrib_{contributor.key}.fit"
                recon = reproject_to_reference(contributor.composite_path, lum_bg, out_path)
                _log(
                    f"[run ] reprojected {contributor.key} onto L's grid "
                    f"(footprint {recon.footprint_mean:.3f}, NaN {recon.nan_fraction:.3f})",
                    notes,
                )
                reprojected_paths.append(out_path)

            # Crop L and every reprojected contributor to their common
            # coverage BEFORE combining, rather than combining with a
            # NaN-aware per-pixel fallback. Each contributor ran its own
            # independent GraXpert background extraction, so their absolute
            # background pedestals genuinely differ (measured on real data:
            # 0.101 vs 0.085, ~18% apart) even though each is internally
            # well balanced (R~G~B within each). A NaN-aware combine uses
            # whichever contributor has data at a given pixel, which leaves
            # a hard STEP in background level exactly at the coverage
            # boundary -- rendered by the shadow-clipped stretch as a
            # visible colour-shifted band along that edge. This is the same
            # failure shape as the single-contributor channel-alignment
            # banding fixed earlier (see crop_to_common_coverage's own
            # docstring) -- cropping means every remaining pixel is a
            # genuine average of every contributor, so there is no boundary
            # left to show a seam at.
            cropped = crop_to_common_coverage(
                [lum_bg, *reprojected_paths], final / "combined_crop"
            )
            cropped_shape = fits.getdata(cropped[0], memmap=False).shape
            _log(
                f"[run ] cropped L + {len(contributors)} contributors to common coverage {cropped_shape}",
                notes,
            )
            shutil.copy2(cropped[0], lum_for_compose_path)
            cropped_rgb = cropped[1:]  # aligned 1:1 with `contributors`

            # Slice 3.4/3.5: the designated reference is the contributor
            # with the most STACKCNT -- both the gain/offset fit (3.4) and
            # the weighting (3.5) are measured against/by it, and it also
            # supplies the combined output's FITS header (3.6), replacing
            # the old paths[0] positional pick. `reference`/`reference_pos`
            # are computed once, above, right after the colour-contributor
            # loop (Slice 4.1 needs the reference's identity for the run
            # signature even on a single-contributor run) -- reused here
            # rather than recomputed.
            gain_matched: list[Path] = []
            for i, (contributor, path) in enumerate(zip(contributors, cropped_rgb)):
                if i == reference_pos:
                    gain_matched.append(path)
                    continue
                matched_path = final / "combined_crop" / f"gain_matched_{contributor.key}.fit"
                fit_results = match_gain_offset(path, cropped_rgb[reference_pos], matched_path)
                for fit in fit_results:
                    _log(
                        f"       {contributor.key} vs {reference.key} ch{fit.channel}: "
                        f"gain={fit.gain:.4f} on {fit.n_pixels} high-signal px "
                        f"(ref bkg {fit.reference_background:.5f}, "
                        f"contrib bkg {fit.contributor_background:.5f})",
                        notes,
                    )
                gain_matched.append(matched_path)

            # Slice 3.5: weight by STACKCNT (subs that actually survived
            # quality filtering/rejection into the stacked masters), not
            # sub_count (raw light count) -- see combine_same_grid's own
            # docstring for why an inverse-variance scheme was tried and
            # rejected in between.
            weights = [float(c.stack_total) for c in contributors]
            combine_same_grid(
                gain_matched, rgb_reconciled, weights=weights, reference_index=reference_pos,
            )
            _log(
                f"[run ] combined {len(contributors)} colour contributors by STACKCNT "
                f"(weights {weights})",
                notes,
            )
    else:
        _log("[skip] reprojection already done", notes)

    cp = checkpoint(
        rgb_reconciled, "04_rgb_reconciled", output_dir=checkpoint_dir,
        linear=True, previous=previous, previous_linear=previous_linear,
    )
    result.checkpoints.append(cp)
    previous, previous_linear = cp.stats, True
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    if stop_after == "reconciled":
        reconciled_note = (
            "colour ready (RGB-only, no Luminance to extract/reconcile against)"
            if is_rgb_only else "L background extracted and colour reconciled"
        )
        _log(f"[stop] stop_after='reconciled' -- {reconciled_note}; stopping before stretch/export", notes)
        return result

    # --- stretch + composition --------------------------------------------
    # RGB-only: stretch the reconciled RGB alone, no rgbcomp -lum= (no L to
    # composite onto). Output named "rgb_final" (not "lrgb_final") -- an
    # RGB-only run producing a file literally named "lrgb" would be
    # misleading on disk.
    composite_name = "rgb_final" if is_rgb_only else "lrgb_final"
    composite = final / f"{composite_name}.fit"
    if not usable(composite, notes):
        if is_rgb_only:
            rgb_in = final / "rgb_for_compose.fit"
            shutil.copy2(rgb_reconciled, rgb_in)
            _log(f"[run ] stretch ({stretch_method}), RGB-only (no Luminance to compose)", notes)
            compose = stretch_rgb(rgb_in, final, output_stem=composite_name, method=stretch_method)
        else:
            lum_in = final / "lum_for_compose.fit"
            rgb_in = final / "rgb_for_compose.fit"
            shutil.copy2(lum_for_compose_path, lum_in)
            shutil.copy2(rgb_reconciled, rgb_in)
            _log(f"[run ] stretch ({stretch_method}) + rgbcomp -lum", notes)
            compose = stretch_and_compose(
                lum_in, rgb_in, final, output_stem=composite_name, method=stretch_method,
            )
        composite = compose.composite_path
    else:
        _log(f"[skip] {'RGB' if is_rgb_only else 'LRGB'} composite already present", notes)
    result.composite_path = composite

    cp = checkpoint(
        composite, "05_rgb_final" if is_rgb_only else "05_lrgb_final", output_dir=checkpoint_dir,
        linear=False, previous=previous, previous_linear=previous_linear,  # post-stretch: render faithfully
    )
    result.checkpoints.append(cp)
    previous, previous_linear = cp.stats, False
    _log(cp.summary(), notes)
    save_checkpoints(result.checkpoints, checkpoints_path)

    # --- export -----------------------------------------------------------
    # Always re-run, unconditionally -- export() has no usable() gate of its
    # own (cheap: format conversion, not a Siril/GraXpert/SPCC call), so it
    # simply reflects whatever `composite` currently is, resumed or fresh.
    _log("[run ] export TIFF + preview", notes)
    # Slice 4.4: `target` threaded through as the stem instead of a
    # hardcoded "M51_lrgb" -- verified safe against the real M51 fixture
    # (target="M51" here reproduces the exact same "M51_lrgb" stem, so
    # tests/test_pipeline.py's byte-identical-output assertions against
    # M51_lrgb.tif are unaffected), and required for the skill (4.4) to
    # generalize the handoff path to whatever target the user throws at it
    # next instead of silently mislabeling every future target's TIFF as
    # M51's.
    result.export_result = export(
        composite, output_dir=final, stem=f"{target}_rgb" if is_rgb_only else f"{target}_lrgb"
    )
    _log(
        f"       row order {result.export_result.row_order}, "
        f"clipped low {result.export_result.clipped_low_fraction:.4f} / "
        f"high {result.export_result.clipped_high_fraction:.4f}",
        notes,
    )

    return result
