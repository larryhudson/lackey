"""RuntimeBackend protocol and RunResult model."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class RunResult(BaseModel):
    """Result returned by a backend after a run completes."""

    run_id: str
    outcome: str  # matches Outcome enum values: success, test_failure, error, etc.
    branch: str = ""
    pr_url: str = ""
    artifact_dir: Path | None = None  # local path to downloaded artifacts
    artifact_s3_prefix: str = ""  # s3 prefix if cloud run
    runtime: str = "local"  # "local" or "cloud"


class RuntimeBackend(Protocol):
    """Protocol for runtime backends that launch lackey runs."""

    def launch(
        self,
        *,
        task: str,
        run_id: str,
        image: str,
        timeout: int,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        """Launch a lackey run and block until completion.

        Args:
            task: Task description for the agent.
            run_id: Unique run identifier.
            image: Docker image tag to use.
            timeout: Run timeout in seconds.
            extra_env: Additional env vars to pass to the container.

        Returns:
            RunResult with outcome and artifact locations.
        """
        ...
