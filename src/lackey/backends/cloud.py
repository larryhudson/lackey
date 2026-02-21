"""CloudBackend — runs lackey on ECS/Fargate (DESIGN.md §8.4)."""

# ruff: noqa: T201 — print is used for user-facing status output

from __future__ import annotations

import json
from pathlib import Path

from lackey.backends.base import RunResult
from lackey.cloud.config import CloudConfig
from lackey.cloud.ecr import ensure_image_in_ecr
from lackey.cloud.ecs import launch_task, poll_task
from lackey.cloud.github_token import get_github_app_private_key, mint_installation_token
from lackey.cloud.s3 import download_artifacts


class CloudBackend:
    """Launches lackey runs on ECS/Fargate with ECR, S3, and GitHub App integration."""

    def __init__(self, config: CloudConfig | None = None) -> None:
        self.config = config or CloudConfig.from_env()

    def launch(
        self,
        *,
        task: str,
        run_id: str,
        image: str,
        timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        cfg = self.config
        repo = cfg.repo

        # 1. Push image to ECR
        ensure_image_in_ecr(
            local_tag=image,
            ecr_registry=cfg.ecr_registry,
            repository="lackey-minion",
            region=cfg.aws_region,
        )

        # 2. Mint GitHub App installation token
        print("Minting GitHub App installation token...")
        private_key = get_github_app_private_key(
            cfg.github_app_private_key_secret,
            cfg.aws_region,
        )
        github_token = mint_installation_token(
            app_id=cfg.github_app_id,
            private_key=private_key,
            installation_id=cfg.github_installation_id,
            repo=repo,
        )

        # 3. Fetch Anthropic API key from Secrets Manager
        print("Fetching Anthropic API key from Secrets Manager...")
        import boto3

        sm = boto3.client("secretsmanager", region_name=cfg.aws_region)
        anthropic_key = sm.get_secret_value(SecretId=cfg.anthropic_secret)["SecretString"]

        # 4. Launch ECS task
        env_overrides = {
            "TASK": task,
            "RUN_ID": run_id,
            "TIMEOUT": str(timeout),
            "GITHUB_TOKEN": github_token,
            "REPO": repo,
            "ARTIFACT_BUCKET": cfg.artifact_bucket,
            "ANTHROPIC_API_KEY": anthropic_key,
            "LACKEY_DEBUG": "1",
            **(extra_env or {}),
        }

        task_arn = launch_task(
            cluster=cfg.ecs_cluster,
            task_definition=cfg.ecs_task_def,
            subnets=cfg.ecs_subnets,
            security_group=cfg.ecs_sg,
            env_overrides=env_overrides,
            region=cfg.aws_region,
        )

        # 5. Poll until completion (extra buffer for image pull + clone)
        poll_timeout = timeout + 300
        ecs_task = poll_task(
            cluster=cfg.ecs_cluster,
            task_arn=task_arn,
            region=cfg.aws_region,
            timeout=poll_timeout,
        )

        # 6. Download artifacts from S3
        local_dir = Path(f"/tmp/lackey/{run_id}")
        print(f"Downloading artifacts to {local_dir}...")
        download_artifacts(
            bucket=cfg.artifact_bucket,
            run_id=run_id,
            local_dir=local_dir,
            region=cfg.aws_region,
        )

        # 7. Parse run_summary.json and build result
        s3_prefix = f"s3://{cfg.artifact_bucket}/{run_id}/"
        return self._collect_result(run_id, local_dir, ecs_task, s3_prefix)

    def _collect_result(
        self,
        run_id: str,
        artifact_dir: Path,
        ecs_task: dict,
        s3_prefix: str,
    ) -> RunResult:
        summary_path = artifact_dir / "run_summary.json"

        if summary_path.exists():
            data = json.loads(summary_path.read_text())
            return RunResult(
                run_id=run_id,
                outcome=data.get("outcome", "error"),
                branch=data.get("branch", ""),
                pr_url=data.get("pr_url", ""),
                artifact_dir=artifact_dir,
                artifact_s3_prefix=s3_prefix,
                runtime="cloud",
            )

        # Determine outcome from ECS exit code
        containers = ecs_task.get("containers", [])
        exit_code = containers[0].get("exitCode") if containers else None

        return RunResult(
            run_id=run_id,
            outcome="error" if exit_code != 0 else "success",
            artifact_dir=artifact_dir,
            artifact_s3_prefix=s3_prefix,
            runtime="cloud",
        )
