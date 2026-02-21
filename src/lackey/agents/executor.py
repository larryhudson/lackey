"""Executor agent — implements the task within scope boundaries."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from pydantic_ai import Agent

from lackey.agents._deps import AgentDeps, ToolLog, get_model
from lackey.agents._tools import (
    bash,
    edit_file_scoped,
    read_file,
    write_file_scoped,
)
from lackey.models import ScopeDisagreement, ScopeResult

log = logging.getLogger("lackey.agents.executor")

EXECUTOR_INSTRUCTIONS = """\
You are a code implementation agent. Your job is to implement the requested
task within the scope boundaries defined by the scoper agent.

You MUST stay within the allowed files, directories, and test files defined
in the scope. If you attempt to write a file outside the scope, the tool will
reject it.

Use the provided tools to:
1. Read relevant files to understand the existing code.
2. Use bash to search (grep, rg), list files (ls, find), or run commands.
3. Edit existing files using edit_file_scoped (find-and-replace). This is
   preferred over write_file_scoped for modifications since it only changes
   the targeted section.
4. Create new files using write_file_scoped.

When you're done implementing successfully, return a brief summary string
describing what you changed.

If you determine that the scope is too narrow and you need files outside it,
return a ScopeDisagreement with:
- executor_reasoning: Why the current scope is insufficient.
- suggested_additions: List of files or directories to add.

Keep changes minimal and focused on the task. Do not refactor unrelated code.
"""

_executor_agent = Agent(
    get_model(),
    output_type=ScopeDisagreement | str,  # type: ignore[arg-type]
    instructions=EXECUTOR_INSTRUCTIONS,
    deps_type=AgentDeps,
    tools=[read_file, bash, edit_file_scoped, write_file_scoped],
    retries=5,
    defer_model_check=True,
)


class ExecuteAgent:
    """Executor agent callable matching the Executor protocol."""

    def __init__(self, tool_log: ToolLog | None = None) -> None:
        self._tool_log = tool_log

    async def __call__(
        self, task: str, scope: ScopeResult | None, work_dir: Path
    ) -> ScopeDisagreement | None:
        log.info("executor starting: %s", task)
        t0 = time.monotonic()
        deps = AgentDeps(
            work_dir=work_dir,
            scope=scope,
            agent_name="executor",
            tool_log=self._tool_log,
        )
        if scope is not None:
            scope_info = scope.model_dump_json(indent=2)
            prompt = f"Task: {task}\n\nScope:\n{scope_info}"
        else:
            prompt = f"Task: {task}\n\nNo scope restrictions — all files are writable."
        result = await _executor_agent.run(prompt, deps=deps)
        elapsed = time.monotonic() - t0
        if isinstance(result.output, ScopeDisagreement):
            log.info("executor done in %.1fs: scope disagreement", elapsed)
            return result.output
        log.info("executor done in %.1fs: success", elapsed)
        return None
