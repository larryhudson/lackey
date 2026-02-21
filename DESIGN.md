# Toy Minion: Design Document

*An unattended coding agent architecture inspired by Stripe's Minions*

---

**Purpose:** The simplest implementation that captures the core architectural principles from Stripe's Minions system, which produces over 1,300 merged PRs per week with no human-written code.

**Source material:** Stripe Engineering blog — "How We Built Minions" (Parts 1 & 2)

**Status:** v8 — adds dual-mode runtime (local Docker + cloud ECS/Fargate) with pluggable backends and parallel run support. Simplifies the cloud isolation model: single container with network access, toolshed as a capability boundary (not a network boundary). The blueprint, agents, and toolshed internals are unchanged from v6.

---

## 1. Overview

Stripe's Minions work because of several reinforcing architectural decisions: isolated environments, a blueprint that mixes deterministic and agentic steps, a centralized tool server with curated access, and bounded iteration. Our toy version captures these patterns using Pydantic AI for the agent framework, FastMCP for the tool server, and Docker for isolation.

The system has four layers:

- **run.py** — the host-side orchestrator with pluggable runtime backends (local Docker or cloud ECS/Fargate)
- **Dockerfile + entrypoint.sh** — the isolated environment where minions execute (same image for both modes)
- **minion.py** — the blueprint orchestrator that interleaves deterministic and agentic steps
- **toolshed.py** — a centralized MCP tool server with curated access profiles

The two things that matter most for making unattended agents viable are **observability** (so you can debug failures) and **isolation** (so mistakes are contained). Get those right and you can start simple on everything else.

Every run gets a unique run ID (UUID). Artifact paths, branch names, and logs are all keyed by this ID, so multiple runs can execute in parallel with no shared state.

## 2. Runtime Backends

The orchestrator (`run.py`) delegates container lifecycle to a pluggable backend. The backend is responsible for launching the container, providing the repo, and collecting artifacts. Everything inside the container — the blueprint, agents, and toolshed — is identical regardless of backend.

### 2.1 The Abstraction

```python
class RuntimeBackend:
    def launch(self, task: str, repo: str, config: RunConfig) -> RunResult:
        """Launch a minion run. Returns artifacts and outcome."""
        ...
```

`RunConfig` includes: run ID, toolshed profile, image tag, timeout, and backend-specific options. `RunResult` includes: outcome status, artifact paths (local or S3), branch name, and timing.

### 2.2 LocalBackend

The local backend runs a Docker container on the host machine.

- Builds the image if needed (cached)
- Bind-mounts the repo read-only at `/repo`
- Bind-mounts an output directory at `/output`
- Starts the host-side Toolshed server on a Unix socket, mounted into the container
- Blocks until the container exits
- Artifacts are on the host filesystem

This is the fast path for development and single-repo use.

### 2.3 CloudBackend

The cloud backend submits an ECS Fargate task — a single container running on AWS-managed infrastructure with no servers to maintain.

**What ECS/Fargate does:** You define a task (which image, how much CPU/memory, what secrets to inject, what network rules). You call `run-task`. AWS finds a machine, pulls your image, starts the container, and runs it. When it exits, AWS cleans up. You pay per-second for compute while the task is running.

The cloud backend flow:

- Pushes the image to ECR (if not already there)
- Mints a short-lived GitHub App installation token for the target repo
- Calls `ecs.run_task()` with per-run overrides: task description, repo URL, run ID, secrets
- Polls `ecs.describe_tasks()` every few seconds, printing status updates
- When the task stops, fetches artifacts from S3

The CLI blocks while polling. If the CLI is interrupted, the ECS task continues running — artifacts still land in S3 and the branch still gets pushed. You can retrieve them later.

### 2.4 Run IDs and Parallelism

Every run gets a UUID assigned by the orchestrator before launch. This ID flows into:

- **Branch name:** `minion/{run-id}/{slug}` (e.g., `minion/a1b2c3d4/fix-auth-bypass`)
- **Artifact path:** local `/output/{run-id}/` or S3 `s3://{bucket}/{run-id}/`
- **Log correlation:** all structured logs include the run ID

Because no state is shared between runs, multiple runs can execute in parallel — locally (multiple `docker run` invocations) or in the cloud (multiple ECS tasks). The orchestrator does not coordinate between runs; each is fully independent.

## 3. The Blueprint

