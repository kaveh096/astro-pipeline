"""One (telescope, binning) colour contributor -- RGB (mono three-filter)
or OSC (one-shot-colour/Bayer) -- and the `ColourContributorBuilder` that
builds either shape (Task 5 refactor, 2026-09-17).

Replaces the former free functions `_build_colour_contributor()`/
`_build_osc_colour_contributor()` in `pipeline.py`. The two shared ~9-10
of their ~13/11 parameters (project_dir, contrib_dir, report, telescope,
target, binning, ra_hours, dec_deg, notes) AND a real amount of body
logic beyond that (the GraXpert background-extraction block and the SPCC
colour-calibration block are near-identical between the two, differing
only in which source path/instrument-profile resolver feeds them and the
exact log wording) -- `run_lrgb` already constructs one builder's worth
of context per (telescope, binning) inside a loop, calling exactly one of
`build_rgb()`/`build_osc()` (never both -- the pre-existing mixed-shape
guard in `run_lrgb` forecloses a binning needing both shapes), which is
exactly the shape a small stateful object fits.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from astropy.io import fits

from .background_color import (
    resolve_instrument_profile,
    resolve_osc_instrument_profile,
    run_graxpert_background_extraction,
    run_spcc,
)
from .calibration import CalibrationMode, FlatPolicy
from .filter_constants import OSC_FILTER, RGB_FILTERS
from .logging_utils import log as _log
from .master_builder import build_group_master, resolve_lights
from .reconciliation import crop_to_common_coverage, reproject_to_reference
from .registration_stacking import MIN_SEQUENCE_FRAMES
from .resume_guard import usable
from .siril_driver import run_script


@dataclass
class ColourContributor:
    """One (telescope, binning) RGB contributor, once built -- Slice 3's
    naming fix (3.3) means downstream code identifies a contributor by
    this, not by its position in a loop (see run_lrgb's combine-multiple-
    contributors branch and its earlier positional `contrib{i}` bug).

    `filter_shape` (RGB-only/OSC plan, 2026-09): "rgb" (default, the
    existing mono path -- three separate R/G/B masters combined via
    rgbcomp) or "osc" (ColourContributorBuilder.build_osc() -- one
    already-Bayer-mosaic filter debayered directly into a 3-channel
    composite). Exists so run_lrgb can detect and refuse a mixed
    OSC+mono-RGB reconciliation at contributor-collection time, BEFORE any
    ColourContributor.key collision could corrupt signature construction --
    channel-order parity between Siril's `-debayer` output and rgbcomp's
    R-then-G-then-B convention has never been verified, so combining the
    two shapes is deliberately unsupported (see run_lrgb's colour-
    contributor discovery and the plan's non-goals)."""

    telescope: str
    binning: int
    composite_path: Path
    sub_count: int
    stack_total: int
    filter_shape: str = "rgb"

    @property
    def key(self) -> str:
        """Stable identity string for filenames/logging, e.g. 'T24_bin1'."""
        return f"{self.telescope}_bin{self.binning}"


class ColourContributorBuilder:
    """Builds one (telescope, binning) colour contributor, either shape.

    Construct once per (telescope, binning) with the context shared by
    both shapes, then call exactly one of `build_rgb()`/`build_osc()`."""

    def __init__(
        self,
        project_dir: Path,
        contrib_dir: Path,
        report,
        telescope: str,
        target: str,
        binning: int,
        ra_hours: float,
        dec_deg: float,
        notes: list[str],
    ) -> None:
        self.project_dir = project_dir
        self.contrib_dir = contrib_dir
        self.report = report
        self.telescope = telescope
        self.target = target
        self.binning = binning
        self.ra_hours = ra_hours
        self.dec_deg = dec_deg
        self.notes = notes

    def _extract_background(self, source_path: Path, label: str) -> Path:
        """Shared GraXpert background-extraction step -- `label` is the
        exact word(s) substituted into the original per-shape log text
        ("RGB" for the mono path, "OSC RGB" for the OSC path), so log
        output is byte-for-byte unchanged from the pre-split functions."""
        rgb_bg = self.contrib_dir / "rgb_native_bg.fits"
        if not usable(rgb_bg, self.notes):
            _log(f"[run ] BIN{self.binning}: GraXpert background extraction on {label}", self.notes)
            rgb_bg = run_graxpert_background_extraction(source_path, output_stem="rgb_native_bg")
        else:
            _log(f"[skip] BIN{self.binning}: {label} background extraction already done", self.notes)
        return rgb_bg

    def _run_spcc(self, rgb_bg: Path, profile, profile_description: str) -> Path:
        """Shared SPCC-invocation step -- stages the input under a
        temporary name and only moves it into place once SPCC succeeds
        (see build_rgb's own docstring for why: a valid-looking but
        untransformed file left behind on failure would fool `usable()`).
        `profile_description` is substituted into the original per-shape
        "SPCC colour calibration (...)" log line verbatim."""
        colour_calibrated = self.contrib_dir / "rgb_colour_calibrated.fit"
        staging = self.contrib_dir / "rgb_colour_calibrated__inprogress.fit"
        shutil.copy2(rgb_bg, staging)
        _log(f"[run ] BIN{self.binning}: SPCC colour calibration ({profile_description})", self.notes)
        solution = run_spcc(staging, self.contrib_dir, profile=profile)
        os.replace(staging, colour_calibrated)
        _log(
            f"       BIN{self.binning} SPCC used {solution.stars_used} stars, "
            f"white balance {solution.white_balance}",
            self.notes,
        )
        return colour_calibrated

    def build_rgb(
        self,
        flat_policy: FlatPolicy = FlatPolicy.SKIP_IF_MISSING,
        calibration_mode: CalibrationMode = CalibrationMode.RAW_LOCAL,
        filters: tuple[str, str, str] = RGB_FILTERS,
        run_colour_calibration: bool = True,
    ) -> ColourContributor | None:
        """Build this binning's 3-filter masters, align + crop + composite
        them, then background-extract and (optionally) colour-calibrate --
        everything the single-contributor pipeline always did, just
        namespaced under `contrib_dir` so multiple binnings can coexist.

        `filters` (capability A, narrowband/SHO-HOO plan, 2026-09): the
        (red-slot, green-slot, blue-slot) filter-name triplet fed to
        `rgbcomp`, defaulting to `RGB_FILTERS` -- this default reproduces
        the exact pre-existing behaviour byte-for-byte (same on-disk
        filenames, same log text, same rgbcomp invocation), verified via
        the existing RGB test suite. A repeated filter name across slots
        (HOO's real `("Ha", "OIII", "OIII")` -- OIII feeding both G and B)
        is handled by duplicating that filter's file under a second
        on-disk name rather than assuming Siril's `rgbcomp` accepts the
        identical path twice (unverified) -- `-nosum` is then added to the
        `rgbcomp` call, per Siril's own real HOO tutorial, to avoid
        double-counting exposure/stack-count metadata from the reused
        filter; only added when a repeat is actually present, so the
        default RGB path's `rgbcomp` invocation is completely unchanged.

        `run_colour_calibration` (capability A): SPCC models real
        broadband filter + sensor spectral response against Gaia's
        realistic stellar spectra -- verified real (Siril's own SPCC
        docs) that this produces a physically "correct" but visually
        wrong result for a narrowband palette (a "huge green cast" on
        SHO, since it models SII's genuinely much fainter real line
        intensity rather than preserving the stylized Hubble-palette
        convention). Callers building a narrowband composite should pass
        False; every existing (broadband RGB) caller keeps the default
        True, unchanged.

        Returns None -- logging why, rather than raising -- if any filter
        in `filters` is missing for this (telescope, binning): a colour
        contributor needs all three, but a partial set is a real,
        survivable state once RGB discovery broadens beyond one telescope.

        `flat_policy` (Slice 2.2 of plan-flats-v3.md): threaded in from
        run_lrgb's own per-telescope inference/override (see
        infer_flat_policy) -- this method has no telescope-discovery
        context of its own, it just applies whatever policy the caller
        decided. Defaults to SKIP_IF_MISSING (the safe, pre-Slice-2
        behaviour) so a caller that doesn't pass one explicitly still
        proceeds without a hard flat requirement.

        `calibration_mode` (precalibrated-path plan, 2026-09): threaded in
        from run_lrgb's own per-telescope inference/override (see
        infer_calibration_mode), same pattern as `flat_policy`. Under
        PRECALIBRATED, `resolve_lights()` looks up calibrated-provenance
        groups instead of raw ones, flat lookup is skipped entirely, and
        `build_group_master()` is called with placeholder
        `cal_index={}`/`flat_frames=[]`/`flat_policy=FlatPolicy.SKIP_IF_MISSING`
        (never read inside its PRECALIBRATED branch).
        """
        contrib_dir = self.contrib_dir
        report = self.report
        telescope = self.telescope
        target = self.target
        binning = self.binning
        notes = self.notes

        contrib_dir.mkdir(parents=True, exist_ok=True)
        cal_index = report.calibration_index()

        unique_filters = list(dict.fromkeys(filters))  # de-duplicated, order preserved
        # "all three"/"both" must match the real distinct-filter count -- HOO's
        # real (Ha, OIII, OIII) has only 2 unique filters, not 3, so a fixed
        # "all three" would misdescribe it.
        _quantifier = {1: "the", 2: "both", 3: "all three"}.get(len(unique_filters), f"all {len(unique_filters)}")
        filters_label = f"{_quantifier} of {'/'.join(unique_filters)}"

        channel_masters: dict[str, Path] = {}
        sub_count = 0
        stack_total = 0
        for filter_name in unique_filters:
            lights, group_name = resolve_lights(
                report, telescope, target, filter_name, binning, calibration_mode=calibration_mode
            )
            if not lights:
                _log(
                    f"[skip] BIN{binning}: no {filter_name} lights found for {telescope}/{target} "
                    f"-- a colour contributor needs {filters_label}; skipping this "
                    "contributor rather than aborting the whole run",
                    notes,
                )
                return None
            if len(lights) < MIN_SEQUENCE_FRAMES:
                # Same real Siril constraint as the Luminance loop (see
                # registration_stacking.MIN_SEQUENCE_FRAMES) -- register_and_
                # stack() cannot form a sequence from too few frames.
                _log(
                    f"[skip] BIN{binning}: only {len(lights)} {filter_name} light(s) for "
                    f"{telescope}/{target}, need at least {MIN_SEQUENCE_FRAMES} to form a Siril "
                    "sequence -- skipping this contributor rather than crashing the whole run",
                    notes,
                )
                return None
            sub_count += len(lights)
            if calibration_mode == CalibrationMode.PRECALIBRATED:
                master_path = build_group_master(
                    self.project_dir, lights, {}, group_name, filter_name,
                    telescope, binning, self.ra_hours, self.dec_deg, notes,
                    flat_frames=[], flat_policy=FlatPolicy.SKIP_IF_MISSING,
                    calibration_mode=calibration_mode,
                )
            else:
                flat_frames = report.flat_index().get((telescope, binning, filter_name), [])
                master_path = build_group_master(
                    self.project_dir, lights, cal_index, group_name, filter_name,
                    telescope, binning, self.ra_hours, self.dec_deg, notes,
                    flat_frames=flat_frames, flat_policy=flat_policy,
                )
            channel_masters[filter_name] = master_path
            stack_total += int(fits.getheader(master_path).get("STACKCNT", len(lights)))

        rgb_native = contrib_dir / "rgb_native.fit"
        has_repeated_filter = len(unique_filters) < len(filters)
        if not usable(rgb_native, notes):
            # Each filter was registered against its OWN reference frame, so the
            # three masters do not share a pointing -- and `rgbcomp` stacks them
            # straight into R/G/B channels without aligning anything. Measured on
            # real data, Green sat 8.8px from Red and Blue 4.4px, comparable to
            # the stars' own ~5-9px FWHM, which showed up as red/green fringing
            # on every star in the checkpoint preview. Reproject the other two
            # onto the reference (first slot)'s grid first so the channels
            # actually correspond.
            reference_filter = filters[0]
            reference = channel_masters[reference_filter]
            slot_names = [f"{reference_filter.lower()}.fit"]
            shutil.copy2(reference, contrib_dir / slot_names[0])
            seen_names = {slot_names[0]}
            for filter_name in filters[1:]:
                candidate_name = f"{filter_name.lower()}.fit"
                if candidate_name in seen_names:
                    # A repeated filter across slots (HOO's real OIII-for-G-
                    # and-B case) -- duplicate under a distinct name rather
                    # than colliding on disk or assuming rgbcomp accepts the
                    # identical path twice for two channels (unverified).
                    candidate_name = f"{filter_name.lower()}_2.fit"
                seen_names.add(candidate_name)
                out_path = contrib_dir / candidate_name
                recon = reproject_to_reference(channel_masters[filter_name], reference, out_path)
                _log(
                    f"[run ] BIN{binning}: aligned {filter_name} onto {reference_filter}'s grid "
                    f"(footprint {recon.footprint_mean:.3f})",
                    notes,
                )
                slot_names.append(candidate_name)
            # Crop away the slivers alignment left uncovered rather than filling
            # them -- a constant fill is visible to GraXpert's background model
            # and produced a green band across the finished image. See
            # crop_to_common_coverage.
            channel_paths = [contrib_dir / name for name in slot_names]
            crop_to_common_coverage(channel_paths, contrib_dir)
            cropped_shape = fits.getdata(channel_paths[0], memmap=False).shape
            _log(f"[run ] BIN{binning}: cropped channels to common coverage {cropped_shape}", notes)

            _log(f"[run ] BIN{binning}: rgbcomp at native resolution", notes)
            stems = [p.stem for p in channel_paths]
            nosum_flag = " -nosum" if has_repeated_filter else ""
            run_script([f"rgbcomp {stems[0]} {stems[1]} {stems[2]} -out=rgb_native{nosum_flag}"], workdir=contrib_dir)
        else:
            _log(f"[skip] BIN{binning}: rgb_native.fit already present", notes)

        rgb_bg = self._extract_background(rgb_native, "RGB")

        colour_calibrated = contrib_dir / "rgb_colour_calibrated.fit"
        if not usable(colour_calibrated, notes):
            if run_colour_calibration:
                # Do the work on a temporary name and only move it into place
                # once it succeeds -- see module docstring / commit history:
                # copying the input to the final name up-front and processing
                # in place leaves a valid-looking file behind when the stage
                # fails, and `usable()` cannot tell the difference, since the
                # data IS intact, it simply has not been transformed.
                # Existence must mean completion.
                profile = resolve_instrument_profile(telescope)
                colour_calibrated = self._run_spcc(rgb_bg, profile, f"{profile.mono_sensor}, local Gaia")
            else:
                # Narrowband (capability A): SPCC deliberately skipped (see
                # this method's own docstring) -- the background-extracted
                # composite IS the contributor's final composite as-is.
                shutil.copy2(rgb_bg, colour_calibrated)
                _log(
                    f"[run ] BIN{binning}: colour calibration skipped (narrowband palette) "
                    "-- using background-extracted composite directly",
                    notes,
                )
        elif run_colour_calibration:
            _log(f"[skip] BIN{binning}: SPCC already done", notes)
        else:
            _log(f"[skip] BIN{binning}: colour calibration stage already done (narrowband, no SPCC)", notes)

        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=colour_calibrated,
            sub_count=sub_count, stack_total=stack_total,
        )

    def build_osc(
        self,
        calibration_mode: CalibrationMode = CalibrationMode.PRECALIBRATED,
        bayer_pattern: int = 0,
    ) -> ColourContributor | None:
        """One-shot-colour (OSC) counterpart of build_rgb() (RGB-only/OSC
        plan, 2026-09) -- much shorter, since there is only one filter
        group (`OSC_FILTER`) to build, not three, and no channel
        alignment/crop/rgbcomp step: debayering already produces one
        already-registered-together 3-channel composite per frame (all
        three channels come from the SAME Bayer-mosaic exposure), so
        there is nothing to align. Real case: T02 (Abell 6 and HFG1) --
        confirmed genuine undemosaiced Bayer CFA data (RGGB), no local
        Bias/Dark at all, so PRECALIBRATED. RAW_LOCAL + debayer is also
        real and supported (T68 / IC 1396: local bias, no local dark at
        all -- see build_group_master()'s bias-only-when-no-dark-exists
        branch), so `calibration_mode` is not always PRECALIBRATED in
        general, just for every OSC target seen so far that has no local
        calibration frames.

        Returns None -- logging why, rather than raising -- for the same
        reasons build_rgb() does: no OSC lights found, or too few to form
        a Siril sequence (MIN_SEQUENCE_FRAMES).
        """
        contrib_dir = self.contrib_dir
        report = self.report
        telescope = self.telescope
        target = self.target
        binning = self.binning
        notes = self.notes

        contrib_dir.mkdir(parents=True, exist_ok=True)

        lights, group_name = resolve_lights(
            report, telescope, target, OSC_FILTER, binning, calibration_mode=calibration_mode
        )
        if not lights:
            _log(
                f"[skip] BIN{binning}: no {OSC_FILTER} lights found for {telescope}/{target} "
                "-- skipping this OSC contributor rather than aborting the whole run",
                notes,
            )
            return None
        if len(lights) < MIN_SEQUENCE_FRAMES:
            _log(
                f"[skip] BIN{binning}: only {len(lights)} {OSC_FILTER} light(s) for "
                f"{telescope}/{target}, need at least {MIN_SEQUENCE_FRAMES} to form a Siril "
                "sequence -- skipping this contributor rather than crashing the whole run",
                notes,
            )
            return None

        master_path = build_group_master(
            self.project_dir, lights, {}, group_name, OSC_FILTER, telescope, binning,
            self.ra_hours, self.dec_deg, notes,
            flat_frames=[], flat_policy=FlatPolicy.SKIP_IF_MISSING,
            calibration_mode=calibration_mode, debayer=True, bayer_pattern=bayer_pattern,
        )
        stack_total = int(fits.getheader(master_path).get("STACKCNT", len(lights)))

        # master_path is now a plate-solved, ALREADY-RGB (3, ny, nx) master --
        # genuinely equivalent to rgb_native.fit in the mono-RGB path, just
        # produced by debayer+stack instead of 3x-build+rgbcomp. No channel
        # alignment/crop needed (see docstring).
        rgb_bg = self._extract_background(master_path, "OSC RGB")

        colour_calibrated = contrib_dir / "rgb_colour_calibrated.fit"
        if not usable(colour_calibrated, notes):
            profile = resolve_osc_instrument_profile(telescope)
            colour_calibrated = self._run_spcc(rgb_bg, profile, f"{profile.osc_sensor}, local Gaia, OSC")
        else:
            _log(f"[skip] BIN{binning}: SPCC already done", notes)

        return ColourContributor(
            telescope=telescope, binning=binning, composite_path=colour_calibrated,
            sub_count=len(lights), stack_total=stack_total, filter_shape="osc",
        )
