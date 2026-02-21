"""Scoper agent — explores the codebase and defines the scope for a task."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pydantic_ai import Agent

from lackey.agents._deps import AgentDeps, ToolLog, get_model
from lackey.agents._tools import list_dir, read_file, search_codebase
from lackey.models import ScopeResult

log = logging.getLogger("lackey.agents.scoper")

SCOPER_INSTRUCTIONS = """\
You are a code scoping agent. Your job is to explore a codebase and determine
the minimal set of files needed to complete a task.

You are READ-ONLY. You MUST NOT modify any files.

Use the provided tools to explore the codebase:
- list_dir: List directory contents to understand the project structure.
- read_file: Read file contents to understand code.
- search_codebase: Search for patterns across the codebase.

Start by listing the top-level directory to understand the project structure,
then explore relevant areas based on the task.

Your output must be a structured ScopeResult with:
- summary: A brief description of what needs to change and why.
- allowed_dirs: Directories the executor is allowed to modify files in.
- allowed_files: Specific files the executor is allowed to modify.
- test_files: Test files relevant to this task.
- rationale: A list of reasons explaining why each file/directory is included.

Be precise and minimal. Only include files and directories that are directly
needed for the task. Prefer listing specific files over broad directories.
"""

_scoper_agent = Agent(
    get_model(),
    output_type=ScopeResult,
    instructions=SCOPER_INSTRUCTIONS,
    deps_type=AgentDeps,
    tools=[read_file, list_dir, search_codebase],
    retries=5,
    defer_model_check=True,
)


class ScopeAgent:
    """Scoper agent callable matching the Scoper protocol."""

    def __init__(self, tool_log: ToolLog | None = None) -> None:
        self._tool_log = tool_log

    async def __call__(self, task: str, work_dir: Path) -> ScopeResult:
        log.info("scoper starting: %s", task)
        t0 = time.monotonic()
        result = await _scoper_agent.run(
            f"Task: {task}",
            deps=AgentDeps(work_dir=work_dir, agent_name="scoper", tool_log=self._tool_log),
        )
        elapsed = time.monotonic() - t0
        log.info(
            "scoper done in %.1fs: %d files, %d dirs",
            elapsed,
            len(result.output.allowed_files),
            len(result.output.allowed_dirs),
        )
        log.debug("scope result: %s", result.output.model_dump_json(indent=2))
        return result.output
