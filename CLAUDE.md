# CLAUDE.md — Lackey

Unattended coding agent inspired by Stripe's Minions. See [README.md](README.md) for overview, [DESIGN.md](DESIGN.md) for full architectural rationale.

## Key concepts

- **Blueprint** (`minion.py`): fixed 9-step sequence mixing deterministic steps (git, lint, test, commit) with agentic steps (scope, implement, fix). Agents can't skip steps or loop.
- **Scope-then-execute**: scoper agent (read-only) emits `scope.json`, executor is mechanically bound by it. Scope disagreement is an explicit outcome, not a silent failure.
- **Two backends**: local Docker (`backends/local.py`) and cloud ECS/Fargate (`backends/cloud.py`). The blueprint is identical in both.

## Where things live

| What | Where |
|---|---|
| Host-side CLI | `run.py` — `lackey run "task" [--cloud]` |
| Container entrypoint | `__main__.py` — reads env vars, runs blueprint |
| Blueprint orchestrator | `minion.py` — the 9-step sequence |
| Data models | `models.py` — `ScopeResult`, `RunConfig`, `RunSummary`, `Outcome` |
| Agent definitions | `agents/scoper.py`, `agents/executor.py`, `agents/fixer.py` |
| Agent tools | `agents/_tools.py` — `read_file`, `bash`, `edit_file_scoped`, `write_file_scoped` |
| Agent deps & logging | `agents/_deps.py` — `AgentDeps`, `ToolLog`, `get_model()` |
| Backend protocol | `backends/base.py` — `RuntimeBackend` protocol, `RunResult` |
| Cloud infra | `cloud/` — `ecr.py`, `ecs.py`, `s3.py`, `upload.py`, `github_token.py`, `pr.py`, `config.py` |
| Docker image | `Dockerfile` (base), `example/Dockerfile.minion` (project) |
| Build helpers | `Makefile` — `build-base`, `build-app`, `push`, `build-and-push` |

## Agents

All use Pydantic AI with model from `LACKEY_MODEL` env var (default `anthropic:claude-haiku-4-5`).

- **Scoper**: read-only (`read_file`, `bash`). Produces `ScopeResult`.
- **Executor**: writes within scope (`edit_file_scoped`, `write_file_scoped`). Returns `ScopeDisagreement` if scope is too narrow.
- **Fixer**: same tools as executor, scoped to already-modified files + test dirs.

Scope enforcement and read-before-write tracking happen in `_tools.py`. Audit logging via `_deps.py`.

## Cloud mode

Cloud backend (`backends/cloud.py`) orchestrates: ECR push, GitHub App token minting, ECS task launch, CloudWatch log streaming, S3 artifact download, PR creation.

Config loaded from `LACKEY_*` env vars — see `cloud/config.py` and the README for the full list.

## Artifacts

Every run emits to `/output` (local) or S3 (cloud): `run_summary.json`, `scope.json`, `diff.patch`, `diff_stats.txt`, `commands.log`, `tool_calls.log`, `lint_report.json`, `test_output.txt`.

## Development

```bash
# Lint and format
ruff check --fix . && ruff format .

# Build and test Docker images
make build-base       # builds minion-base:latest
make build-app        # builds example image on top of base
make build-and-push   # build + push to ECR

# Run locally via CLI (set LACKEY_REPO and LACKEY_IMAGE in .env)
lackey run "task"
```

No test suite yet — `pytest` is a dev dependency but `tests/` doesn't exist.

## Code style

- **Line length**: 100 chars
- **`from __future__ import annotations`** at the top of every module
- **Print statements are banned** by ruff (`T20` rule). Use `logging` for diagnostics. CLI-facing output files get `# ruff: noqa: T201` at the top.
- **`ModelRetry`** from pydantic-ai for agent-recoverable errors in tools (bad path, scope violation, etc.). Regular exceptions for system failures.
- **All tool functions are async**, take `ctx: RunContext[AgentDeps]` as first arg, and audit-log themselves.
- Agents are injected into the blueprint as **protocol callables** — keep this so stubs can be swapped in.

## Design docs

- `DESIGN.md` — full architectural rationale, container hardening, data flow diagrams
- `history/` — AI-generated planning documents (ephemeral, not in version control)
