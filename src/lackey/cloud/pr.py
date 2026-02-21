"""Create a GitHub pull request after a successful cloud run.

Called from entrypoint.sh as: python -m lackey.cloud.pr
Reads GITHUB_TOKEN, REPO, and run artifacts from environment/filesystem.
"""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _get_branch() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _get_default_branch(repo: str, token: str) -> str:
    """Get the default branch from the GitHub API."""
    import httpx

    r = httpx.get(
        f"https://api.github.com/repos/{repo}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    r.raise_for_status()
    return r.json()["default_branch"]


def _build_pr_body(summary: dict, diff_stats: str, s3_prefix: str = "") -> str:
    """Build a markdown PR body from the run summary."""
    outcome = summary.get("outcome", "unknown")
    run_id = summary.get("run_id", "")

    lines = []
    lines.append(f"**Outcome:** {outcome} | **Run ID:** `{run_id}`")
    lines.append("")

    steps = summary.get("steps", [])
    if steps:
        lines.append("### Steps")
        for step in steps:
            check = "x" if step.get("success") else " "
            name = step.get("name", "?")
            detail = step.get("detail", "")

            # Show a short detail snippet for interesting steps
            snippet = ""
            if name == "test" and "passed" in detail:
                # Extract "N passed" from pytest output
                for part in detail.split("\n"):
                    if "passed" in part and "=" in part:
                        snippet = f" — {part.strip().strip('=').strip()}"
                        break
            elif name == "lint" and step.get("success"):
                snippet = " — clean"

            lines.append(f"- [{check}] {name}{snippet}")
        lines.append("")

    if diff_stats.strip():
        lines.append("### Diff")
        lines.append("```")
        lines.append(diff_stats.strip())
        lines.append("```")
        lines.append("")

    if s3_prefix:
        # Build S3 console URL for easy browsing
        # s3://bucket/run_id/ -> https://s3.console.aws.amazon.com/s3/buckets/bucket?prefix=run_id/
        bucket_and_prefix = s3_prefix.removeprefix("s3://")
        bucket, _, prefix = bucket_and_prefix.partition("/")
        region = os.environ.get("AWS_REGION", "us-east-1")
        console_url = (
            f"https://s3.console.aws.amazon.com/s3/buckets/{bucket}?region={region}&prefix={prefix}"
        )
        lines.append("### Artifacts")
        lines.append(f"[View on S3]({console_url}) | `{s3_prefix}`")
        lines.append("")

    lines.append("---")
    lines.append("*Created automatically by [Lackey](https://github.com/larryhudson/lackey)*")

    return "\n".join(lines)


def create_pr(
    *,
    repo: str,
    token: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str | None:
    """Create a pull request via the GitHub API. Returns the PR URL or None on failure."""
    import httpx

    r = httpx.post(
        f"https://api.github.com/repos/{repo}/pulls",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        json={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        },
    )

    if r.status_code == 201:
        pr_url = r.json()["html_url"]
        print(f"  Created PR: {pr_url}")
        return pr_url

    print(f"  WARNING: Failed to create PR (HTTP {r.status_code}): {r.text}", file=sys.stderr)
    return None


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("REPO")

    if not token or not repo:
        print("ERROR: GITHUB_TOKEN and REPO are required", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(os.environ.get("OUTPUT_DIR", "/output"))

    # Load run summary
    summary_path = output_dir / "run_summary.json"
    if not summary_path.exists():
        print("WARNING: No run_summary.json found, skipping PR creation", file=sys.stderr)
        return

    summary = json.loads(summary_path.read_text())

    # Only create PR on success
    if summary.get("outcome") != "success":
        print(f"  Skipping PR creation (outcome: {summary.get('outcome')})")
        return

    # Load diff stats if available
    diff_stats_path = output_dir / "diff_stats.txt"
    diff_stats = diff_stats_path.read_text() if diff_stats_path.exists() else ""

    # Build S3 prefix from env vars
    artifact_bucket = os.environ.get("ARTIFACT_BUCKET", "")
    run_id = summary.get("run_id", "")
    s3_prefix = f"s3://{artifact_bucket}/{run_id}/" if artifact_bucket and run_id else ""

    head = _get_branch()
    base = _get_default_branch(repo, token)
    task = summary.get("task", "lackey task")
    title = f"lackey: {task}"
    body = _build_pr_body(summary, diff_stats, s3_prefix)

    pr_url = create_pr(repo=repo, token=token, head=head, base=base, title=title, body=body)

    # Write PR URL to summary for the host CLI to pick up
    if pr_url:
        summary["pr_url"] = pr_url
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
