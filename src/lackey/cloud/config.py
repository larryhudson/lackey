"""Cloud configuration loaded from environment variables."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class CloudConfig(BaseModel):
    """All cloud settings, loaded from environment variables."""

    aws_region: str = Field(default="us-east-1")
    ecr_registry: str = Field(description="ECR registry URI")
    ecs_cluster: str = Field(description="ECS cluster name")
    ecs_task_def: str = Field(description="ECS task definition family")
    ecs_subnets: list[str] = Field(description="Subnet IDs for ECS tasks")
    ecs_sg: str = Field(description="Security group ID for ECS tasks")
    artifact_bucket: str = Field(description="S3 bucket for artifacts")
    github_app_id: str = Field(description="GitHub App ID")
    github_app_private_key_secret: str = Field(
        description="Secrets Manager secret name for GitHub App private key"
    )
    github_installation_id: str = Field(description="GitHub App installation ID")
    anthropic_secret: str = Field(description="Secrets Manager secret name for Anthropic API key")

    @classmethod
    def from_env(cls) -> CloudConfig:
        """Load configuration from environment variables."""
        subnets_raw = os.environ.get("LACKEY_ECS_SUBNETS", "")
        subnets = [s.strip() for s in subnets_raw.split(",") if s.strip()]

        return cls(
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            ecr_registry=os.environ["LACKEY_ECR_REGISTRY"],
            ecs_cluster=os.environ["LACKEY_ECS_CLUSTER"],
            ecs_task_def=os.environ["LACKEY_ECS_TASK_DEF"],
            ecs_subnets=subnets,
            ecs_sg=os.environ["LACKEY_ECS_SG"],
            artifact_bucket=os.environ["LACKEY_ARTIFACT_BUCKET"],
            github_app_id=os.environ["LACKEY_GITHUB_APP_ID"],
            github_app_private_key_secret=os.environ["LACKEY_GITHUB_APP_PRIVATE_KEY_SECRET"],
            github_installation_id=os.environ["LACKEY_GITHUB_INSTALLATION_ID"],
            anthropic_secret=os.environ["LACKEY_ANTHROPIC_SECRET"],
        )
