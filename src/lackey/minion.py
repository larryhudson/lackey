"""Compatibility shim — re-exports from blueprint.py.

The blueprint logic has moved to blueprint.py. This module exists only to
avoid breaking existing imports during the transition. It can be deleted once
all callers have been updated.
"""

from __future__ import annotations

from lackey.blueprint import (
    AgentRegistry,
    Executor,
    Fixer,
    Scoper,
    run_blueprint,
)

__all__ = [
    "AgentRegistry",
    "Executor",
    "Fixer",
    "Scoper",
    "run_blueprint",
]
