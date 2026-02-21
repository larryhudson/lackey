"""CLI entrypoint for running the blueprint inside a container.

Reads configuration from environment variables and calls run_blueprint().
Invoked as: python -m lackey

Environment variables:
    TASK            — the task description (required)
    RUN_ID          — unique run identifier (required)
    WORK_DIR        — working directory with the cloned repo (default: /work)
    OUTPUT_DIR      — directory for artifacts (default: /output)
"""

# ruff: noqa: T201 — print is the correct output mechanism for a CLI entrypoint

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from lackey.models import RunConfig, ScopeResult

# ---------------------------------------------------------------------------
# Stub agents — replaced with real Pydantic AI agents later
# ---------------------------------------------------------------------------


async def _stub_scoper(task: str, work_dir: Path) -> ScopeResult:
    """Stub scoper that allows everything."""
    return ScopeResult(
        summary=f"Stub scope for: {task}",
        allowed_dirs=["."],
        allowed_files=[],
        test_files=[],
        rationale=["Stub scoper — allows all files"],
    )


async def _stub_executor(task: str, scope: ScopeResult, work_dir: Path) -> None:
    """Stub executor that does nothing."""
    return None


async def _stub_fixer(failure_output: str, work_dir: Path, scope: ScopeResult) -> None:
    """Stub fixer that does nothing."""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    debug = bool(os.environ.get("LACKEY_DEBUG"))
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Only show detailed logs for our own code
    logging.getLogger("lackey").setLevel(logging.DEBUG if debug else logging.INFO)

    task = os.environ.get("TASK")
    run_id = os.environ.get("RUN_ID")

    if not task:
        print("ERROR: TASK environment variable is required", file=sys.stderr)
        sys.exit(1)
    if not run_id:
        print("ERROR: RUN_ID environment variable is required", file=sys.stderr)
        sys.exit(1)

    work_dir = Path(os.environ.get("WORK_DIR", "/work"))
    output_dir = Path(os.environ.get("OUTPUT_DIR", "/output"))

    cfg = RunConfig(
        task=task,
        run_id=run_id,
        work_dir=work_dir,
        output_dir=output_dir,
    )

    if os.environ.get("LACKEY_STUBS"):
        scoper, executor, fixer = _stub_scoper, _stub_executor, _stub_fixer
    else:
        from lackey.agents import ExecuteAgent, FixAgent, ScopeAgent, ToolLog

        tool_log = ToolLog(output_dir / "tool_calls.log")
        scoper = ScopeAgent(tool_log=tool_log)
        executor = ExecuteAgent(tool_log=tool_log)
        fixer = FixAgent(tool_log=tool_log)

    from lackey.minion import run_blueprint

    summary = asyncio.run(
        run_blueprint(
            cfg,
            scoper=scoper,
            executor=executor,
            fixer=fixer,
        )
    )

    print(f"Run {summary.run_id} finished: {summary.outcome.value}")
    sys.exit(0 if summary.outcome == "success" else 1)


if __name__ == "__main__":
    main()