The blueprint is a fixed sequence of steps alternating deterministic code and agentic LLM calls. The orchestrator, not the agent, decides when to lint, test, and commit. The agents are only invoked for creative work.

| # | Step | Type | Purpose |
|---|------|------|---------|
| 1 | Create branch + clone | Deterministic | Predictable setup |
| 2 | Scope the task | Agentic (scoper) | Explore codebase, define boundaries |
| 3 | Implement the task | Agentic (executor) | The creative work, within scoper's boundaries |
| 4 | Run linters + autofix | Deterministic | Cheap feedback before tests |
| 5 | Fix remaining lint errors | Agentic (fixer, if needed) | Only if linters still fail |
| 6 | Run tests | Deterministic | Full pytest suite |
| 7 | Fix test failures | Agentic (fixer, if needed) | Only if tests fail |
| 8 | Run tests (round 2, final) | Deterministic | Last chance — then hand back to human |
| 9 | Commit + push + emit artifacts | Deterministic | Clean tree, artifacts, fingerprint |

**Tradeoff:** Less flexible than a fully agentic loop. But it eliminates entire classes of failure modes — the agent cannot skip linting, cannot forget to commit, and cannot loop indefinitely. Stripe found that "putting LLMs into contained boxes compounds into system-wide reliability upside."

The blueprint is runtime-agnostic. It runs the same steps in local and cloud mode. The only difference is how step 1 (clone) and step 9 (push + artifacts) interact with the outside world, and that's handled by the toolshed and entrypoint, not the blueprint itself.

## 4. Scoper / Executor Design

### 4.1 Why Two Agents

The scoper and executor exist because scope control is valuable but scope *selection* requires understanding the code. A deterministic gate (keyword search, AST import tracing) is brittle — it misses files that are semantically related but have no lexical or import connection. An LLM that actually reads the code can find the rate limiter that "fix the auth bypass" requires.

Separating the two agents gives us:

- **Inspectable scope** — the scoper's output is a structured artifact (`scope.json`) that a human or the executor can review before any code is written
- **Deterministic enforcement** — once the scope is defined by an agent that understands the code, enforcement is mechanical and trustworthy
- **A natural feedback loop** — if the executor disagrees with the scope, the run exits with the executor's reasoning, and a new run can start with adjusted boundaries

### 4.2 The Scoper

The scoper is a read-only agent. It explores the codebase, reads files, searches documentation, and produces a structured scope definition. It has no write tools — it cannot modify code.

**Inputs:**
- Task description
- Repository access (read-only)
- Toolshed access (read-only tools: `search_docs`, `search_codebase`)

**Output:** `scope.json`

```json
{
  "summary": "The auth bypass is in login.py but the actual session validation happens in middleware/auth.py. The rate limiter in middleware/rate_limit.py also needs updating because it bypasses auth checks for cached sessions.",
  "allowed_dirs": ["src/auth/", "src/middleware/", "tests/auth/", "tests/middleware/"],
  "allowed_files": [
    "src/auth/login.py",
    "src/auth/session_manager.py",
    "src/middleware/auth.py",
    "src/middleware/rate_limit.py"
  ],
  "test_files": [
    "tests/auth/test_login.py",
    "tests/middleware/test_auth.py",
    "tests/middleware/test_rate_limit.py"
  ],
  "rationale": [
    "login.py — entry point for auth flow, contains the bypass",
    "session_manager.py — imported by login.py, manages session tokens",
    "middleware/auth.py — validates sessions on each request",
    "middleware/rate_limit.py — has a code path that skips auth for cached sessions"
  ]
}
```

The `rationale` field is key — it makes the scoper's reasoning inspectable. If a run fails because scope was too narrow, you can read the rationale and understand why.

### 4.3 The Executor

The executor implements the task within the scoper's boundaries. It has full file tools (read, write, shell) but writes are checked against the scope.

**Scope enforcement:**
- `write_file` checks the target path against `scope.json` allowed files/dirs. Writes outside scope are rejected with an error message.
- `run_shell` — no pre-enforcement (too many edge cases). The hygiene step checks for out-of-scope changes before commit and reverts them with `git restore`.
- Test files referenced in `scope.json` are always writable. The executor can also create new test files within allowed directories.

**If the executor disagrees:** If the executor determines it needs to modify files outside the defined scope, it cannot do so. Instead, the run completes with a special outcome status in `run_summary.json`:

