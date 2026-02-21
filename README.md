# Lackey

An unattended coding agent inspired by [Stripe's Minions](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents). Give it a task, it scopes the work, implements it, and pushes a branch — no human in the loop.

## How it works

Lackey runs a fixed **blueprint** that interleaves deterministic steps (branch, lint, test, commit) with bounded agentic steps:

1. **Scope** — A read-only agent explores the codebase and identifies the minimal set of files needed.
2. **Execute** — An implementation agent makes changes, constrained to the scoped files.
3. **Fix** — If lint or tests fail, a fixer agent patches things up (bounded retries).

Each agent gets four tools: `read_file`, `bash`, `edit_file_scoped`, and `write_file_scoped`. Write tools enforce scope boundaries — the executor can't touch files the scoper didn't approve.

## Prerequisites

- Python 3.11+
- Docker
- An Anthropic API key (`ANTHROPIC_API_KEY`)
- For cloud mode: AWS credentials with access to your ECS/ECR/S3/Secrets Manager resources, plus a GitHub App configured for the target repos

## Getting started

```bash
# Install lackey + dev tools (ruff, pytest)
pip install -e . && pip install --dependency-groups dev

# Build the Docker images
make build-base                    # builds minion-base:latest
make build-app                     # builds example image on top (adds ruff + pytest)

# Configure your .env file (see Configuration below)
# LACKEY_REPO=/path/to/your/repo   (local path for local mode, org/repo for cloud)
# LACKEY_IMAGE=minion-example:latest

# Run against a local repo
lackey run "Add a hello() function to src/app.py"

# Run in the cloud (requires LACKEY_* env vars — see Configuration below)
lackey run "Add a hello() function to src/app.py" --cloud
```

Artifacts land in `/tmp/lackey/{run_id}/`: `run_summary.json`, `scope.json`, `diff.patch`, `tool_calls.log`, and more.

## Runtime backends

Lackey has two runtime backends. The blueprint, agents, and tools are identical in both — only the container lifecycle differs.

### Local (default)

Runs in a hardened Docker container on your machine:
- Repo bind-mounted read-only at `/repo`
- `--read-only` filesystem, `--cap-drop=ALL`, `--network none`, non-root user
- Artifacts written to a host-mounted `/output` directory
- Branch pushed to a local bare remote

### Cloud (`--cloud`)

Submits an ECS Fargate task on AWS:
- Image pushed to ECR (cached)
- Short-lived GitHub App token minted for the target repo
- Repo cloned from GitHub inside the container
- Artifacts uploaded to S3, then downloaded by the CLI after completion
- CloudWatch logs streamed to your terminal during the run
- GitHub PR auto-created on successful runs

Cloud infrastructure is provisioned externally (no Terraform/CDK in this repo). You need:
- An **ECR repository** for the container image
- An **ECS Fargate cluster** with a task definition named in `LACKEY_ECS_TASK_DEF`
- An **S3 bucket** for run artifacts
- A **GitHub App** installed on target repos (private key stored in Secrets Manager) with permissions:
  - **Contents**: read & write (clone repo, push branches)
  - **Pull requests**: read & write (create PRs)
  - **Metadata**: read (get default branch)
- **Anthropic API key** stored in Secrets Manager

Populate the `LACKEY_*` env vars (see [Configuration](#cloud-backend-environment-host-side-for---cloud-mode) below) in a `.env` file — the CLI loads it automatically via `python-dotenv`. See the [Cloud Deployment Guide](DEPLOYMENT_GUIDE.md) for step-by-step setup instructions.

## Project structure

```
src/lackey/
  __main__.py          # Container entrypoint (reads env vars, runs blueprint)
  run.py               # Host-side CLI (`lackey run ...`)
  minion.py            # Blueprint orchestrator (9-step sequence)
  models.py            # Pydantic models (ScopeResult, RunConfig, etc.)
  agents/
    _deps.py           # Shared dependencies & audit logging
    _tools.py          # Tool functions (read_file, bash, edit, write)
    scoper.py          # Scoper agent (read-only exploration)
    executor.py        # Executor agent (implementation within scope)
    fixer.py           # Fixer agent (lint/test repair)
  backends/
    base.py            # RuntimeBackend protocol & RunResult
    local.py           # LocalBackend (Docker on host)
    cloud.py           # CloudBackend (ECS/Fargate)
  cloud/
    config.py          # CloudConfig (loaded from env vars)
    ecr.py             # Push images to ECR
    ecs.py             # Launch & poll ECS tasks, stream logs
    s3.py              # Download artifacts from S3
    upload.py          # Upload artifacts to S3
    github_token.py    # Mint GitHub App installation tokens
    pr.py              # Create pull requests
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

### Container environment (inside the Docker container)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TASK` | Yes | — | Task description |
| `RUN_ID` | Yes | — | Unique run identifier |
| `ANTHROPIC_API_KEY` | Yes | — | API key for Claude |
| `LACKEY_MODEL` | No | `anthropic:claude-haiku-4-5` | Model to use |
| `WORK_DIR` | No | `/work` | Working directory |
| `OUTPUT_DIR` | No | `/output` | Artifact output directory |
| `TIMEOUT` | No | `600` | Run timeout in seconds |
| `LACKEY_DEBUG` | No | — | Enable debug logging |

### Host-side environment (`.env` file)

These are read from environment variables or a `.env` file. The CLI loads `.env` automatically. In local mode, the `.env` file is also passed into the Docker container.

| Variable | Required | Description |
|---|---|---|
| `LACKEY_REPO` | Yes | Local path (local mode) or GitHub org/repo slug (cloud mode) |
| `LACKEY_IMAGE` | Yes | Docker image tag to use |
| `ANTHROPIC_API_KEY` | Local only | API key for Claude (passed to container via `.env`) |

### Cloud-only environment (additional `LACKEY_*` vars for `--cloud` mode)

| Variable | Description |
|---|---|
| `LACKEY_ECR_REGISTRY` | ECR registry URI (e.g. `123456789.dkr.ecr.us-east-1.amazonaws.com`) |
| `LACKEY_ECS_CLUSTER` | ECS cluster name |
| `LACKEY_ECS_TASK_DEF` | ECS task definition family |
| `LACKEY_ECS_SUBNETS` | Comma-separated subnet IDs |
| `LACKEY_ECS_SG` | Security group ID for ECS tasks |
| `LACKEY_ARTIFACT_BUCKET` | S3 bucket for run artifacts |
| `LACKEY_GITHUB_APP_ID` | GitHub App ID |
| `LACKEY_GITHUB_APP_PRIVATE_KEY_SECRET` | Secrets Manager secret name for the GitHub App private key |
| `LACKEY_GITHUB_INSTALLATION_ID` | GitHub App installation ID |
| `LACKEY_ANTHROPIC_SECRET` | Secrets Manager secret name for the Anthropic API key |
| `AWS_REGION` | AWS region (default: `us-east-1`) |

## CLI reference

```
lackey run "task description" [options]

Options:
  --cloud         Use cloud backend (ECS/Fargate) instead of local Docker
  --timeout       Run timeout in seconds (default: 600)
```

## License

MIT
