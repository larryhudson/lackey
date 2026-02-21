"""Data models for lackey blueprint runs."""

from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field


class Outcome(enum.StrEnum):
    """Possible outcomes of a blueprint run."""

    SUCCESS = "success"
    TEST_FAILURE = "test_failure"
    SCOPE_DISAGREEMENT = "scope_disagreement"
    TIMEOUT = "timeout"
    ERROR = "error"


class ScopeResult(BaseModel):
    """Output of the scoper agent (DESIGN.md §4.2)."""

    summary: str
    allowed_dirs: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)


class ScopeDisagreement(BaseModel):
    """Raised when executor needs files outside scope (DESIGN.md §4.3)."""

    executor_reasoning: str
    suggested_additions: list[str] = Field(default_factory=list)


class CommandEntry(BaseModel):
    """A single entry in commands.log (NDJSON)."""

    step: int
    command: str
    cwd: str
    exit_code: int
    duration_ms: int
    output: str = ""


class StepResult(BaseModel):
    """Result of a single blueprint step."""

    step: int
    name: str
    success: bool
    detail: str = ""


class RunSummary(BaseModel):
    """Final run summary artifact (DESIGN.md §10)."""

    run_id: str
    task: str
    outcome: Outcome
    runtime: str = "local"
    steps: list[StepResult] = Field(default_factory=list)
    branch: str = ""
    base_sha: str = ""
    head_sha: str = ""


class RunConfig(BaseModel):
    """Configuration for a blueprint run."""

    task: str
    work_dir: Path
    output_dir: Path
    run_id: str
    repo_dir: Path | None = None  # source repo (local mode)
    remote_url: str | None = None  # source repo (cloud mode)
    timeout_seconds: int = 600
