"""GitHub App token minting via AWS Secrets Manager."""

from __future__ import annotations

import time


def get_github_app_private_key(secret_name: str, region: str) -> str:
    """Fetch the GitHub App PEM private key from AWS Secrets Manager."""
    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]


def mint_installation_token(
    app_id: str,
    private_key: str,
    installation_id: str,
    repo: str | None = None,
) -> str:
    """Create a short-lived GitHub App installation access token.

    Args:
        app_id: GitHub App ID.
        private_key: PEM-encoded RSA private key.
        installation_id: GitHub App installation ID.
        repo: Optional "owner/repo" to scope the token to.

    Returns:
        Installation access token string (~1 hour validity).
    """
    import httpx
    import jwt

    now = int(time.time())
    payload = {
        "iat": now - 60,  # small clock drift buffer
        "exp": now + 600,  # 10 minute JWT (max allowed)
        "iss": app_id,
    }
    encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")

    headers = {
        "Authorization": f"Bearer {encoded_jwt}",
        "Accept": "application/vnd.github+json",
    }

    body: dict = {}
    if repo:
        # Scope token to a specific repository
        _owner, name = repo.split("/", 1)
        body["repositories"] = [name]

    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    response = httpx.post(url, headers=headers, json=body)
    response.raise_for_status()

    return response.json()["token"]
