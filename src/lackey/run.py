"""Host-side CLI for launching lackey runs.

Usage:
    lackey run "task description" --repo ./path-or-org/repo
    lackey run "task description" --repo org/repo --cloud

This is the *host* entry point — it selects a backend (local Docker or cloud
ECS/Fargate) and delegates the actual work to the container.
"""

# ruff: noqa: T201 — print is the correct output mechanism for a CLI

from __future__ import annotations

import argparse
import sys
import uuid


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lackey",
        description="Launch an unattended coding agent run.",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run a task against a repository")
    run_p.add_argument("task", help="Task description for the agent")
    run_p.add_argument(
        "--repo",
        required=True,
        help="Path to local repo or GitHub org/repo slug",
    )
    run_p.add_argument(
        "--image",
        default="minion-base:latest",
        help="Docker image tag (default: minion-base:latest)",
    )
    run_p.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Run timeout in seconds (default: 600)",
    )
    run_p.add_argument(
        "--run-id",
        default=None,
        help="Run ID override (default: generated UUID)",
    )
    run_p.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file to pass into the container",
    )
    run_p.add_argument(
        "--cloud",
        action="store_true",
        help="Use cloud backend (ECS/Fargate) instead of local Docker",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv

    load_dotenv()

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command != "run":
        parser.print_help()
        sys.exit(1)

    run_id = args.run_id or str(uuid.uuid4())

    if args.cloud:
        from lackey.backends.cloud import CloudBackend

        backend = CloudBackend()
    else:
        from lackey.backends.local import LocalBackend

        backend = LocalBackend(env_file=args.env_file)

    result = backend.launch(
        task=args.task,
        repo=args.repo,
        run_id=run_id,
        image=args.image,
        timeout=args.timeout,
    )

    print()
    print(f"Run {result.run_id} finished: {result.outcome}")
    print(f"  runtime: {result.runtime}")
    if result.branch:
        print(f"  branch:  {result.branch}")
    if result.pr_url:
        print(f"  pr:      {result.pr_url}")
    if result.artifact_dir:
        print(f"  artifacts: {result.artifact_dir}")
    if result.artifact_s3_prefix:
        print(f"  s3: {result.artifact_s3_prefix}")

    sys.exit(0 if result.outcome == "success" else 1)


if __name__ == "__main__":
    main()
