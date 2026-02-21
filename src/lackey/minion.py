"""Blueprint orchestrator — the fixed-step sequence from DESIGN.md §3.

The blueprint interleaves deterministic steps (git, lint, test, commit) with
agentic steps (scope, implement, fix). The orchestrator decides *when* to call
agents; agents only do the creative work.

Agent steps are pluggable callables so we can swap stubs for real Pydantic AI
agents later without changing the blueprint.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Protocol

from lackey.models import (
    CommandEntry,
    Outcome,
    RunConfig,
    RunSummary,
    ScopeDisagreement,
    ScopeResult,
    StepResult,
)

# ---------------------------------------------------------------------------
# Agent protocols — what the blueprint expects from each agent
# ---------------------------------------------------------------------------


class Scoper(Protocol):
    async def __call__(self, task: str, work_dir: Path) -> ScopeResult: ...


class Executor(Protocol):
    async def __call__(
        self, task: str, scope: ScopeResult, work_dir: Path
    ) -> ScopeDisagreement | None: ...


class Fixer(Protocol):
    async def __call__(self, failure_output: str, work_dir: Path, scope: ScopeResult) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_cmd(
    cmd: list[str],
    cwd: Path,
    *,
    step: int,
    log: list[CommandEntry],
    timeout: int = 120,
) -> tuple[int, str]:
    """Run a shell command and log it. Returns (exit_code, output)."""
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = raw.decode(errors="replace")
        exit_code = proc.returncode or 0
    except TimeoutError:
        exit_code = -1
        output = f"Command timed out after {timeout}s"
    except FileNotFoundError:
        exit_code = -1
        output = f"Command not found: {cmd[0]}"

    duration_ms = int((time.monotonic() - start) * 1000)

    # Truncate long output for the log
    max_log = 50_000
    logged_output = output[:max_log] + ("..." if len(output) > max_log else "")

    log.append(
        CommandEntry(
            step=step,
            command=" ".join(cmd),
            cwd=str(cwd),
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=logged_output,
        )
    )
    return exit_code, output


def _slugify(task: str) -> str:
    """Turn a task description into a short branch-name-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())
    slug = slug.strip("-")[:50]
    return slug or "task"


