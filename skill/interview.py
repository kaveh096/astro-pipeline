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
    for real inside pipeline.build_master) is a safe per-image scale-down.
    Computed here by calling the exact same `select_dark()` against the
    exact same `calibration_index()` build_master() will use -- a preview,
    not a re-implementation of the policy.

This is presentation logic for the interview checkpoint, not pipeline
logic -- it never calls Siril/GraXpert/SPCC and never writes to
`_pipeline/`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astro_pipeline.calibration import CalibrationFramesMissingError, select_dark  # noqa: E402
from astro_pipeline.ingest import IngestReport, scan_session  # noqa: E402

# The three fixed templates IngestReport.missing_calibration_warnings()
# emits today (ingest.py ~line 200-233). If that function's wording ever
# changes, dedupe_calibration_warnings() below degrades to passing the
# line through unrecognized rather than crashing or dropping it.
_BIAS_RE = re.compile(
    r"^No Bias frames found for (?P<telescope>\S+) BIN(?P<binning>\d+) "
    r"\(needed for (?P<target>[^/]+)/(?P<filter>[^)]+)\)\.$"
)
_DARK_RE = re.compile(
    r"^No Dark frames at (?P<exptime>[\d.]+)s found for (?P<telescope>\S+) BIN(?P<binning>\d+) "
    r"\(needed for (?P<target>[^/]+)/(?P<filter>[^)]+)\)\.$"
)
_FLAT_RE = re.compile(
    r"^No Flat frames found for (?P<telescope>\S+) BIN(?P<binning>\d+) "
    r"\(needed for (?P<target>[^/]+)/(?P<filter>[^)]+)\)\.$"
)


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


def render(project_dir: Path, report: IngestReport) -> str:
    summary = session_summary(report)
    lines = [f"=== {project_dir.name} ==="]
    lines.append(f"Telescopes found: {', '.join(summary['telescopes']) or '(none)'}")
    lines.append(f"Targets found:    {', '.join(summary['targets']) or '(none)'}")
    lines.append(f"Binnings found:   {', '.join(str(b) for b in summary['binnings']) or '(none)'}")
    lines.append(f"Users found:      {', '.join(summary['users']) or '(none)'}")
    lines.append("")
    lines.append("Instrument groups (telescope, target, filter, binning) -> #lights:")
    for key, n in summary["instrument_groups"].items():
        lines.append(f"  {key} -> {n}")
    lines.append("")

    raw = report.missing_calibration_warnings()
    deduped = dedupe_calibration_warnings(raw)
    lines.append(f"Calibration gaps: {len(raw)} raw warning(s) -> {len(deduped)} deduplicated:")
    for line in deduped:
        lines.append(f"  - {line}")
    lines.append("")

    scaling = dark_scaling_notes(report)
    if scaling:
        lines.append("Dark-scaling resolution (calibration.select_dark preview):")
        for line in scaling:
            lines.append(f"  - {line}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python interview.py <project_dir>", file=sys.stderr)
        return 2
    project_dir = Path(argv[1])
    report = scan_session(project_dir)
    print(render(project_dir, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
