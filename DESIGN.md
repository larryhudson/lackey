# Toy Minion: Design Document

*An unattended coding agent architecture inspired by Stripe's Minions*

---

**Purpose:** The simplest implementation that captures the core architectural principles from Stripe's Minions system, which produces over 1,300 merged PRs per week with no human-written code.

**Source material:** Stripe Engineering blog — "How We Built Minions" (Parts 1 & 2)

**Status:** v6 — introduces a scoper/executor two-agent design. The scoper explores the codebase and defines the boundaries; the executor implements within them. Budgets and tool-level restrictions deferred to future work — the initial version trusts the model and relies on container isolation and human review.

---

## 1. Overview

Stripe's Minions work because of several reinforcing architectural decisions: isolated environments, a blueprint that mixes deterministic and agentic steps, a centralized tool server with curated access, and bounded iteration. Our toy version captures these patterns using Pydantic AI for the agent framework, FastMCP for the tool server, and Docker for isolation.

The system has four layers:

- **run.py** — the host-side orchestrator that manages Docker containers
- **Dockerfile + entrypoint.sh** — the isolated environment where minions execute
- **minion.py** — the blueprint orchestrator that interleaves deterministic and agentic steps
- **toolshed.py** — a centralized MCP tool server with curated access profiles

The two things that matter most for making unattended agents viable are **observability** (so you can debug failures) and **isolation** (so mistakes are contained). Get those right and you can start simple on everything else.

## 2. The Blueprint

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

## 3. Scoper / Executor Design

### 3.1 Why Two Agents

The scoper and executor exist because scope control is valuable but scope *selection* requires understanding the code. A deterministic gate (keyword search, AST import tracing) is brittle — it misses files that are semantically related but have no lexical or import connection. An LLM that actually reads the code can find the rate limiter that "fix the auth bypass" requires.

Separating the two agents gives us:

- **Inspectable scope** — the scoper's output is a structured artifact (`scope.json`) that a human or the executor can review before any code is written
- **Deterministic enforcement** — once the scope is defined by an agent that understands the code, enforcement is mechanical and trustworthy
- **A natural feedback loop** — if the executor disagrees with the scope, the run exits with the executor's reasoning, and a new run can start with adjusted boundaries

### 3.2 The Scoper

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

### 3.3 The Executor

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

### 3.4 Why Not One Agent That Scopes and Executes?

A single agent that defines its own scope and enforces it on itself is just an unconstrained agent with extra steps. The separation matters because the scoper's output is committed as an artifact *before* any code is written, and the executor is mechanically bound by it. This gives the human reviewer a contract: "the scoper said these files, the executor touched these files, here's the diff."

## 4. Rule Discovery

