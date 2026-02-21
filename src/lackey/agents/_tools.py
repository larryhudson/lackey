"""Shared tool functions for lackey agents.

All tools receive RunContext[AgentDeps] as their first argument.
Path-traversal protection is enforced on every file operation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from lackey.agents._deps import AgentDeps, ToolCall

log = logging.getLogger("lackey.tools")


def _audit(
    ctx: RunContext[AgentDeps],
    tool: str,
    args: dict[str, str],
    result_summary: str,
    start: float,
) -> None:
    """Record a tool call to the audit log if available."""
    if ctx.deps.tool_log is None:
        return
    ctx.deps.tool_log.record(
        ToolCall(
            agent=ctx.deps.agent_name,
            tool=tool,
            args=args,
            result_summary=result_summary,
            timestamp=start,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    )


def _resolve_path(work_dir: Path, rel_path: str) -> Path:
    """Resolve a path relative to work_dir, blocking traversal attempts."""
    resolved = (work_dir / rel_path).resolve()
    work_resolved = work_dir.resolve()
    if not resolved.is_relative_to(work_resolved):
        raise ModelRetry(f"Path traversal blocked: {rel_path}")
    return resolved


async def read_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Read the contents of a file.

    Args:
        ctx: Agent run context.
        path: Relative path to the file from the working directory.
    """
    t0 = time.monotonic()
    log.debug("read_file: %s", path)
    resolved = _resolve_path(ctx.deps.work_dir, path)
    if not resolved.is_file():
        log.warning("read_file NOT FOUND: %s (resolved to %s)", path, resolved)
        raise ModelRetry(f"File not found: {path}")
    content = resolved.read_text(errors="replace")
    # Record mtime so write/edit tools can enforce read-before-write
    rel = str(resolved.relative_to(ctx.deps.work_dir.resolve()))
    ctx.deps.read_mtimes[rel] = resolved.stat().st_mtime
    if len(content) > 100_000:
        content = content[:100_000] + "\n... (truncated at 100K chars)"
    log.debug("read_file: %s → %d chars", path, len(content))
    _audit(ctx, "read_file", {"path": path}, f"{len(content)} chars", t0)
    return content


def _check_read_before_write(ctx: RunContext[AgentDeps], resolved: Path, rel: str) -> None:
    """Ensure the file was read (and hasn't changed since) before writing."""
    if not resolved.exists():
        return  # New file, no need to read first
    if rel not in ctx.deps.read_mtimes:
        raise ModelRetry(f"You must read_file('{rel}') before editing or writing to it.")
    current_mtime = resolved.stat().st_mtime
    if current_mtime != ctx.deps.read_mtimes[rel]:
        # Clear stale mtime so next read refreshes it
        del ctx.deps.read_mtimes[rel]
        raise ModelRetry(
            f"File '{rel}' has been modified since you last read it. "
            f"Call read_file('{rel}') again before editing."
        )


def _check_scope(ctx: RunContext[AgentDeps], resolved: Path) -> str:
    """Check that a resolved path is within scope. Returns the relative path."""
    rel = str(resolved.relative_to(ctx.deps.work_dir.resolve()))
    scope = ctx.deps.scope
    if scope is None:
        # No scope set — all files are allowed
        return rel

    allowed = set(scope.allowed_files + scope.test_files)
    allowed_dirs = [d.rstrip("/") + "/" for d in scope.allowed_dirs]

    in_scope = rel in allowed or any(rel.startswith(d) for d in allowed_dirs)
    if not in_scope:
        msg = (
            f"File '{rel}' is outside the allowed scope. "
            f"If you need this file, return a ScopeDisagreement instead. "
            f"Allowed files: {scope.allowed_files}, "
            f"Allowed dirs: {scope.allowed_dirs}, "
            f"Test files: {scope.test_files}"
        )
        log.warning("scope check REJECTED: %s", msg)
        raise ModelRetry(msg)
    return rel


async def edit_file_scoped(
    ctx: RunContext[AgentDeps], path: str, old_string: str, new_string: str
) -> str:
    """Edit a file by replacing an exact string match, enforcing scope boundaries.

    This is preferred over write_file_scoped for modifying existing files,
    since it only changes the targeted section rather than rewriting the
    entire file.

    Args:
        ctx: Agent run context.
        path: Relative path to the file from the working directory.
        old_string: The exact text to find in the file (must match uniquely).
        new_string: The replacement text.
    """
    t0 = time.monotonic()
    log.debug("edit_file_scoped: %s", path)
    resolved = _resolve_path(ctx.deps.work_dir, path)
    rel = _check_scope(ctx, resolved)
    _check_read_before_write(ctx, resolved, rel)

    if not resolved.is_file():
        raise ModelRetry(f"File not found: {path}")

    content = resolved.read_text(errors="replace")
    count = content.count(old_string)
    if count == 0:
        raise ModelRetry(
            f"old_string not found in {path}. Make sure the string matches exactly, "
            f"including whitespace and indentation."
        )
    if count > 1:
        raise ModelRetry(
            f"old_string appears {count} times in {path}. "
            f"Provide more surrounding context to make it unique."
        )

    new_content = content.replace(old_string, new_string, 1)
    resolved.write_text(new_content)
    ctx.deps.read_mtimes[rel] = resolved.stat().st_mtime
    summary = f"replaced {len(old_string)} chars → {len(new_string)} chars"
    log.info("edit_file_scoped: edited %s (%s)", rel, summary)
    _audit(ctx, "edit_file_scoped", {"path": path}, summary, t0)
    return f"Edited {rel}: {summary}"


async def write_file_scoped(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Write content to a file, enforcing scope boundaries.

    Use this for creating new files. For modifying existing files, prefer
    edit_file_scoped instead.

    Args:
        ctx: Agent run context.
        path: Relative path to the file from the working directory.
        content: Full file content to write.
    """
    t0 = time.monotonic()
    log.debug("write_file_scoped: %s (%d chars)", path, len(content))
    resolved = _resolve_path(ctx.deps.work_dir, path)
    rel = _check_scope(ctx, resolved)
    _check_read_before_write(ctx, resolved, rel)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    ctx.deps.read_mtimes[rel] = resolved.stat().st_mtime
    summary = f"wrote {len(content)} chars"
    log.info("write_file_scoped: %s (%s)", rel, summary)
    _audit(ctx, "write_file_scoped", {"path": path}, summary, t0)
    return f"Wrote {len(content)} chars to {rel}"


async def bash(ctx: RunContext[AgentDeps], command: str) -> str:
    """Run a shell command in the working directory.

    Use this for searching (grep, rg), listing files (ls, find), running
    tests, linting, or any other shell operation.

    Args:
        ctx: Agent run context.
        command: Shell command to execute.
    """
    t0 = time.monotonic()
    log.debug("bash: %s", command)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=ctx.deps.work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except TimeoutError:
        proc.kill()  # type: ignore[possibly-undefined]
        return "Command timed out after 120 seconds."

    output = raw.decode(errors="replace")
    exit_code = proc.returncode or 0
    if len(output) > 50_000:
        output = output[:50_000] + "\n... (truncated)"
    _audit(ctx, "bash", {"command": command}, f"exit_code={exit_code}", t0)
    return f"Exit code: {exit_code}\n{output}"
