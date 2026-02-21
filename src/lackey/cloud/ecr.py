"""ECR image push — ensures the minion image is available in ECR."""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

import base64
import subprocess
import sys


def ensure_image_in_ecr(
    local_tag: str,
    ecr_registry: str,
    repository: str,
    region: str,
    *,
    force: bool = False,
) -> str:
    """Push local Docker image to ECR if not already present.

    Returns the full ECR image URI (registry/repository:tag).
    Set force=True to push even if the tag already exists in ECR.
    """
    import boto3

    ecr = boto3.client("ecr", region_name=region)

    # Extract the tag portion (after ':') or default to 'latest'
    tag = local_tag.split(":")[-1] if ":" in local_tag else "latest"
    ecr_uri = f"{ecr_registry}/{repository}:{tag}"

    # Check if the image already exists in ECR
    if not force:
        try:
            ecr.describe_images(
                repositoryName=repository,
                imageIds=[{"imageTag": tag}],
            )
            print(f"Image {ecr_uri} already exists in ECR")
            return ecr_uri
        except ecr.exceptions.ImageNotFoundException:
            pass

    print(f"Pushing {local_tag} → {ecr_uri}")

    # Authenticate Docker with ECR
    auth = ecr.get_authorization_token()
    auth_data = auth["authorizationData"][0]
    token = base64.b64decode(auth_data["authorizationToken"]).decode()
    username, password = token.split(":", 1)
    endpoint = auth_data["proxyEndpoint"]

    login = subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", endpoint],
        input=password.encode(),
        capture_output=True,
    )
    if login.returncode != 0:
        print(f"ERROR: docker login failed: {login.stderr.decode()}", file=sys.stderr)
        raise SystemExit(1)

    # Tag and push
    subprocess.run(["docker", "tag", local_tag, ecr_uri], check=True)
    push = subprocess.run(["docker", "push", ecr_uri], capture_output=False)
    if push.returncode != 0:
        print("ERROR: docker push failed", file=sys.stderr)
        raise SystemExit(1)

    print(f"Pushed {ecr_uri}")
    return ecr_uri
