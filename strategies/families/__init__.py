"""Setup-family detectors (2026-09-17).

One detector per named trade idea, written to a fixed contract (side, entry
reference, pattern-defined stop, invalidation, expiry) and validated in
scripts/scanner_lab.py against a pre-registered kill list BEFORE anything
here is routed live. Nothing in this package is imported by the live scan
loop until its pre-registration passes — see docs/research/.
"""
