"""Derived market signals that sit on top of ingest + pricing reductions.

Not capture, not a pricer. These jobs read landed datasets and write a
narrower table another stage can consume without re-deriving the same
quantity. ``python -m signals.spot`` was the first; ``signals.har_rv`` turns
that series into the baseline realised-variance forecast.
"""