def _write_artifact(output_dir: Path, name: str, data: str | dict | list) -> None:
    """Write an artifact to the output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / name
    if isinstance(data, dict | list):
        path.write_text(json.dumps(data, indent=2) + "\n")
    else:
        path.write_text(data)


# ---------------------------------------------------------------------------
# Blueprint steps
# ---------------------------------------------------------------------------


async def _step_1_branch(
    cfg: RunConfig,
    cmd_log: list[CommandEntry],
) -> tuple[StepResult, str]:
    """Create a working branch."""
    slug = _slugify(cfg.task)
    branch = f"minion/{cfg.run_id}/{slug}"

    # Get the base SHA before branching
    rc, base_sha = await _run_cmd(
        ["git", "rev-parse", "HEAD"],
        cfg.work_dir,
        step=1,
        log=cmd_log,
    )
    base_sha = base_sha.strip()

    rc, out = await _run_cmd(
        ["git", "checkout", "-b", branch],
        cfg.work_dir,
        step=1,
        log=cmd_log,
    )
    success = rc == 0
    return StepResult(step=1, name="create_branch", success=success, detail=out.strip()), branch


async def _step_4_lint(
    cfg: RunConfig,
    cmd_log: list[CommandEntry],
) -> StepResult:
    """Run ruff check --fix + ruff format."""
    _, out1 = await _run_cmd(
        ["ruff", "check", "--fix", "."],
        cfg.work_dir,
        step=4,
        log=cmd_log,
    )
    _, out2 = await _run_cmd(
        ["ruff", "format", "."],
        cfg.work_dir,
        step=4,
        log=cmd_log,
    )

    combined = f"ruff check: {out1}\nruff format: {out2}"

    # Re-check to see if anything remains
    rc3, lint_out = await _run_cmd(
        ["ruff", "check", "--output-format=json", "."],
        cfg.work_dir,
        step=4,
        log=cmd_log,
    )

    _write_artifact(cfg.output_dir, "lint_report.json", lint_out)

    success = rc3 == 0
    return StepResult(step=4, name="lint", success=success, detail=combined.strip())


async def _step_6_test(
    cfg: RunConfig,
    cmd_log: list[CommandEntry],
    *,
    step: int = 6,
) -> StepResult:
    """Run pytest."""
    rc, out = await _run_cmd(
        ["pytest", "-x", "--tb=short"],
        cfg.work_dir,
        step=step,
        log=cmd_log,
        timeout=300,
    )

    _write_artifact(cfg.output_dir, "test_output.txt", out)

    success = rc == 0
    return StepResult(step=step, name="test", success=success, detail=out[-2000:])


async def _step_9_commit(
    cfg: RunConfig,
    cmd_log: list[CommandEntry],
    scope: ScopeResult,
    branch: str,
) -> StepResult:
    """Hygiene checks, commit, push, write final artifacts."""
    # Revert out-of-scope changes from shell commands
    rc, status_out = await _run_cmd(
        ["git", "status", "--porcelain"],
        cfg.work_dir,
        step=9,
        log=cmd_log,
    )

    if status_out.strip():
        allowed = set(scope.allowed_files + scope.test_files)
        allowed_dirs = [d.rstrip("/") + "/" for d in scope.allowed_dirs]

        for line in status_out.strip().splitlines():
            # porcelain format: "XY filename" or "XY filename -> newname"
            filepath = line[3:].split(" -> ")[-1].strip()
            in_scope = filepath in allowed or any(filepath.startswith(d) for d in allowed_dirs)
            if not in_scope:
                await _run_cmd(
                    ["git", "restore", filepath],
                    cfg.work_dir,
                    step=9,
                    log=cmd_log,
                )

    # Stage and commit
    await _run_cmd(["git", "add", "-A"], cfg.work_dir, step=9, log=cmd_log)

    rc, status_check = await _run_cmd(
        ["git", "status", "--porcelain"],
        cfg.work_dir,
        step=9,
        log=cmd_log,
    )

    if not status_check.strip():
        return StepResult(step=9, name="commit", success=True, detail="nothing to commit")

    rc, out = await _run_cmd(
        ["git", "commit", "-m", f"lackey: {cfg.task}"],
        cfg.work_dir,
        step=9,
        log=cmd_log,
    )

    # Get head SHA (logged for audit trail)
    await _run_cmd(
        ["git", "rev-parse", "HEAD"],
        cfg.work_dir,
        step=9,
        log=cmd_log,
    )

    # Write diff artifacts
    _, diff_patch = await _run_cmd(
        ["git", "diff", "HEAD~1..HEAD"],
        cfg.work_dir,
        step=9,
        log=cmd_log,
    )
    _write_artifact(cfg.output_dir, "diff.patch", diff_patch)

    _, diff_stats = await _run_cmd(
        ["git", "diff", "--stat", "HEAD~1..HEAD"],
        cfg.work_dir,
        step=9,
        log=cmd_log,
    )
    _write_artifact(cfg.output_dir, "diff_stats.txt", diff_stats)

    success = rc == 0
    return StepResult(step=9, name="commit", success=success, detail=out.strip())


# ---------------------------------------------------------------------------
# Main blueprint
# ---------------------------------------------------------------------------


async def run_blueprint(
    cfg: RunConfig,
    *,
    scoper: Scoper,
    executor: Executor,
    fixer: Fixer,
) -> RunSummary:
    """Execute the 9-step blueprint (DESIGN.md §3).

    Agent callables (scoper, executor, fixer) are injected so the blueprint
    is testable with stubs and swappable for real Pydantic AI agents later.
    """
    cmd_log: list[CommandEntry] = []
    steps: list[StepResult] = []
    outcome = Outcome.SUCCESS
    branch = ""
    base_sha = ""

    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Step 1: Create branch ──────────────────────────────────
        result, branch = await _step_1_branch(cfg, cmd_log)
        steps.append(result)
        if not result.success:
            outcome = Outcome.ERROR
            return _finalize(cfg, cmd_log, steps, outcome, branch, base_sha)

        # Capture base SHA
        _, sha = await _run_cmd(
            ["git", "rev-parse", "HEAD"],
            cfg.work_dir,
            step=1,
            log=cmd_log,
        )
        base_sha = sha.strip()

        # ── Step 2: Scope the task (agentic) ───────────────────────
        scope = await scoper(cfg.task, cfg.work_dir)
        _write_artifact(cfg.output_dir, "scope.json", scope.model_dump())
        steps.append(StepResult(step=2, name="scope", success=True, detail=scope.summary))

        # ── Step 3: Implement the task (agentic) ──────────────────
        disagreement = await executor(cfg.task, scope, cfg.work_dir)
        if disagreement is not None:
            outcome = Outcome.SCOPE_DISAGREEMENT
            _write_artifact(
                cfg.output_dir,
                "run_summary.json",
                {
                    "outcome": "scope_disagreement",
                    "executor_reasoning": disagreement.executor_reasoning,
                    "suggested_additions": disagreement.suggested_additions,
                },
            )
            steps.append(
                StepResult(
                    step=3,
                    name="implement",
                    success=False,
                    detail=disagreement.executor_reasoning,
                )
            )
            return _finalize(cfg, cmd_log, steps, outcome, branch, base_sha)

        steps.append(StepResult(step=3, name="implement", success=True))

        # ── Step 4: Lint + autofix (deterministic) ─────────────────
        lint_result = await _step_4_lint(cfg, cmd_log)
        steps.append(lint_result)

        # ── Step 5: Fix remaining lint errors (agentic, conditional)
        if not lint_result.success:
            await fixer(lint_result.detail, cfg.work_dir, scope)
            # Re-lint after fix
            lint_result_2 = await _step_4_lint(cfg, cmd_log)
            steps.append(
                StepResult(
                    step=5,
                    name="fix_lint",
                    success=lint_result_2.success,
                    detail=lint_result_2.detail,
                )
            )

        # ── Step 6: Run tests (deterministic) ─────────────────────
        test_result = await _step_6_test(cfg, cmd_log, step=6)
        steps.append(test_result)

        # ── Step 7: Fix test failures (agentic, conditional) ──────
        if not test_result.success:
            await fixer(test_result.detail, cfg.work_dir, scope)
            steps.append(StepResult(step=7, name="fix_tests", success=True))

            # ── Step 8: Run tests round 2 (deterministic, final) ──
            test_result_2 = await _step_6_test(cfg, cmd_log, step=8)
            steps.append(test_result_2)

            if not test_result_2.success:
                outcome = Outcome.TEST_FAILURE

        # ── Step 9: Commit + push + emit artifacts (deterministic) ─
        commit_result = await _step_9_commit(cfg, cmd_log, scope, branch)
        steps.append(commit_result)

    except TimeoutError:
        outcome = Outcome.TIMEOUT
    except Exception as exc:
        outcome = Outcome.ERROR
        steps.append(StepResult(step=0, name="error", success=False, detail=str(exc)))

    return _finalize(cfg, cmd_log, steps, outcome, branch, base_sha)


def _finalize(
    cfg: RunConfig,
    cmd_log: list[CommandEntry],
    steps: list[StepResult],
    outcome: Outcome,
    branch: str,
    base_sha: str,
) -> RunSummary:
    """Write final artifacts and return the run summary."""
    # Write commands.log (NDJSON)
    log_lines = [entry.model_dump_json() for entry in cmd_log]
    _write_artifact(cfg.output_dir, "commands.log", "\n".join(log_lines) + "\n")

    summary = RunSummary(
        run_id=cfg.run_id,
        task=cfg.task,
        outcome=outcome,
        steps=steps,
        branch=branch,
        base_sha=base_sha,
    )
    _write_artifact(cfg.output_dir, "run_summary.json", summary.model_dump())

    return summary
