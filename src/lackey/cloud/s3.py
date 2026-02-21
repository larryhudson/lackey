"""S3 artifact download."""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

from pathlib import Path


def download_artifacts(
    bucket: str,
    run_id: str,
    local_dir: Path,
    region: str,
) -> Path:
    """Download all artifacts under {run_id}/ from S3 to local_dir.

    Returns local_dir.
    """
    import boto3

    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")
    prefix = f"{run_id}/"

    local_dir.mkdir(parents=True, exist_ok=True)

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            relative = key[len(prefix) :]
            if not relative:
                continue
            dest = local_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dest))
            print(f"  downloaded {relative}")

    return local_dir
