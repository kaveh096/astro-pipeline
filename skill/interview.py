"""Deterministic pre-run summary for SKILL.md's interview step (Slice 4.4).

Runs `scan_session()` against a project folder and produces the real,
non-templated numbers the skill's interview step shows a human before
anything runs:

  - which telescopes/targets/binnings/users were actually found
    (`session_summary`)
  - `IngestReport.missing_calibration_warnings()`'s real output,
    DEDUPLICATED (`dedupe_calibration_warnings`) -- that function repeats
    one warning per (telescope, user, target, filter, binning) group,
    because it iterates `light_groups()`, which is keyed per user (see its
    own docstring). That repetition is a known, accepted property of
    `light_groups()` -- collaborators' subs must NOT be silently merged at
    that layer -- so this is a presentation fix, not a bug fix: don't touch
    `missing_calibration_warnings()`, just don't show its redundancy to a
    human. Verified on the real M51 project folder: 10 raw lines collapse
    to 3 (see tests/test_skill_interview.py's requires_project-gated test
    for the live assertion, and this module's own CLI output for the exact
    text).
  - the real "no matching dark at Xs; scaling from Ys" resolution
    (`dark_scaling_notes`) that `missing_calibration_warnings()` cannot
    surface at all -- it only checks for an EXACT-exptime dark, so T21's
    real 300s/600s-vs-900s-dark case reads as a flat "missing" gap there,
    when what will actually happen (see calibration.select_dark, called
    for real inside master_builder.build_group_master) is a safe
    per-image scale-down. Computed here by calling the exact same
    `select_dark()` against the exact same `calibration_index()`
    build_group_master() will use -- a preview,
    not a re-implementation of the policy.

This is presentation logic for the interview checkpoint, not pipeline
logic -- it never calls Siril/GraXpert/SPCC. It does still call
`scan_session()`, which extracts any zip deliveries into
`_pipeline/_extracted_zips/` (a real, if incidental, filesystem write --
`classify_tree()` is the side-effect-free alternative when that matters,
e.g. for preflight.py's own checks).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.calibration import (  # noqa: E402
    CalibrationFramesMissingError,
    CalibrationMode,
    FlatPolicy,
    classify_flat_basename_groups,
    select_dark,
)
from astro_pipeline.ingest import CALIBRATION_WARNING_RES, IngestReport, scan_session, warning_telescope  # noqa: E402
from astro_pipeline.calibration_policy import infer_calibration_mode, infer_flat_policy  # noqa: E402
from astro_pipeline import preflight  # noqa: E402
from astro_pipeline.background_extraction import find_graxpert  # noqa: E402
from astro_pipeline.color_calibration import (  # noqa: E402
    UnknownInstrumentError,
    resolve_instrument_profile,
    resolve_osc_instrument_profile,
)
from astro_pipeline.filter_constants import OSC_FILTER, RGB_FILTERS  # noqa: E402
from astro_pipeline.siril_driver import find_siril_cli, get_version  # noqa: E402
from astro_pipeline.solving import find_astap_cli  # noqa: E402
from astro_pipeline.star_removal import find_starnet  # noqa: E402

# The three fixed templates IngestReport.missing_calibration_warnings()
# emits today (ingest.py, CALIBRATION_WARNING_RES -- moved there from this
# file, 2026-09, as the shared source of truth lrgb_orchestrator.py's
# precalibrated-path filtering also needs; skill/ has no __init__.py so it isn't
# importable the other direction). If that function's wording ever
# changes, dedupe_calibration_warnings() below degrades to passing the
# line through unrecognized rather than crashing or dropping it.
_BIAS_RE = CALIBRATION_WARNING_RES["bias"]
_DARK_RE = CALIBRATION_WARNING_RES["dark"]
_FLAT_RE = CALIBRATION_WARNING_RES["flat"]


def dedupe_calibration_warnings(raw_warnings: list[str]) -> list[str]:
    """Collapse `missing_calibration_warnings()`'s real per-user repeats to
    one line per (telescope, frame_type, binning).

    Dark warnings additionally aggregate every missing exptime for a given
    (telescope, binning) onto that ONE line (rather than emitting a second
    line per exptime) -- the collapse key plan-rev4.md specifies is
    (telescope, frame_type, binning), which does not include exptime, so
    two exptimes at the same binning collapse together; the exptimes
    themselves are preserved as aggregated detail within the line instead
    of being dropped.
    """
    bias: dict[tuple[str, int], set[str]] = {}
    dark: dict[tuple[str, int], dict[float, set[str]]] = {}
    flat: dict[tuple[str, int], set[str]] = {}
    passthrough: list[str] = []

    for line in raw_warnings:
        m = _BIAS_RE.match(line)
        if m:
            bias.setdefault((m["telescope"], int(m["binning"])), set()).add(m["filter"])
            continue
        m = _DARK_RE.match(line)
        if m:
            key = (m["telescope"], int(m["binning"]))
            dark.setdefault(key, {}).setdefault(float(m["exptime"]), set()).add(m["filter"])
            continue
        m = _FLAT_RE.match(line)
        if m:
            flat.setdefault((m["telescope"], int(m["binning"])), set()).add(m["filter"])
            continue
        passthrough.append(line)

    out: list[str] = []
    for (telescope, binning), filters in sorted(bias.items()):
        out.append(f"{telescope} BIN{binning}: no Bias frames (needed for {', '.join(sorted(filters))})")
    for (telescope, binning), by_exptime in sorted(dark.items()):
        exptimes = ", ".join(f"{e:.0f}s" for e in sorted(by_exptime))
        filters = sorted({f for fs in by_exptime.values() for f in fs})
        out.append(
            f"{telescope} BIN{binning}: no Dark frames at {exptimes} "
            f"(needed for {', '.join(filters)})"
        )
    for (telescope, binning), filters in sorted(flat.items()):
        out.append(f"{telescope} BIN{binning}: no Flat frames (needed for {', '.join(sorted(filters))})")
    return out + passthrough


def dark_scaling_notes(report: IngestReport) -> list[str]:
    """Preview, per (telescope, target, filter, binning) instrument group,
    what `calibration.select_dark()` will actually decide once `run_lrgb`
    calls `build_master()` for real -- the "no matching dark at Xs; scaling
    from Ys" note plan-rev4.md's 4.4 spec asks for.

    Uses the exact same `cal_index` and per-group light exptimes
    `build_master()` uses (via `instrument_groups()`, which is already
    merged across users -- the same unit `build_master` actually
    calibrates), so this is a preview of the real decision, not a second
    implementation of the policy in calibration.py.
    """
    notes: list[str] = []
    cal_index = report.calibration_index()
    for (telescope, target, filter_name, binning), lights in sorted(report.instrument_groups().items()):
        light_exptimes = {f.exptime for f in lights}
        try:
            selection = select_dark(cal_index, telescope, binning, light_exptimes)
        except CalibrationFramesMissingError as exc:
            notes.append(f"[BLOCKED] {telescope}/{target}/{filter_name} BIN{binning}: {exc}")
            continue
        if selection.scaled:
            exptimes_str = ", ".join(f"{e:.0f}s" for e in sorted(light_exptimes))
            notes.append(
                f"{telescope}/{target}/{filter_name} BIN{binning}: no dark at {exptimes_str}; "
                f"will scale from {selection.exptime:.0f}s dark via -opt=exp"
            )
    return notes


def precalibrated_telescopes(report: IngestReport) -> set[str]:
    """Every telescope found in this session whose lights the pipeline
    will use directly (CalibrationMode.PRECALIBRATED), computed with the
    exact same `infer_calibration_mode` run_lrgb itself uses for its
    default -- a preview, not a second implementation of the policy.
    Real case: NGC 3628/T73 (Feb 2025), zero recognized Bias/Dark, real
    calibrated-provenance lights present."""
    telescopes = {k[0] for k in report.instrument_groups()} | {
        k[0] for k in report.calibrated_instrument_groups()
    }
    return {t for t in telescopes if infer_calibration_mode(report, t) == CalibrationMode.PRECALIBRATED}


def unrecognized_summary(report: IngestReport) -> list[str]:
    """Step 6a(i) (plan-flats-v4.md, fixes half of G6): "Filenames and
    FITS header both unrecognized" today just vanishes into
    `report.unrecognized`, never rendered -- a real telescope's flats
    can look flat-less and be SKIPped silently with no visible cause.
    Grouped by `reason` (which already NAMES the real IMAGETYP verbatim
    wherever the header was readable at all -- see `classify_frame`'s own
    message text), not a separate IMAGETYP re-read: this is presentation
    only, and a second header read per unrecognized frame just to
    duplicate information already embedded in `reason` buys nothing.
    """
    from collections import Counter

    counts = Counter(f.reason for f in report.unrecognized)
    return [
        f"  {count}x: {reason}"
        for reason, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def consequence_annotations(
    report: IngestReport, deduped_warnings: list[str], flat_policy_override: FlatPolicy | None = None,
) -> list[str]:
    """Step 6a(ii): the existing flat-gap lines say WHAT is missing, never
    the CONSEQUENCE -- darks get [BLOCKED] on a real run
    (CalibrationFramesMissingError), flats do not, and neither says so
    today. `_FLAT_RE`/the warning wording itself is untouched -- this
    only annotates a separate copy of the same deduplicated lines.

    `flat_policy_override` (Step 8): mirrors `run_lrgb`'s own uniform
    `--flat-policy` override -- when given, used for EVERY telescope's
    annotation here instead of each one's own `infer_flat_policy` result,
    so the interview's preview matches what a run with that same override
    would actually do.

    Real bug fixed here: a missing-Dark line used to get an unconditional
    `[BLOCKED]`, even when `calibration.select_dark()` would actually
    scale a longer dark down and let the run proceed (T21's real
    300s/600s-vs-900s-dark case) -- the interview told a human "this stops
    a real run" for something that doesn't. Computed per (telescope,
    binning) by actually calling `select_dark()` against the same
    `calibration_index()`/real light exptimes `build_group_master()` uses
    -- not by re-deriving exptimes from this function's own lossy
    `:.0f`-formatted warning text. If ANY instrument group at that
    (telescope, binning) would still raise, the line stays `[BLOCKED]`
    (conservative: a mixed-filter binning where one filter genuinely has
    no usable dark is still a real blocker).
    """
    cal_index = report.calibration_index()
    blocked_dark_binnings: set[tuple[str, int]] = set()
    for (telescope, _target, _filter_name, binning), lights in report.instrument_groups().items():
        light_exptimes = {f.exptime for f in lights}
        try:
            select_dark(cal_index, telescope, binning, light_exptimes)
        except CalibrationFramesMissingError:
            blocked_dark_binnings.add((telescope, binning))

    annotated: list[str] = []
    for line in deduped_warnings:
        telescope = line.split(" BIN", 1)[0]
        if "no Dark frames" in line:
            binning = int(line.split(" BIN", 1)[1].split(":", 1)[0])
            if (telescope, binning) in blocked_dark_binnings:
                annotated.append(f"  [BLOCKED] {line}")
            else:
                annotated.append(f"  (will scale -- see Dark-scaling resolution below) {line}")
        elif "no Bias frames" in line:
            annotated.append(f"  [BLOCKED] {line}")
        elif "no Flat frames" in line:
            policy = flat_policy_override if flat_policy_override is not None else infer_flat_policy(report, telescope)
            if policy == FlatPolicy.REQUIRE:
                annotated.append(f"  [BLOCKED] {line}")
            else:
                annotated.append(f"  (no flat applied -- proceeding without flat correction) {line}")
        else:
            annotated.append(f"  {line}")
    return annotated


def _flat_header_signature(frame) -> tuple:
    """`flat_identity_preview`'s own CHEAP header-identity signal (DATE-OBS,
    EXPTIME, file size), NOT SHA-256 (R3-10) -- the interview makes no copy
    of the flats the way `run_calibration` does, so re-hashing T21's 330
    flats (or T68's 88 large frames) on every interview run would be
    needlessly expensive. The SHA-256-based mechanism stays in Step 3a's
    `run_calibration` path, which copies the files anyway.

    `except Exception`, matching every other FITS-header-read site touched
    in this diff (ingest.py, calibration.py): astropy can reject a corrupt
    header with VerifyError, a struct-unpacking ValueError,
    UnicodeDecodeError, etc, not just OSError -- any of those must fold
    into this "unreadable" signature rather than crash the whole render.
    """
    from astropy.io import fits

    try:
        header = fits.getheader(frame.path)
        size = frame.path.stat().st_size
        return (str(header.get("DATE-OBS")), header.get("EXPTIME"), size)
    except Exception:
        return ("unreadable", None, None)


def flat_identity_preview(frames: list) -> str:
    """Step 6a(iii): `N frames (K copies, C collisions)` -- built on the
    same shared `classify_flat_basename_groups` skeleton
    `classify_flat_identity`/`stage_flat_frames` (calibration.py) use,
    fed `_flat_header_signature` (above) instead of their SHA-256 content
    signature (see that function's own docstring for why)."""
    groups = classify_flat_basename_groups(frames, _flat_header_signature)

    copies = 0
    collisions = 0
    for group in groups:
        if len(group.frames) == 1:
            continue
        if group.is_collision:
            collisions += 1
        else:
            copies += len(group.frames) - 1
    return f"{len(frames)} frames ({copies} copies, {collisions} collisions)"


def prerequisites_summary(report: IngestReport) -> list[str]:
    """Informational (never blocking) status lines: which external tools
    are actually found on this machine, Siril's version against the
    minimum, and -- per telescope this scan found R/G/B or Color data
    for -- whether an SPCC colour-calibration profile is registered.

    This calls the real find_*()/get_version() functions (unlike the rest
    of this module, which never touches Siril/GraXpert/SPCC) -- it is the
    one place in the interview that actually looks for the tools, so a
    human sees "GraXpert: not found" before a multi-hour run gets there.
    Real preflight enforcement (the exit-2 blocking check) lives in
    preflight.py; this is its read-only, informational cousin.
    """
    lines = ["Prerequisites:"]

    for label, finder in (
        ("Siril", find_siril_cli),
        ("ASTAP", find_astap_cli),
        ("GraXpert", find_graxpert),
        ("StarNet2", find_starnet),
    ):
        try:
            exe = finder()
            lines.append(f"  {label}: found at {exe}")
        except FileNotFoundError:
            lines.append(f"  {label}: NOT FOUND")

    try:
        siril_cli = find_siril_cli()
        version = get_version(siril_cli, timeout=30)
        version_str = ".".join(map(str, version))
        if version < preflight.MIN_SIRIL_VERSION:
            min_str = ".".join(map(str, preflight.MIN_SIRIL_VERSION))
            lines.append(f"  Siril version: {version_str} -- older than the required {min_str}+")
        else:
            lines.append(f"  Siril version: {version_str} (OK)")
    except FileNotFoundError:
        pass  # already reported as NOT FOUND above
    except Exception as exc:
        lines.append(f"  Siril version: could not determine ({exc})")

    groups = report.instrument_groups()
    telescopes_seen = sorted({t for (t, _tgt, _f, _b) in groups})
    for telescope in telescopes_seen:
        filters_here = {f for (t, _tgt, f, _b) in groups if t == telescope}
        statuses = []
        if filters_here & set(RGB_FILTERS):
            try:
                resolve_instrument_profile(telescope)
                statuses.append("mono SPCC profile OK")
            except UnknownInstrumentError:
                statuses.append("NO mono SPCC profile registered")
        if OSC_FILTER in filters_here:
            try:
                resolve_osc_instrument_profile(telescope)
                statuses.append("OSC SPCC profile OK")
            except UnknownInstrumentError:
                statuses.append("NO OSC SPCC profile registered")
        if statuses:
            lines.append(f"  {telescope}: {'; '.join(statuses)}")

    return lines


def session_summary(report: IngestReport) -> dict:
    """Real counts for the interview's "what was found" line -- no
    hardcoded target/telescope/binning assumed, so this generalizes to
    whatever project folder is actually scanned."""
    groups = report.instrument_groups()
    return {
        "telescopes": sorted({k[0] for k in groups}),
        "targets": sorted({k[1] for k in groups}),
        "binnings": sorted({k[3] for k in groups}),
        "users": sorted({f.user for f in report.lights}),
        "instrument_groups": {k: len(v) for k, v in sorted(groups.items())},
    }


def render(
    project_dir: Path,
    report: IngestReport,
    flat_policy_override: FlatPolicy | None = None,
    prerequisites: list[str] | None = None,
) -> str:
    """`prerequisites` is pre-computed (via prerequisites_summary()) and
    passed in rather than computed here, deliberately -- this function
    must stay callable without touching any real external tool, since
    most of this module's own tests do exactly that."""
    summary = session_summary(report)
    lines = [f"=== {project_dir.name} ==="]
    if prerequisites:
        lines.extend(prerequisites)
        lines.append("")
    lines.append(f"Telescopes found: {', '.join(summary['telescopes']) or '(none)'}")
    lines.append(f"Targets found:    {', '.join(summary['targets']) or '(none)'}")
    lines.append(f"Binnings found:   {', '.join(str(b) for b in summary['binnings']) or '(none)'}")
    lines.append(f"Users found:      {', '.join(summary['users']) or '(none)'}")
    lines.append("")
    lines.append("Instrument groups (telescope, target, filter, binning) -> #lights:")
    for key, n in summary["instrument_groups"].items():
        lines.append(f"  {key} -> {n}")
    lines.append("")

    precalibrated = precalibrated_telescopes(report)
    if precalibrated:
        lines.append(
            f"Precalibrated (no local bias/dark/flat needed): {', '.join(sorted(precalibrated))} "
            "-- calibrated-provenance lights will be used directly (CalibrationMode.PRECALIBRATED)."
        )
        lines.append("")

    raw = report.missing_calibration_warnings()
    # A PRECALIBRATED telescope's "no Bias/Dark/Flat" warnings are expected
    # (that's WHY it resolved precalibrated, see infer_calibration_mode) and
    # not worth surfacing as if they were a problem for this run -- filtered
    # here via the same anchored regexes missing_calibration_warnings()'s
    # own wording is built from (warning_telescope()), not a guess at which
    # lines mention which telescope.
    raw = [line for line in raw if warning_telescope(line) not in precalibrated]
    deduped = dedupe_calibration_warnings(raw)
    lines.append(f"Calibration gaps: {len(raw)} raw warning(s) -> {len(deduped)} deduplicated:")
    for line in deduped:
        lines.append(f"  - {line}")
    lines.append("")

    # Step 6a(ii): the consequence of each gap above -- darks/bias always
    # [BLOCKED]; flats depend on this run's own inferred/overridden
    # FlatPolicy. A separate section; the gap lines themselves (and their
    # dedupe-parseable wording) are untouched.
    if deduped:
        lines.append("Consequences:")
        lines.extend(consequence_annotations(report, deduped, flat_policy_override))
        lines.append("")

    # Step 6a(iii): a cheap identity preview for every MATCHED flat group
    # (as opposed to the gaps above, which are about MISSING ones) --
    # surfaces G1's own class of finding (colliding basenames silently
    # collapsed by staging) before a real run ever touches Siril.
    flat_index = report.flat_index()
    flat_preview_lines = [
        f"  {telescope} BIN{binning}/{filt}: {flat_identity_preview(frames)}"
        for (telescope, binning, filt), frames in sorted(flat_index.items())
        if telescope not in precalibrated
    ]
    if flat_preview_lines:
        lines.append("Matched flats:")
        lines.extend(flat_preview_lines)
        lines.append("")

    # Step 6a(i): frames neither a filename pattern nor (if enabled)
    # header-based fallback could place anywhere -- today's real, silent
    # failure mode this section exists to end.
    unrecognized_lines = unrecognized_summary(report)
    if unrecognized_lines:
        lines.append(f"Unrecognized frames ({len(report.unrecognized)} total):")
        lines.extend(unrecognized_lines)
        lines.append(
            "  (expected light filename: raw-T24-<user>-<target>-YYYYMMDD-HHMMSS-<filter>"
            "-BIN<n>-<E|W|_>-<exptime>-<seq>.fits, case-insensitive; only iTelescope's own "
            "naming is recognized)"
        )
        lines.append("")

    scaling = [
        line for line in dark_scaling_notes(report)
        if not any(line.startswith(f"{t}/") or line.startswith(f"[BLOCKED] {t}/") for t in precalibrated)
    ]
    if scaling:
        lines.append("Dark-scaling resolution (calibration.select_dark preview):")
        for line in scaling:
            lines.append(f"  - {line}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="interview.py",
        description="Scan a project folder and report what's there -- telescopes, targets, "
        "calibration gaps, and tool/version prerequisites -- without running or writing anything.",
    )
    parser.add_argument("project_dir", help="the project folder to scan (raw iTelescope delivery, not _pipeline/)")
    parser.add_argument(
        "--calibration-header-fallback", action="store_true",
        help="opt-in header-based recognition of calibration frames a filename pattern can't see (Step 4b)",
    )
    parser.add_argument(
        "--flat-policy", choices=["require", "skip_if_missing"], default=None,
        help="preview consequence annotations as if this FlatPolicy were uniformly overridden",
    )
    args = parser.parse_args(argv[1:])

    project_dir = Path(args.project_dir)
    if not project_dir.exists():
        print(f"{project_dir} does not exist.")
        return 2

    report = scan_session(project_dir, calibration_header_fallback=args.calibration_header_fallback)
    flat_policy_override = FlatPolicy(args.flat_policy) if args.flat_policy else None
    prerequisites = prerequisites_summary(report)
    print(render(project_dir, report, flat_policy_override, prerequisites))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
