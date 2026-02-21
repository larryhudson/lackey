"""Upload artifacts from /output to S3 after a cloud run.

Called from entrypoint.sh as: python -m lackey.cloud.upload
Reads ARTIFACT_BUCKET and RUN_ID from environment.
"""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

import os
import sys
from pathlib import Path


def upload_artifacts(output_dir: Path, bucket: str, run_id: str) -> None:
    """Upload all files under output_dir to s3://{bucket}/{run_id}/."""
    import boto3

    s3 = boto3.client("s3")

    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        key = f"{run_id}/{path.relative_to(output_dir)}"
        s3.upload_file(str(path), bucket, key)
        print(f"  uploaded {key}")


def main() -> None:
    bucket = os.environ.get("ARTIFACT_BUCKET")
    run_id = os.environ.get("RUN_ID")

    if not bucket or not run_id:
        print("ERROR: ARTIFACT_BUCKET and RUN_ID are required", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(os.environ.get("OUTPUT_DIR", "/output"))
    upload_artifacts(output_dir, bucket, run_id)


if __name__ == "__main__":
    main()
