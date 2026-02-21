# Lackey

An unattended coding agent inspired by [Stripe's Minions](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents). Give it a task, it scopes the work, implements it, and pushes a branch — no human in the loop.

## How it works

Lackey runs a fixed **blueprint** that interleaves deterministic steps (branch, lint, test, commit) with bounded agentic steps:

1. **Scope** — A read-only agent explores the codebase and identifies the minimal set of files needed.
2. **Execute** — An implementation agent makes changes, constrained to the scoped files.
3. **Fix** — If lint or tests fail, a fixer agent patches things up (bounded retries).

Each agent gets four tools: `read_file`, `bash`, `edit_file_scoped`, and `write_file_scoped`. Write tools enforce scope boundaries — the executor can't touch files the scoper didn't approve.

## Quick start

```bash
# Build the base image
docker build -t minion-base:latest .

# Build a project image (adds ruff + pytest)
docker build -t minion-example:latest -f example/Dockerfile.minion .

# Run against a local repo
docker run --rm \
  -v /path/to/your/repo:/repo:ro \
  -v /tmp/lackey-output:/output \
  -e ANTHROPIC_API_KEY=sk-... \
  -e TASK="Add a hello() function to src/app.py" \
  -e RUN_ID="run-$(date +%s)" \
  minion-example:latest
```

Artifacts land in `/output`: `run_summary.json`, `scope.json`, `diff.patch`, `tool_calls.log`, and more.

## Project structure

```
src/lackey/
  __main__.py       # CLI entrypoint (reads env vars, runs blueprint)
  minion.py         # Blueprint orchestrator (9-step sequence)
  models.py         # Pydantic models (ScopeResult, RunConfig, etc.)
  agents/
    _deps.py        # Shared dependencies & audit logging
    _tools.py       # Tool functions (read_file, bash, edit, write)
    scoper.py       # Scoper agent (read-only exploration)
    executor.py     # Executor agent (implementation within scope)
    fixer.py        # Fixer agent (lint/test repair)
```

## Design principles

These are drawn from Stripe's Minions architecture. See [DESIGN.md](DESIGN.md) for the full rationale.

**Blueprint over autonomy.** The agent doesn't choose its own workflow. A fixed blueprint decides when to lint, test, and commit. Agents are only invoked for creative work — understanding code, writing code, and fixing failures. This eliminates entire classes of failure modes: the agent cannot skip linting, forget to commit, or loop indefinitely.

**Scope-then-execute.** Two separate agents, not one. The scoper explores read-only and produces a structured `scope.json` — which files and directories the executor is allowed to touch. The executor is then mechanically bound by that scope. Writes outside scope are rejected. This gives reviewers a contract: "the scoper said these files, the executor touched these files, here's the diff."

**Why not one agent?** A single agent that defines its own scope and enforces it on itself is just an unconstrained agent with extra steps. The separation matters because scope is committed as an artifact *before* any code is written.

**LLM-derived scope, deterministic enforcement.** Deterministic scoping (keywords, import tracing) is brittle — it misses files that are semantically related but have no lexical connection. An LLM that reads the code finds relationships a heuristic never would. But once scope is defined, enforcement is mechanical and trustworthy.

**Scope disagreement as an outcome.** If the executor needs files outside scope, the run exits with a `scope_disagreement` outcome containing the executor's reasoning and suggested additions. No silent failures, no silent scope expansion.

**Isolation first.** Each run gets a Docker container, a fresh branch, and a unique run ID. The container is hardened: read-only filesystem, non-root user, dropped capabilities, global timeout. Mistakes are contained by default.

**Observability everywhere.** Every tool call is logged as NDJSON with agent name, args, timing, and result summary. Every run produces structured artifacts: `scope.json`, `diff.patch`, `run_summary.json`, `tool_calls.log`. If a run fails, you can reconstruct exactly what happened.

**Constrain where predictable, free where creative.** Linting, testing, committing, and isolation are predictable — the blueprint handles them. Understanding code and deciding what to change are not — the agents handle those.

## Configuration

Environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TASK` | Yes | — | Task description |
| `RUN_ID` | Yes | — | Unique run identifier |
| `ANTHROPIC_API_KEY` | Yes | — | API key for Claude |
| `LACKEY_MODEL` | No | `anthropic:claude-haiku-4-5` | Model to use |
| `WORK_DIR` | No | `/work` | Working directory |
| `OUTPUT_DIR` | No | `/output` | Artifact output directory |
| `TIMEOUT` | No | `600` | Run timeout in seconds |

## License

MIT
