"""The single boto3 S3 client for the Massive flat-files endpoint.

Every caller (flatfile_pull, entitlements) builds the same client: s3v4
signing against the vendor endpoint with the dashboard-issued key pair.
Credential prechecks stay with the callers -- flatfile_pull exits 3 with a
fix-creds message, entitlements reports SKIP -- so this module assumes
nothing about how a missing key should be surfaced.
"""

from __future__ import annotations

from typing import Any

from ingest.common.config import Settings


def s3_client(settings: Settings) -> Any:
    """boto3 S3 client pointed at the Massive flat-files endpoint."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.massive_s3_endpoint,
        aws_access_key_id=settings.massive_s3_access_key_id,
        aws_secret_access_key=settings.massive_s3_secret_access_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )
