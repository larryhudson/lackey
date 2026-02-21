"""Fixer agent — fixes lint errors and test failures."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pydantic_ai import Agent

from lackey.agents._deps import AgentDeps, ToolLog, get_model
from lackey.agents._tools import (
    edit_file_scoped,
    list_dir,
    read_file,
    run_shell,
    search_codebase,
    write_file_scoped,
)
from lackey.models import ScopeResult

log = logging.getLogger("lackey.agents.fixer")

FIXER_INSTRUCTIONS = """\
You are a code fixing agent. Your job is to fix lint errors or test failures
reported in the failure output.

You MUST stay within the allowed scope. Read the failing files, understand
the errors, and make minimal targeted fixes.

Use the provided tools to:
1. Read the failing files to understand the current code.
2. Edit files using edit_file_scoped (find-and-replace) to make targeted fixes.
   Prefer this over write_file_scoped for existing files.
3. Run shell commands to verify your fixes (e.g., ruff check, pytest).

Keep changes minimal and focused on fixing the reported errors. Do not
refactor or improve unrelated code.
"""

_fixer_agent = Agent(
    get_model(),
    output_type=str,
    instructions=FIXER_INSTRUCTIONS,
    deps_type=AgentDeps,
    tools=[read_file, list_dir, search_codebase, edit_file_scoped, write_file_scoped, run_shell],
    retries=5,
    defer_model_check=True,
)


class FixAgent:
    """Fixer agent callable matching the Fixer protocol."""

    def __init__(self, tool_log: ToolLog | None = None) -> None:
        self._tool_log = tool_log

    async def __call__(self, failure_output: str, work_dir: Path, scope: ScopeResult) -> None:
        log.info("fixer starting (%d chars of failure output)", len(failure_output))
        t0 = time.monotonic()
        deps = AgentDeps(
            work_dir=work_dir,
            scope=scope,
            agent_name="fixer",
            tool_log=self._tool_log,
        )
        scope_info = scope.model_dump_json(indent=2)
        prompt = f"Fix the following failures:\n\n{failure_output}\n\nScope:\n{scope_info}"
        await _fixer_agent.run(prompt, deps=deps)
        elapsed = time.monotonic() - t0
        log.info("fixer done in %.1fs", elapsed)
