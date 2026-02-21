"""ECS Fargate task launch and polling."""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

import sys
import time


def launch_task(
    *,
    cluster: str,
    task_definition: str,
    subnets: list[str],
    security_group: str,
    env_overrides: dict[str, str],
    region: str,
) -> str:
    """Launch a Fargate task and return the task ARN."""
    import boto3

    ecs = boto3.client("ecs", region_name=region)

    env = [{"name": k, "value": v} for k, v in env_overrides.items()]

    response = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": [security_group],
                "assignPublicIp": "ENABLED",
            },
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "minion",
                    "environment": env,
                },
            ],
        },
        count=1,
    )

    failures = response.get("failures", [])
    if failures:
        print(f"ERROR: ECS run_task failed: {failures}", file=sys.stderr)
        raise SystemExit(1)

    task_arn = response["tasks"][0]["taskArn"]
    print(f"Launched ECS task: {task_arn}")
    return task_arn


def poll_task(
    *,
    cluster: str,
    task_arn: str,
    region: str,
    poll_interval: int = 10,
    timeout: int = 900,
) -> dict:
    """Poll an ECS task until STOPPED.

    Prints status updates to stderr. Returns the task description dict.
    Raises TimeoutError if polling exceeds timeout.
    """
    import boto3

    ecs = boto3.client("ecs", region_name=region)

    deadline = time.monotonic() + timeout
    last_status = ""

    while time.monotonic() < deadline:
        response = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])

        if not response["tasks"]:
            print("WARNING: task not found, retrying...", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        task = response["tasks"][0]
        status = task.get("lastStatus", "UNKNOWN")

        if status != last_status:
            print(f"  ECS task status: {status}", file=sys.stderr)
            last_status = status

        if status == "STOPPED":
            exit_code = None
            containers = task.get("containers", [])
            if containers:
                exit_code = containers[0].get("exitCode")
            print(f"  ECS task stopped (exit code: {exit_code})")
            return task

        time.sleep(poll_interval)

    raise TimeoutError(f"ECS task {task_arn} did not stop within {timeout}s")
