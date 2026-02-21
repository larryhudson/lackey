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
    artifact_dir: Path | None = None  # local path to downloaded artifacts
    artifact_s3_prefix: str = ""  # s3 prefix if cloud run
    runtime: str = "local"  # "local" or "cloud"


class RuntimeBackend(Protocol):
    """Protocol for runtime backends that launch lackey runs."""

    def launch(
        self,
        *,
        task: str,
        repo: str,
        run_id: str,
        image: str,
        timeout: int,
    ) -> RunResult:
        """Launch a lackey run and block until completion.

        Args:
            task: Task description for the agent.
            repo: Path to local repo or GitHub org/repo slug.
            run_id: Unique run identifier.
            image: Docker image tag to use.
            timeout: Run timeout in seconds.

        Returns:
            RunResult with outcome and artifact locations.
        """
        ...
