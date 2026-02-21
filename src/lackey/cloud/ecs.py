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


def _task_id_from_arn(task_arn: str) -> str:
    """Extract the task ID from an ECS task ARN.

    ARN format: arn:aws:ecs:region:account:task/cluster/task-id
    """
    return task_arn.rsplit("/", 1)[-1]


def _tail_logs(
    logs_client,
    log_group: str,
    log_stream: str,
    next_token: str | None,
) -> str | None:
    """Fetch and print new log events. Returns the next forward token."""
    kwargs: dict = {
        "logGroupName": log_group,
        "logStreamName": log_stream,
        "startFromHead": True,
    }
    if next_token:
        kwargs["nextToken"] = next_token

    try:
        response = logs_client.get_log_events(**kwargs)
    except logs_client.exceptions.ResourceNotFoundException:
        # Log stream doesn't exist yet (container hasn't written anything)
        return next_token

    for event in response.get("events", []):
        msg = event["message"].rstrip("\n")
        print(f"  │ {msg}", file=sys.stderr)

    return response.get("nextForwardToken")


def poll_task(
    *,
    cluster: str,
    task_arn: str,
    region: str,
    log_group: str = "/ecs/lackey-minion",
    container_name: str = "minion",
    log_stream_prefix: str = "minion",
    poll_interval: int = 10,
    timeout: int = 900,
) -> dict:
    """Poll an ECS task until STOPPED, streaming CloudWatch logs.

    Prints status updates and container logs to stderr.
    Returns the task description dict.
    Raises TimeoutError if polling exceeds timeout.
    """
    import boto3

    ecs = boto3.client("ecs", region_name=region)
    logs = boto3.client("logs", region_name=region)

    task_id = _task_id_from_arn(task_arn)
    log_stream = f"{log_stream_prefix}/{container_name}/{task_id}"

    deadline = time.monotonic() + timeout
    last_status = ""
    log_token: str | None = None

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

        # Stream logs once the task is RUNNING (or later)
        if status in ("RUNNING", "DEPROVISIONING", "STOPPED"):
            log_token = _tail_logs(logs, log_group, log_stream, log_token)

        if status == "STOPPED":
            # Final log flush
            _tail_logs(logs, log_group, log_stream, log_token)

            exit_code = None
            containers = task.get("containers", [])
            if containers:
                exit_code = containers[0].get("exitCode")
            print(f"  ECS task stopped (exit code: {exit_code})")
            return task

        time.sleep(poll_interval)

    raise TimeoutError(f"ECS task {task_arn} did not stop within {timeout}s")
