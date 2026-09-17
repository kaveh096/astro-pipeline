"""Shared print+collect logging helper (Task 5 refactor, 2026-09-16).

Deduplicates two near-identical private copies that used to live in
`pipeline.py` and `calibration.py`. NOT byte-identical originally: keep
`notes: list[str] | None` with the `if notes is not None:` guard
(calibration.py's version) -- `calibration.py`'s `stage_precalibrated_
lights()` defaults `notes=None` and is called with no `notes` arg by real,
Siril-gated tests, which would crash under the OTHER copy's stricter
`notes: list[str]` (unconditional `notes.append(...)`).
"""

from __future__ import annotations


def log(message: str, notes: list[str] | None) -> None:
    print(message, flush=True)
    if notes is not None:
        notes.append(message)