```json
{
  "outcome": "scope_disagreement",
  "executor_reasoning": "The auth bypass also affects the OAuth callback handler in src/oauth/callback.py, which validates sessions using the same flawed logic. This file is not in scope.",
  "suggested_additions": ["src/oauth/callback.py", "tests/oauth/test_callback.py"]
}
```

The engineer can then re-run the minion with the expanded scope, or handle it manually. This is better than either silently failing or silently expanding scope.

### 4.4 Why Not One Agent That Scopes and Executes?

A single agent that defines its own scope and enforces it on itself is just an unconstrained agent with extra steps. The separation matters because the scoper's output is committed as an artifact *before* any code is written, and the executor is mechanically bound by it. This gives the human reviewer a contract: "the scoper said these files, the executor touched these files, here's the diff."

## 5. Rule Discovery

Agent rule files (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules/*`) are discovered as a side effect of the agent reading code, not predicted upfront.

The `read_file` tool is instrumented so that when the agent reads a file, the tool automatically walks from that file's directory up to the repo root, collecting any rule files found along the way. New rules are prepended to the file content returned to the agent, with the most specific (closest to the file) appearing first.

A run-scoped `seen_rules: set[Path]` ensures each rule file is injected only once per run. If the agent reads three files under `services/todo_service/`, the `CLAUDE.md` in that directory is prepended only on the first read.

The agent never knows this is happening — it just gets richer context naturally as it explores the codebase. This matches how Claude Code and Cursor handle rule discovery. Both the scoper and executor benefit from this — the scoper picks up rules as it explores, and the executor picks up rules as it implements.

## 6. The Toolshed

### 6.1 Architecture

All agent-callable tools live in a single FastMCP server. Each agent run receives a curated subset via Pydantic AI's `PreparedToolset` — tools are genuinely absent from the API call, not hidden by prompt instructions.

| Profile | Categories | Use Case |
|---|---|---|
| default | docs, code_intel | Standard coding tasks |
| oncall | docs, code_intel, tickets, observability | On-call triage |
| migration | docs, code_intel, feature_flags | Code migrations |
| full | All categories | Unrestricted |

### 6.2 Tool Trust Boundaries

| Class | Examples | Policy |
|---|---|---|
| Read-only | `search_docs`, `search_codebase`, `get_ticket` | Available by default |
| Write | `update_ticket` | Requires explicit profile opt-in |
| Destructive | (none currently) | Forbidden; mocked in toy version |

Every tool call is audit-logged in `commands.log`.

### 6.3 The Toolshed as Capability Boundary

The toolshed is a **capability boundary**, not a network boundary. The container has network access (see §8), but the agent's tools for interacting with external services are mediated through MCP. The agent can't `curl` the GitHub API or query Jira directly — those aren't tools it has. It calls `search_codebase`, `get_ticket`, or `search_docs` through the toolshed.

This gives us the "single auditable channel" property: every external interaction the agent initiates during agentic steps is a toolshed call, logged in `commands.log`, and controllable via tool profiles.

Deterministic steps (git clone, git push, S3 upload, linting, testing) use the network directly — they're scripted by `entrypoint.sh` and `minion.py`, not driven by the LLM. The toolshed doesn't gate these because they're not agent-initiated.

The app-under-development (e.g., a FastAPI server, a Vite frontend, headless Chromium for testing) also uses the network normally. If the app needs to call external APIs, that's configured at the application level (`.env`, config files, etc.) and is the app's concern, not the agent's.

```
┌─────────────────────────────────────────────┐
│  CONTAINER                                  │
│                                             │
│  ┌─────────────────────────────────┐        │
│  │  Agentic steps (LLM-driven)    │        │
│  │                                 │        │
│  │  Agent ──MCP──→ Toolshed       │        │
│  │  (search_docs, get_ticket...)  │        │
│  │                                 │        │
│  │  Capability boundary:           │        │
│  │  agent can only use MCP tools   │        │
│  │  for external interactions      │        │
│  └─────────────────────────────────┘        │
│                                             │
│  ┌─────────────────────────────────┐        │
│  │  Deterministic steps (scripted) │        │
│  │                                 │        │
│  │  git clone / git push           │        │
│  │  ruff / pytest                  │        │
│  │  S3 upload (cloud mode)         │        │
│  │                                 │        │
│  │  Uses network directly          │        │
│  └─────────────────────────────────┘        │
│                                             │
│  ┌─────────────────────────────────┐        │
│  │  App-under-development          │        │
│  │                                 │        │
│  │  FastAPI, Vite, Chromium, etc.  │        │
│  │  Uses network per app config    │        │
│  └─────────────────────────────────┘        │
│                                             │
└─────────────────────────────────────────────┘
```

#### Local Mode: Toolshed on Host

In local mode, the Toolshed server runs on the host with full network access. The container communicates through a mounted Unix domain socket. This lets the container run with `--network none` while still giving the agent access to external data sources via MCP.

```
HOST:  Toolshed server → /tmp/toolshed.sock
       │
CONTAINER (--network none):
       /mcp.sock (mounted from host)
```

For the toy implementation where tools return mock data, the Toolshed runs in-process inside the container.

#### Cloud Mode: Toolshed In-Process

In cloud mode, the container has network access (restricted by security groups). The Toolshed runs in-process — no Unix socket or sidecar needed. It makes network calls to external services directly from inside the container.

The simplification: because the container has network, we don't need a separate process or container to proxy external calls. The Toolshed is just a library that the agent calls via MCP in-process.

## 7. Agent Design

Three agent roles, all the same underlying agent with different configurations:

| | Scoper (step 2) | Executor (step 3) | Fixer (steps 5, 7) |
|---|---|---|---|
| Invoked | Once | Once | Zero or more times |
| Context | Task description + repo | Task + `scope.json` + repo | Diff + failure output |
| File tools | **read, list, shell (read-only)** | read, write, list, shell | read, write, list, shell |
| Toolshed (MCP) tools | Read-only | Full profile for run type | Read-only |
| Write scope | None — read only | Bound by `scope.json` | Previously touched files + test dirs |
| Output | `scope.json` | Code changes | Code fixes |

The scoper is strictly read-only. The executor is bound by the scoper's output. The fixer operates on files already touched by the executor plus test directories.

Agents do not know which runtime backend is in use. They interact with the repo via local file tools and with external services via MCP tools. The toolshed runs in-process in both modes — the agent doesn't know or care whether it's in a local Docker container or an ECS Fargate task.

## 8. Container Isolation

### 8.1 Two-Layer Design

The container image is the same for local and cloud. What changes is how the repo, artifacts, and toolshed are wired in.

**Local mode:**

| Layer | Contains | Rebuilt When |
|---|---|---|
| Docker image | Python, pip deps, ruff, pytest, minion.py, toolshed.py | Tooling or deps change |
| Container `/repo` | Target repo, bind-mounted read-only from host | Never — always current |
| Container `/work` | Writable clone of `/repo` (tmpfs, sized) | Every run |
| Container `/output` | Artifacts, bind-mounted to host | Every run |

**Cloud mode:**

| Layer | Contains | Rebuilt When |
|---|---|---|
| ECR image | Same image as local | Pushed by CI on merge |
| Container `/work` | Writable clone from GitHub (no `/repo` mount) | Every run (cloned at startup) |
| Container `/output` | Artifacts, uploaded to S3 at end of run | Every run |

In cloud mode there is no `/repo` bind mount. The entrypoint detects this and clones from the remote directly (the container has network access and git credentials):

```bash
if [ -d /repo ]; then
    # Local mode: repo is bind-mounted
    git clone /repo /work
else
    # Cloud mode: clone from remote
    git clone "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" /work
fi
```

### 8.2 Container Hardening

#### Local

| Constraint | Flag | Purpose |
|---|---|---|
| No network (TCP/IP) | `--network none` | Prevent internet access |
| Toolshed socket | `-v /tmp/toolshed.sock:/mcp.sock` | MCP access without network |
| Read-only filesystem | `--read-only` | Prevent writes outside designated areas |
| Writable work area | `--tmpfs /work:size=4g` | Explicit size cap |
| Writable temp | `--tmpfs /tmp:size=1g` | For pip/pytest temp files |
| Writable output | `-v /output` bind mount | Artifact extraction |
| Drop all capabilities | `--cap-drop=ALL` | Minimize kernel attack surface |
| No privilege escalation | `--security-opt=no-new-privileges` | Prevent setuid/setgid |
| Non-root user | `--user 1000:1000` | No root assumptions |
| Global timeout | `timeout 600` wrapper | Kill hung runs |

Container isolation is the primary safety mechanism. The agent runs in a disposable environment with no access to production, the internet, or the host filesystem beyond the repo and output mounts.

#### Cloud

| Constraint | Mechanism | Purpose |
|---|---|---|
| Restricted egress | Security group: allowlist of required endpoints | Container can reach LLM API, GitHub, S3, app dependencies |
| No SSH/exec access | No ECS Exec enabled | Cannot shell into running task |
| Read-only root filesystem | ECS task definition `readonlyRootFilesystem: true` | Same as local |
| Non-root user | ECS task definition `user: "1000:1000"` | Same as local |
| No privilege escalation | ECS `linuxParameters.capabilities.drop: ["ALL"]` | Same as local |
| Task timeout | ECS `stopTimeout` + minion `timeout 600` | Kill hung runs |
| Scoped IAM | Task role: S3 write (artifact bucket only), Secrets Manager read | Minimal AWS permissions |

Cloud mode trades `--network none` for restricted security group egress. The container needs network access for three distinct reasons:

1. **LLM API calls** — the agent calling Anthropic (`api.anthropic.com`). Always needed.
2. **Infrastructure operations** — deterministic steps: git clone/push (`github.com`), artifact upload (`S3`). Scripted, not agent-initiated.
3. **App-under-development** — the application the agent is working on may need to reach its own dependencies (external APIs, CDNs, package registries). Configured at the application level via `.env` or config files.

The toolshed remains the capability boundary for the agent's *own* interactions with external services during agentic steps. But network isolation is not the enforcement mechanism — tool availability is. The agent can only use the MCP tools it's given.

### 8.3 Branch Output

**Local mode:** The container pushes to a host-mounted bare git remote, creating a proper branch.

**Safety controls** (via server-side `pre-receive` hook):

- Only `refs/heads/minion/*` allowed
- Force pushes rejected
- Tags rejected
- Pushes over a size threshold rejected

**Cloud mode:** The entrypoint pushes directly to the real remote (GitHub) using the short-lived GitHub App installation token.

**Safety controls** (via GitHub repo settings):

- Branch protection rules on `main`/`master` prevent direct pushes
- The GitHub App installation token is scoped to the target repo only
- Branch naming convention (`minion/{run-id}/*`) enforced by the push script
- Token expires after the run, limiting the window for misuse

### 8.4 Hygiene Checks

Before commit (step 9), regardless of runtime mode:

- Check for out-of-scope file changes (from shell commands); revert with `git restore`
- `git status --porcelain` must be clean after staging
- Record environment fingerprint: image tag, `pip freeze`, Python version

## 9. Cloud Infrastructure

This section describes the AWS resources required for cloud mode.

### 9.1 ECR (Elastic Container Registry)

The same Docker image used locally is pushed to ECR. CI pushes a new image on merge to main. The image tag includes a content hash for reproducibility.

### 9.2 ECS / Fargate

Each minion run is a single ECS Fargate task with one container. The task definition is a template that specifies:

- Which image to pull (from ECR)
- How much CPU and memory (e.g., 1 vCPU, 2 GB)
- Secrets to inject from Secrets Manager (GitHub token, Anthropic API key)
- Security group (network egress rules)
- IAM task role (S3 and Secrets Manager permissions)
- Execution role (ECR pull, CloudWatch Logs)
- Read-only root filesystem, non-root user, dropped capabilities

The `CloudBackend` calls `ecs.run_task()` with per-run overrides (task description, repo URL, run ID) layered on top of this template. Fargate pulls the image, starts the container, and runs `entrypoint.sh → minion.py`. When it exits, Fargate cleans up the underlying compute.

**Cost:** You pay per-second for CPU/memory while the task runs. A 10-minute run with 1 vCPU / 2 GB costs roughly $0.01-0.02 in compute (LLM API costs dominate). There's no idle cost — nothing runs between minion runs.

### 9.3 S3 (Artifact Storage)

Artifacts are stored under a convention-based path:

```
s3://{bucket}/{run-id}/
  run_summary.json
  scope.json
  decision_summary.md
  diff.patch
  diff_stats.txt
  commands.log
  lint_report.json
  test_output.txt
  trace.ndjson
```

The entrypoint uploads the contents of `/output` to S3 as its final step (using the AWS CLI or boto3, which are included in the image). The CLI's `CloudBackend` downloads these artifacts for local display after the task completes.

No lifecycle policy initially. Add S3 lifecycle rules (e.g., expire after 90 days) when storage costs matter.

### 9.4 Secrets Manager

Two secrets:

| Secret | Contents | Used By |
|---|---|---|
| GitHub App private key | PEM file for the installed GitHub App | Orchestrator (to mint installation tokens before each run) |
| Anthropic API key | API key for LLM calls | Container (injected as env var) |

The orchestrator mints a short-lived GitHub App installation token before each run and passes it to the ECS task as an environment variable. The token is scoped to the target repo and expires after 1 hour (GitHub's default). This avoids storing long-lived git credentials in the container.

### 9.5 GitHub App

A GitHub App is installed on the target organization or repo. It needs these permissions:

| Permission | Level | Purpose |
|---|---|---|
| Contents | Read & write | Clone repo, push branches |
| Pull requests | Write | (Future) Create PRs from minion output |
| Metadata | Read | Required by GitHub for all Apps |

The orchestrator uses the App's private key to mint a short-lived installation token for each run. This token is injected into the ECS task as an environment variable and used by the entrypoint for git clone/push.

### 9.6 IAM Roles

| Role | Attached To | Permissions |
|---|---|---|
| Task execution role | ECS task (Fargate runtime) | ECR pull, CloudWatch Logs write, Secrets Manager read |
| Task role | Container | S3 write to artifact bucket |

The task role follows least privilege: it can write to the artifact S3 bucket, nothing else. Secrets (API keys, GitHub tokens) are injected as environment variables at task launch, not fetched at runtime.

### 9.7 Security Groups

| Security Group | Attached To | Inbound | Outbound |
|---|---|---|---|
| `minion-sg` | Container | None | `api.anthropic.com:443`, `github.com:443`, S3 gateway endpoint, toolshed backend endpoints |

The security group allows egress to the services the container needs: the LLM API, GitHub (for clone/push), S3 (for artifact upload), and any external backends the toolshed tools require. Inbound is fully closed — nothing can connect to the container.

For apps-under-development that need additional egress (e.g., external APIs the app calls), the security group can be widened per task definition, or a broader default can be used with the understanding that container isolation (read-only filesystem, non-root, dropped capabilities, timeout) is the primary safety mechanism.

## 10. Observability

Every run produces structured artifacts in `/output`:

| Artifact | Format | Contents |
|---|---|---|
| `run_summary.json` | JSON | Task, profile, base/head SHA, start/end time, outcome (success / test_failure / scope_disagreement / timeout), **run ID, runtime mode** |
| `scope.json` | JSON | Scoper's output: allowed files/dirs, rationale |
| `decision_summary.md` | Markdown | Human-readable narrative: what changed, why, what failed, what remains |
| `diff.patch` | Unified diff | The code changes |
| `diff_stats.txt` | Text | `git diff --stat` for quick review |
| `commands.log` | NDJSON | Every shell command: command, cwd, exit code, duration, truncated output |
| `lint_report.json` | JSON | Machine-readable ruff output |
| `test_output.txt` | Text | pytest output |
| `trace.ndjson` | NDJSON | Agent messages, tool invocations, token counts |

The `scope.json` is now produced by the scoper agent rather than a deterministic algorithm, but it serves the same purpose: an inspectable contract between what was intended and what was changed.

**Artifact delivery** depends on runtime mode:
- **Local:** artifacts are on the host filesystem at `/output/{run-id}/`
- **Cloud:** artifacts are uploaded to `s3://{bucket}/{run-id}/` and downloaded by the CLI after the task completes

The artifact set is identical. The only addition in `run_summary.json` is the `runtime` field (`"local"` or `"cloud"`) and the run ID.

## 11. Data Flow

### 11.1 Local Mode

1. Engineer invokes a task via CLI: `minion run "fix the auth bypass" --repo ./my-app`
2. `run.py` assigns a run ID and selects `LocalBackend`
3. `LocalBackend` ensures Docker image exists (builds if needed, cached)
4. `LocalBackend` starts host-side Toolshed server on Unix socket (or uses in-process mocks)
5. `LocalBackend` creates a hardened container: `/repo` read-only, `/mcp.sock` for Toolshed, `/output/{run-id}` for artifacts
6. `entrypoint.sh` detects `/repo`, clones it into `/work` (tmpfs)
7. `minion.py` runs the blueprint (identical in both modes):
   - **Scoper** explores codebase → emits `scope.json`
   - **Executor** implements task within scope → code changes
   - Deterministic lint + autofix
   - **Fixer** if lint errors remain
   - Deterministic test run
   - **Fixer** if test failures
   - Deterministic test run (round 2, final)
   - Hygiene checks → revert out-of-scope shell changes → commit → push
8. Rule files discovered automatically as agents read code
9. Artifacts written to `/output`; branch pushed to host-mounted bare remote
10. Container exits and is destroyed
11. Engineer reviews: `git switch minion/{run-id}/fix-auth-bypass`, reads `scope.json` and `decision_summary.md`

### 11.2 Cloud Mode

1. Engineer invokes a task via CLI: `minion run "fix the auth bypass" --repo org/my-app --cloud`
2. `run.py` assigns a run ID and selects `CloudBackend`
3. `CloudBackend` mints a GitHub App installation token for `org/my-app`
4. `CloudBackend` calls `ecs.run_task()` with per-run overrides:
   - Secrets: GitHub token, Anthropic API key
   - Environment: run ID, repo URL, task description, toolshed profile
5. Fargate pulls image, starts the container
6. `entrypoint.sh` detects no `/repo`, clones from GitHub using the injected token
7. `minion.py` runs the blueprint (identical to local mode — step 7 above)
8. Rule files discovered automatically as agents read code
9. Artifacts written to `/output`, uploaded to S3; branch pushed to GitHub
10. Container exits, Fargate cleans up
11. CLI detects task completion via `ecs.describe_tasks()`, downloads artifacts from S3
12. Engineer reviews: `git fetch && git switch minion/{run-id}/fix-auth-bypass`, reads artifacts locally or in S3

### 11.3 What's the Same

The blueprint (step 7) is byte-for-byte identical. The agents, the toolshed tool definitions, rule discovery, scope enforcement, hygiene checks — all unchanged. The only differences are:

- How the repo gets into the container (bind mount vs. clone)
- How artifacts get out (host filesystem vs. S3)
- How the branch gets pushed (bare remote vs. GitHub)
- Where the toolshed runs (host process via socket vs. in-process)

## 12. Why We Dropped Strict Deterministic Scoping

Early versions of this design (v4) included a fully deterministic scope gate: keywords extracted from the task, ripgrep file search, AST import tracing, and three enforcement checkpoints interleaved through the blueprint. We dropped it for several reasons:

**Deterministic scope selection is inherently brittle.** A keyword + import-tracing gate will always have false negatives. "Fix the auth bypass" might require touching a rate limiter that has no lexical or import relationship to the auth module. The agent would recognize this within seconds of reading the code, but the deterministic gate would never find it.

**It created cascading complexity.** The scope gate required a dedicated derivation step, three enforcement checkpoints, a separate algorithm for expanding fixer permissions, and forbidden pattern enforcement at multiple tool boundaries. This added implementation surface area without proportional value.

**Frontier models don't need it.** Current models, with clear prompts, don't produce "AI refactor soup" often enough to justify mechanical constraints. If they do, the fix is prompt tuning, not a gate that second-guesses the agent.

**The scoper/executor design is better.** An LLM that reads the code produces better scope than any heuristic. The scoper's output is still a structured, inspectable artifact — we didn't lose the reviewability property. We just moved scope selection from a brittle algorithm to an agent that actually understands the code.

## 13. Gaps vs. Stripe's Production System

Intentionally omitted:

- **Pre-warmed container pools** — Stripe keeps devboxes ready in ~10 seconds
- **Slack integration** — Stripe triggers minions from Slack threads
- **PR creation** — Stripe's minions open pull requests via GitHub API
- **Real tool backends** — our Toolshed returns mock data (in-process with network access in cloud mode; Unix socket to host in local mode)
- **Background linting** — Stripe pre-computes lint results for sub-second feedback
- **Build graph analysis** — our test selection runs the full suite; Stripe uses dependency graphs
- **Agent decision UI** — Stripe tracks decisions in a web interface
- **Coverage-based test selection** — pytest-testmon deferred until test run times become a problem

## 14. Future Work

These are intentionally deferred, not forgotten. They can be added without changing the core architecture.

### Existing deferrals (from v6)

- **Agent budgets** — wall clock timeout, tool call limits, file write limits, churn limits (max files/lines changed). Currently we rely on the container timeout and human review. Granular budgets become important when running many minions in parallel or when cost control matters.
- **Container resource limits** — CPU limits (`--cpus`), memory limits (`--memory`), process limits (`--pids-limit`). Currently the container runs with default Docker / Fargate resource allocation. These become important for shared infrastructure.
- **Forbidden file patterns** — rejecting writes to lockfiles, vendor directories, generated code at the tool level. Currently the scoper is expected to exclude these from scope. A mechanical safeguard would add defense in depth.
- **Tool-level write restrictions** — different MCP tool profiles for the fixer vs. executor. Currently both get the same Toolshed profile.
- **Coverage-based test selection** — pytest-testmon for running only affected tests, deferred until full suite run times become a problem.

### Cloud-specific deferrals (new in v7)

- **Durable orchestrator** — Replace CLI polling with AWS Step Functions for reliable multi-step workflows (launch → wait → collect → notify). Needed when runs are triggered by automation rather than a human at a terminal.
- **DynamoDB run index** — Structured metadata per run (task, repo, outcome, timing, cost) queryable without listing S3 prefixes. Useful once you have many runs and want to answer "what failed this week?"
- **Cost monitoring and alerts** — CloudWatch alarms on Fargate spend, per-run cost tracking (LLM tokens + compute time). Important when running many minions in parallel.
- **Pre-warmed Fargate tasks** — Keep containers warm to eliminate cold start latency (~30-60s for image pull + clone). Justified when run volume is high enough that startup time dominates.
- **PR creation** — The GitHub App already has the permissions. Adding a `create_pr` toolshed tool and a blueprint step after push is straightforward.

## 15. Summary of Tradeoffs

| Decision | What We Get | What We Give Up |
|---|---|---|
| Blueprint (fixed steps) | Reliability, guaranteed linting/testing | Flexibility for agent to choose workflow |
| Scoper/executor separation | Inspectable scope from an agent that understands the code | Extra LLM call, potential for scoper mistakes |
| LLM-derived scope with deterministic enforcement | Accurate scope that finds semantic relationships | Scope quality depends on model quality |
| Scope disagreement as explicit outcome | Natural feedback loop, no silent failures | Some tasks require multiple runs |
| Rule discovery via read_file | Context relevant to actual work, no upfront prediction | Rules for untouched directories never loaded |
| No agent budgets (initially) | Simpler implementation, fewer false stops | Risk of runaway cost/time on bad tasks |
| Container isolation as primary safety | Simple, strong, well-understood | No defense in depth inside the container |
| Full test suite (no testmon) | Simple, no cache management | Slower for large test suites |
| Trust the model | Adapts to frontier model improvements, simpler system | Relies on model quality for discipline |
| Same image for local and cloud | Identical behavior in both modes, one build pipeline | Image includes deps for both modes (slightly larger) |
| Clone at startup (cloud) | Always latest code, no pre-warming infrastructure | 10-60s startup latency for large repos |
| CLI polling (cloud orchestrator) | Simple, no extra AWS services | CLI must stay alive; interrupted polling loses visibility (not data) |
| Network access in cloud mode (vs. `--network none` local) | Container can clone repos, push branches, run apps that need network, call LLM directly | Wider attack surface than local mode's zero-network model |
| Toolshed as capability boundary (not network boundary) | Single auditable channel for agent-initiated external interactions, simple single-container deployment | Enforcement is at tool layer, not network layer — agent's shell commands could theoretically reach the network |
| GitHub App installation tokens | Short-lived, repo-scoped, no long-lived credentials | Requires GitHub App setup, token minting adds latency |
| Parallel runs via unique run IDs | No coordination needed, scales trivially | No deduplication — same task submitted twice runs twice |

The overarching theme: constrain the agent where you can predict behavior, and give it freedom where creativity is required. Linting, testing, committing, and isolation are predictable. Understanding code and deciding what to change are not. The runtime backend abstraction extends this — how and where the container runs is predictable infrastructure; the orchestrator handles it so the agent doesn't have to know. The toolshed extends it further — external interactions during agentic steps are mediated and audited, while deterministic steps and the app-under-development use the network directly.
