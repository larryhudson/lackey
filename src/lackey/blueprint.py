"""YAML-driven blueprint runner.

Replaces the hardcoded 9-step sequence in minion.py with a generic step-walker
that reads a YAML blueprint and dispatches to built-in step handlers.

Blueprint files live in the target repo at .lackey/blueprint.yaml.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field

from lackey.models import (
    CommandEntry,
    Outcome,
    RunConfig,
    RunSummary,
    ScopeDisagreement,
    ScopeResult,
    StepResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent protocols — same as minion.py, re-exported for convenience
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
# YAML blueprint models
# ---------------------------------------------------------------------------


class StepType(enum.StrEnum):
    GIT_BRANCH = "git_branch"
    GIT_CHECKOUT = "git_checkout"
    AGENT = "agent"
    COMMAND = "command"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    GIT_PR = "git_pr"


class CheckSpec(BaseModel):
    command: str
    artifact: str


class StepSpec(BaseModel):
    name: str
    type: StepType
    # git_branch / git_checkout
    branch: str = ""
    # agent
    agent: str = ""
    input_from: str = ""
    on_scope_disagreement: str = ""
    # command
    commands: list[str] = Field(default_factory=list)
    check: CheckSpec | None = None
    timeout: int = 120
    success_codes: list[int] = Field(default_factory=lambda: [0])
    artifact: str = ""
    # git_commit
    message: str = ""
    # git_pr
    title: str = ""
    # flow control
    when: str = ""
    on_failure: str = ""


class Blueprint(BaseModel):
    name: str
    description: str = ""
    steps: list[StepSpec]


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


@dataclass
class AgentRegistry:
    scoper: Scoper
    executor: Executor
    fixer: Fixer


@dataclass
class RunState:
    cfg: RunConfig
    cmd_log: list[CommandEntry] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    outcome: Outcome = Outcome.SUCCESS
    branch: str = ""
    base_sha: str = ""
    scope: ScopeResult | None = None
    step_results: dict[str, StepResult] = field(default_factory=dict)
    agents: AgentRegistry | None = None
    pr_url: str = ""


# ---------------------------------------------------------------------------
# Helpers (ported from minion.py)
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
# Blueprint loading and discovery
# ---------------------------------------------------------------------------


def load_blueprint(path: Path) -> Blueprint:
    """Parse a YAML blueprint file into a Blueprint model."""
    raw = yaml.safe_load(path.read_text())
    return Blueprint.model_validate(raw)


def discover_blueprint(work_dir: Path) -> Path | None:
    """Find a blueprint file in the work directory or env var.

    Search order:
    1. LACKEY_BLUEPRINT env var (absolute path, relative path, or bare name)
    2. Only .yaml file in .lackey/blueprints/ (if exactly one exists)
    """
    env_path = os.environ.get("LACKEY_BLUEPRINT")
    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            # Bare name like "scope-execute-test" → look in .lackey/blueprints/
            candidate = work_dir / ".lackey" / "blueprints" / env_path
            if not candidate.suffix:
                candidate = candidate.with_suffix(".yaml")
            if candidate.exists():
                return candidate
            # Otherwise treat as relative to work_dir
            p = work_dir / env_path
        if p.exists():
            return p

    # Auto-discover: if there's exactly one blueprint, use it
    blueprints_dir = work_dir / ".lackey" / "blueprints"
    if blueprints_dir.is_dir():
        yamls = sorted(blueprints_dir.glob("*.yaml")) + sorted(blueprints_dir.glob("*.yml"))
        if len(yamls) == 1:
            return yamls[0]
        if len(yamls) > 1:
            names = [y.name for y in yamls]
            logger.warning(
                "Multiple blueprints found: %s — set LACKEY_BLUEPRINT to pick one", names
            )

    return None


# ---------------------------------------------------------------------------
# Template expansion and condition evaluation
# ---------------------------------------------------------------------------


def expand_template(template: str, state: RunState) -> str:
    """Expand {run_id}, {task_slug}, {task}, {env.VAR} in a template string."""

    def _replacer(m: re.Match) -> str:
        key = m.group(1)
        if key == "run_id":
            return state.cfg.run_id
        if key == "task_slug":
            return _slugify(state.cfg.task)
        if key == "task":
            return state.cfg.task
        if key.startswith("env."):
            return os.environ.get(key[4:], "")
        return m.group(0)

    return re.sub(r"\{([^}]+)\}", _replacer, template)


def evaluate_condition(expr: str, state: RunState) -> bool:
    """Evaluate a when-condition.

    Supported forms:
    - "stepname.failed" — true if the named step ran and failed
    - "stepname.succeeded" — true if the named step ran and succeeded
    - "env.VAR" — true if the environment variable is set and non-empty
    """
    if not expr:
        return True

    if expr.startswith("env."):
        var_name = expr[4:]
        return bool(os.environ.get(var_name))

    if "." in expr:
        step_name, _, predicate = expr.partition(".")
        result = state.step_results.get(step_name)
        if result is None:
            return False
        if predicate == "failed":
            return not result.success
        if predicate == "succeeded":
            return result.success

    return False


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------


async def _handle_git_branch(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Create a new branch. Ported from minion.py _step_1_branch."""
    branch_template = spec.branch or "lackey/{run_id}/{task_slug}"
    branch = expand_template(branch_template, state)

    # Get the base SHA before branching
    rc, base_sha = await _run_cmd(
        ["git", "rev-parse", "HEAD"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    state.base_sha = base_sha.strip()

    rc, out = await _run_cmd(
        ["git", "checkout", "-b", branch],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    success = rc == 0
    if success:
        state.branch = branch
    else:
        state.outcome = Outcome.ERROR
    return StepResult(step=step_idx, name=spec.name, success=success, detail=out.strip())


async def _handle_git_checkout(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Check out an existing branch."""
    branch = expand_template(spec.branch, state)

    rc, base_sha = await _run_cmd(
        ["git", "rev-parse", "HEAD"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    state.base_sha = base_sha.strip()

    rc, out = await _run_cmd(
        ["git", "checkout", branch],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    success = rc == 0
    if success:
        state.branch = branch
    else:
        state.outcome = Outcome.ERROR
    return StepResult(step=step_idx, name=spec.name, success=success, detail=out.strip())


async def _handle_agent(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Dispatch to scoper/executor/fixer agent."""
    assert state.agents is not None

    if spec.agent == "scoper":
        scope = await state.agents.scoper(state.cfg.task, state.cfg.work_dir)
        state.scope = scope
        _write_artifact(state.cfg.output_dir, "scope.json", scope.model_dump())
        return StepResult(step=step_idx, name=spec.name, success=True, detail=scope.summary)

    if spec.agent == "executor":
        assert state.scope is not None
        disagreement = await state.agents.executor(state.cfg.task, state.scope, state.cfg.work_dir)
        if disagreement is not None:
            state.outcome = Outcome.SCOPE_DISAGREEMENT
            _write_artifact(
                state.cfg.output_dir,
                "run_summary.json",
                {
                    "outcome": "scope_disagreement",
                    "executor_reasoning": disagreement.executor_reasoning,
                    "suggested_additions": disagreement.suggested_additions,
                },
            )
            return StepResult(
                step=step_idx,
                name=spec.name,
                success=False,
                detail=disagreement.executor_reasoning,
            )
        return StepResult(step=step_idx, name=spec.name, success=True)

    if spec.agent == "fixer":
        assert state.scope is not None
        failure_output = ""
        if spec.input_from:
            prev = state.step_results.get(spec.input_from)
            if prev:
                failure_output = prev.detail
        await state.agents.fixer(failure_output, state.cfg.work_dir, state.scope)
        return StepResult(step=step_idx, name=spec.name, success=True)

    return StepResult(
        step=step_idx, name=spec.name, success=False, detail=f"Unknown agent: {spec.agent}"
    )


async def _handle_command(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Run commands and optional check. Generalized from _step_4_lint / _step_6_test."""
    combined_output = ""

    # Run each command in sequence
    for cmd_str in spec.commands:
        _, out = await _run_cmd(
            ["sh", "-c", cmd_str],
            state.cfg.work_dir,
            step=step_idx,
            log=state.cmd_log,
            timeout=spec.timeout,
        )
        combined_output += out + "\n"

    # Run check command if present
    check_rc = 0
    if spec.check:
        check_rc, check_out = await _run_cmd(
            ["sh", "-c", spec.check.command],
            state.cfg.work_dir,
            step=step_idx,
            log=state.cmd_log,
            timeout=spec.timeout,
        )
        _write_artifact(state.cfg.output_dir, spec.check.artifact, check_out)
        combined_output += check_out + "\n"
        success = check_rc in spec.success_codes
    elif spec.commands:
        # Use the exit code of the last command run
        last_entry = state.cmd_log[-1]
        success = last_entry.exit_code in spec.success_codes
    else:
        success = True

    # Write artifact if specified (for non-check commands like test)
    if spec.artifact and not spec.check:
        _write_artifact(state.cfg.output_dir, spec.artifact, combined_output)

    # Handle on_failure
    if not success and spec.on_failure and spec.on_failure.startswith("outcome:"):
        outcome_name = spec.on_failure.split(":", 1)[1]
        try:
            state.outcome = Outcome(outcome_name)
        except ValueError:
            logger.warning("Unknown outcome in on_failure: %s", outcome_name)

    # Truncate detail for storage
    detail = combined_output.strip()[-2000:]
    return StepResult(step=step_idx, name=spec.name, success=success, detail=detail)


async def _handle_git_commit(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Commit changes. Ported from minion.py _step_9_commit."""
    # Revert out-of-scope changes
    if state.scope:
        rc, status_out = await _run_cmd(
            ["git", "status", "--porcelain"],
            state.cfg.work_dir,
            step=step_idx,
            log=state.cmd_log,
        )

        if status_out.strip():
            allowed = set(state.scope.allowed_files + state.scope.test_files)
            allowed_dirs = [d.rstrip("/") + "/" for d in state.scope.allowed_dirs]

            for line in status_out.strip().splitlines():
                filepath = line[3:].split(" -> ")[-1].strip()
                in_scope = filepath in allowed or any(filepath.startswith(d) for d in allowed_dirs)
                if not in_scope:
                    await _run_cmd(
                        ["git", "restore", filepath],
                        state.cfg.work_dir,
                        step=step_idx,
                        log=state.cmd_log,
                    )

    # Stage and commit
    await _run_cmd(["git", "add", "-A"], state.cfg.work_dir, step=step_idx, log=state.cmd_log)

    rc, status_check = await _run_cmd(
        ["git", "status", "--porcelain"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )

    if not status_check.strip():
        return StepResult(step=step_idx, name=spec.name, success=True, detail="nothing to commit")

    message = expand_template(spec.message or "lackey: {task}", state)
    rc, out = await _run_cmd(
        ["git", "commit", "-m", message],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )

    # Get head SHA
    await _run_cmd(
        ["git", "rev-parse", "HEAD"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )

    # Write diff artifacts
    _, diff_patch = await _run_cmd(
        ["git", "diff", "HEAD~1..HEAD"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    _write_artifact(state.cfg.output_dir, "diff.patch", diff_patch)

    _, diff_stats = await _run_cmd(
        ["git", "diff", "--stat", "HEAD~1..HEAD"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    _write_artifact(state.cfg.output_dir, "diff_stats.txt", diff_stats)

    success = rc == 0
    return StepResult(step=step_idx, name=spec.name, success=success, detail=out.strip())


async def _handle_git_push(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Push the current branch to origin. Non-fatal on failure."""
    rc, out = await _run_cmd(
        ["git", "push", "origin", "HEAD"],
        state.cfg.work_dir,
        step=step_idx,
        log=state.cmd_log,
    )
    success = rc == 0
    if not success:
        logger.warning("git push failed: %s", out.strip())
    return StepResult(step=step_idx, name=spec.name, success=success, detail=out.strip())


async def _handle_git_pr(spec: StepSpec, state: RunState, step_idx: int) -> StepResult:
    """Create a GitHub PR. Wraps cloud.pr.create_pr(). Non-fatal on failure."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("REPO", "")

    if not token or not repo:
        return StepResult(
            step=step_idx,
            name=spec.name,
            success=False,
            detail="GITHUB_TOKEN or REPO not set, skipping PR",
        )

    from lackey.cloud.pr import _build_pr_body, _get_default_branch, create_pr

    title = expand_template(spec.title or "lackey: {task}", state)

    # Build PR body from run summary
    summary_dict = {
        "outcome": state.outcome.value,
        "run_id": state.cfg.run_id,
        "steps": [s.model_dump() for s in state.steps],
    }

    diff_stats_path = state.cfg.output_dir / "diff_stats.txt"
    diff_stats = diff_stats_path.read_text() if diff_stats_path.exists() else ""

    artifact_bucket = os.environ.get("ARTIFACT_BUCKET", "")
    s3_prefix = (
        f"s3://{artifact_bucket}/{state.cfg.run_id}/"
        if artifact_bucket and state.cfg.run_id
        else ""
    )

    body = _build_pr_body(summary_dict, diff_stats, s3_prefix)

    try:
        base = _get_default_branch(repo, token)
        pr_url = create_pr(
            repo=repo,
            token=token,
            head=state.branch,
            base=base,
            title=title,
            body=body,
        )
        if pr_url:
            state.pr_url = pr_url
            return StepResult(step=step_idx, name=spec.name, success=True, detail=pr_url)
        return StepResult(
            step=step_idx, name=spec.name, success=False, detail="PR creation returned None"
        )
    except Exception as exc:
        logger.warning("PR creation failed: %s", exc)
        return StepResult(step=step_idx, name=spec.name, success=False, detail=str(exc))


# ---------------------------------------------------------------------------
# Step dispatcher
# ---------------------------------------------------------------------------

_HANDLERS = {
    StepType.GIT_BRANCH: _handle_git_branch,
    StepType.GIT_CHECKOUT: _handle_git_checkout,
    StepType.AGENT: _handle_agent,
    StepType.COMMAND: _handle_command,
    StepType.GIT_COMMIT: _handle_git_commit,
    StepType.GIT_PUSH: _handle_git_push,
    StepType.GIT_PR: _handle_git_pr,
}


# ---------------------------------------------------------------------------
# Main blueprint runner
# ---------------------------------------------------------------------------


async def run_blueprint(
    cfg: RunConfig,
    blueprint: Blueprint,
    agents: AgentRegistry,
) -> RunSummary:
    """Execute a YAML-defined blueprint step by step."""
    state = RunState(cfg=cfg, agents=agents)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        for idx, spec in enumerate(blueprint.steps, start=1):
            # Evaluate when-condition
            if spec.when and not evaluate_condition(spec.when, state):
                logger.info("Skipping step %s (condition %r not met)", spec.name, spec.when)
                continue

            logger.info("Running step %d: %s (%s)", idx, spec.name, spec.type)

            handler = _HANDLERS.get(spec.type)
            if handler is None:
                result = StepResult(
                    step=idx,
                    name=spec.name,
                    success=False,
                    detail=f"Unknown step type: {spec.type}",
                )
            else:
                result = await handler(spec, state, idx)

            state.steps.append(result)
            state.step_results[spec.name] = result

            # Check for abort conditions
            if state.outcome in (Outcome.ERROR, Outcome.SCOPE_DISAGREEMENT):
                logger.info("Aborting blueprint: outcome=%s", state.outcome.value)
                break

    except TimeoutError:
        state.outcome = Outcome.TIMEOUT
    except Exception as exc:
        state.outcome = Outcome.ERROR
        state.steps.append(StepResult(step=0, name="error", success=False, detail=str(exc)))

    return _finalize(state)


def _finalize(state: RunState) -> RunSummary:
    """Write final artifacts and return the run summary."""
    log_lines = [entry.model_dump_json() for entry in state.cmd_log]
    _write_artifact(state.cfg.output_dir, "commands.log", "\n".join(log_lines) + "\n")

    summary = RunSummary(
        run_id=state.cfg.run_id,
        task=state.cfg.task,
        outcome=state.outcome,
        steps=state.steps,
        branch=state.branch,
        base_sha=state.base_sha,
    )

    # Include PR URL if created
    summary_dict = summary.model_dump()
    if state.pr_url:
        summary_dict["pr_url"] = state.pr_url

    _write_artifact(state.cfg.output_dir, "run_summary.json", summary_dict)

    return summary
