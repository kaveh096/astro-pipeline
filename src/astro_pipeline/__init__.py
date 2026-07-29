"""Automated, checkpointed astrophotography processing pipeline.

Calibration -> registration/stacking -> pixel-grid reconciliation ->
background extraction -> color calibration -> stretch -> LRGB/narrowband
composition -> export, orchestrated stage-by-stage with human checkpoints.
See research/2026-07-27-tooling-research.md for the tool survey behind
these choices.
"""

__version__ = "0.1.0"
