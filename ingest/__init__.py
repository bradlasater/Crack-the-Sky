"""Crack the Sky: Massive.com (ex-Polygon.io) options data ingestion.

Subpackages:
    common:  shared config, HTTP client, market gate, landing writers, CLI runner.
    schemas: pyarrow schemas and ClickHouse DDL for every dataset.
    jobs:    cron-driven ingestion jobs (python -m ingest.jobs.<name>).
"""

__version__ = "0.1.0"