Agent rule files (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules/*`) are discovered as a side effect of the agent reading code, not predicted upfront.

The `read_file` tool is instrumented so that when the agent reads a file, the tool automatically walks from that file's directory up to the repo root, collecting any rule files found along the way. New rules are prepended to the file content returned to the agent, with the most specific (closest to the file) appearing first.

A run-scoped `seen_rules: set[Path]` ensures each rule file is injected only once per run. If the agent reads three files under `services/todo_service/`, the `CLAUDE.md` in that directory is prepended only on the first read.

The agent never knows this is happening — it just gets richer context naturally as it explores the codebase. This matches how Claude Code and Cursor handle rule discovery. Both the scoper and executor benefit from this — the scoper picks up rules as it explores, and the executor picks up rules as it implements.

## 5. The Toolshed

### 5.1 Architecture

All agent-callable tools live in a single FastMCP server. Each agent run receives a curated subset via Pydantic AI's `PreparedToolset` — tools are genuinely absent from the API call, not hidden by prompt instructions.

| Profile | Categories | Use Case |
|---|---|---|
| default | docs, code_intel | Standard coding tasks |
| oncall | docs, code_intel, tickets, observability | On-call triage |
| migration | docs, code_intel, feature_flags | Code migrations |
| full | All categories | Unrestricted |

### 5.2 Tool Trust Boundaries

| Class | Examples | Policy |
|---|---|---|
| Read-only | `search_docs`, `search_codebase`, `get_ticket` | Available by default |
| Write | `update_ticket` | Requires explicit profile opt-in |
| Destructive | (none currently) | Forbidden; mocked in toy version |

Every tool call is audit-logged in `commands.log`.

### 5.3 Networking Topology

The container runs with `--network none`, but Toolshed tools like `get_ticket` need to reach external services.

**Resolution: Unix domain socket transport.**

```
┌─────────────────────────────────────┐
│  HOST                               │
│                                     │
│  Toolshed FastMCP server            │
│  (has network access to Jira,       │
│   Sourcegraph, Datadog, etc.)       │
│       │                             │
│       │ /tmp/toolshed.sock          │
│       │                             │
├───────┼─────────────────────────────┤
│  CONTAINER (--network none)         │
│       │                             │
│  /mcp.sock (mounted from host)      │
│       │                             │
│  Agent → MCP over Unix socket       │
│                                     │
│  (no TCP/IP stack at all)           │
└─────────────────────────────────────┘
```

The Toolshed server runs on the host with full network access. The container communicates through a mounted Unix domain socket — a single, auditable channel.

For the toy implementation where tools return mock data, the Toolshed runs in-process inside the container. The Unix socket architecture is the upgrade path for real backends.

## 6. Agent Design

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

## 7. Container Isolation

### 7.1 Two-Layer Design

| Layer | Contains | Rebuilt When |
|---|---|---|
| Docker image | Python, pip deps, ruff, pytest, minion.py, toolshed.py | Tooling or deps change |
| Container `/repo` | Target repo, mounted read-only from host | Never — always current |
| Container `/work` | Writable clone of `/repo` (tmpfs, sized) | Every run |
| Container `/output` | Artifacts | Every run |

### 7.2 Container Hardening

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

### 7.3 Branch Output

The container pushes to a host-mounted bare git remote, creating a proper branch.

**Safety controls** (via server-side `pre-receive` hook):

- Only `refs/heads/minion/*` allowed
- Force pushes rejected
- Tags rejected
- Pushes over a size threshold rejected

### 7.4 Hygiene Checks

Before commit (step 9):

- Check for out-of-scope file changes (from shell commands); revert with `git restore`
- `git status --porcelain` must be clean after staging
- Record environment fingerprint: image tag, `pip freeze`, Python version

## 8. Observability

Every run produces structured artifacts in `/output`:

| Artifact | Format | Contents |
|---|---|---|
| `run_summary.json` | JSON | Task, profile, base/head SHA, start/end time, outcome (success / test_failure / scope_disagreement / timeout) |
| `scope.json` | JSON | Scoper's output: allowed files/dirs, rationale |
| `decision_summary.md` | Markdown | Human-readable narrative: what changed, why, what failed, what remains |
| `diff.patch` | Unified diff | The code changes |
| `diff_stats.txt` | Text | `git diff --stat` for quick review |
| `commands.log` | NDJSON | Every shell command: command, cwd, exit code, duration, truncated output |
| `lint_report.json` | JSON | Machine-readable ruff output |
| `test_output.txt` | Text | pytest output |
| `trace.ndjson` | NDJSON | Agent messages, tool invocations, token counts |

The `scope.json` is now produced by the scoper agent rather than a deterministic algorithm, but it serves the same purpose: an inspectable contract between what was intended and what was changed.

## 9. Data Flow

1. Engineer invokes a task via CLI
2. `run.py` ensures Docker image exists (builds if needed, cached)
3. `run.py` starts host-side Toolshed server on Unix socket (or uses in-process mocks)
4. `run.py` creates a hardened container: `/repo` read-only, `/mcp.sock` for Toolshed, `/output` for artifacts
5. `entrypoint.sh` clones `/repo` into `/work` (tmpfs)
6. `minion.py` runs the blueprint:
   - **Scoper** explores codebase → emits `scope.json`
   - **Executor** implements task within scope → code changes
   - Deterministic lint + autofix
   - **Fixer** if lint errors remain
   - Deterministic test run
   - **Fixer** if test failures
   - Deterministic test run (round 2, final)
   - Hygiene checks → revert out-of-scope shell changes → commit → push
7. Rule files discovered automatically as agents read code
8. Artifacts written to `/output`; branch pushed to bare remote
9. Container exits and is destroyed
10. Engineer reviews: `git switch minion/fix-the-bug`, reads `scope.json` and `decision_summary.md`

## 10. Why We Dropped Strict Deterministic Scoping

Early versions of this design (v4) included a fully deterministic scope gate: keywords extracted from the task, ripgrep file search, AST import tracing, and three enforcement checkpoints interleaved through the blueprint. We dropped it for several reasons:

**Deterministic scope selection is inherently brittle.** A keyword + import-tracing gate will always have false negatives. "Fix the auth bypass" might require touching a rate limiter that has no lexical or import relationship to the auth module. The agent would recognize this within seconds of reading the code, but the deterministic gate would never find it.

**It created cascading complexity.** The scope gate required a dedicated derivation step, three enforcement checkpoints, a separate algorithm for expanding fixer permissions, and forbidden pattern enforcement at multiple tool boundaries. This added implementation surface area without proportional value.

**Frontier models don't need it.** Current models, with clear prompts, don't produce "AI refactor soup" often enough to justify mechanical constraints. If they do, the fix is prompt tuning, not a gate that second-guesses the agent.

**The scoper/executor design is better.** An LLM that reads the code produces better scope than any heuristic. The scoper's output is still a structured, inspectable artifact — we didn't lose the reviewability property. We just moved scope selection from a brittle algorithm to an agent that actually understands the code.

## 11. Gaps vs. Stripe's Production System

Intentionally omitted:

- **Pre-warmed container pools** — Stripe keeps devboxes ready in ~10 seconds
- **Slack integration** — Stripe triggers minions from Slack threads
- **PR creation** — Stripe's minions open pull requests via GitHub API
- **Real tool backends** — our Toolshed returns mock data (Unix socket upgrade path defined)
- **Background linting** — Stripe pre-computes lint results for sub-second feedback
- **Build graph analysis** — our test selection runs the full suite; Stripe uses dependency graphs
- **Agent decision UI** — Stripe tracks decisions in a web interface
- **Coverage-based test selection** — pytest-testmon deferred until test run times become a problem

## 12. Future Work

These are intentionally deferred, not forgotten. They can be added without changing the core architecture.

- **Agent budgets** — wall clock timeout, tool call limits, file write limits, churn limits (max files/lines changed). Currently we rely on the container timeout and human review. Granular budgets become important when running many minions in parallel or when cost control matters.
- **Container resource limits** — CPU limits (`--cpus`), memory limits (`--memory`), process limits (`--pids-limit`). Currently the container runs with default Docker resource allocation. These become important for shared infrastructure.
- **Forbidden file patterns** — rejecting writes to lockfiles, vendor directories, generated code at the tool level. Currently the scoper is expected to exclude these from scope. A mechanical safeguard would add defense in depth.
- **Tool-level write restrictions** — different MCP tool profiles for the fixer vs. executor. Currently both get the same Toolshed profile.
- **Coverage-based test selection** — pytest-testmon for running only affected tests, deferred until full suite run times become a problem.
- **Pre-warmed container pools** — for faster startup when running many minions in parallel.

## 13. Summary of Tradeoffs

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

The overarching theme: constrain the agent where you can predict behavior, and give it freedom where creativity is required. Linting, testing, committing, and isolation are predictable. Understanding code and deciding what to change are not.
