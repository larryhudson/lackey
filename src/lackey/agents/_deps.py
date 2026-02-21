"""Shared dependencies for all lackey agents."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from lackey.models import ScopeResult

DEFAULT_MODEL = "anthropic:claude-haiku-4-5"


def get_model() -> str:
    """Return the model identifier, overridable via LACKEY_MODEL env var."""
    return os.environ.get("LACKEY_MODEL", DEFAULT_MODEL)


@dataclass
class ToolCall:
    """A single tool call record for the audit log."""

    agent: str
    tool: str
    args: dict[str, str]
    result_summary: str
    timestamp: float
    duration_ms: int


class ToolLog:
    """Append-only audit log of all agent tool calls, written as NDJSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: ToolCall) -> None:
        line = json.dumps(
            {
                "agent": entry.agent,
                "tool": entry.tool,
                "args": entry.args,
                "result_summary": entry.result_summary,
                "timestamp": entry.timestamp,
                "duration_ms": entry.duration_ms,
            }
        )
        with self._path.open("a") as f:
            f.write(line + "\n")


@dataclass
class AgentDeps:
    """Dependencies injected into every agent tool call."""

    work_dir: Path
    agent_name: str = ""
    scope: ScopeResult | None = None
    read_mtimes: dict[str, float] = field(default_factory=dict)
    tool_log: ToolLog | None = None
