#!/bin/sh
set -euo pipefail

# entrypoint.sh — Container entrypoint for lackey minion runs (DESIGN.md §8.2)
#
# Detects local vs cloud mode, clones the repo, runs the blueprint,
# and handles artifact collection.
#
# Environment variables:
#   TASK              — task description (required)
#   RUN_ID            — unique run identifier (required)
#   TIMEOUT           — run timeout in seconds (default: 600)
#   GITHUB_TOKEN      — GitHub access token (cloud mode only)
#   REPO              — GitHub repo slug, e.g. "owner/repo" (cloud mode only)
#   TOOLSHED_PROFILE  — toolshed profile name (default: "default")

# ── Validate required env vars ────────────────────────────────────────────

if [ -z "${TASK:-}" ]; then
    echo "ERROR: TASK environment variable is required" >&2
    exit 1
fi

if [ -z "${RUN_ID:-}" ]; then
    echo "ERROR: RUN_ID environment variable is required" >&2
    exit 1
fi

TIMEOUT="${TIMEOUT:-600}"

# ── Clone repo ────────────────────────────────────────────────────────────

if [ -d /repo ]; then
    # Local mode: repo is bind-mounted read-only at /repo
    echo "Local mode: cloning from /repo"
    git clone /repo /work
else
    # Cloud mode: clone from GitHub
    if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${REPO:-}" ]; then
        echo "ERROR: Cloud mode requires GITHUB_TOKEN and REPO env vars" >&2
        exit 1
    fi
    echo "Cloud mode: cloning from github.com/${REPO}"
    git clone "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" /work
fi

cd /work

# Configure git identity for commits
git config user.email "lackey@localhost"
git config user.name "Lackey"

# ── Ensure output directory exists ────────────────────────────────────────

mkdir -p /output

# ── Run the blueprint ─────────────────────────────────────────────────────

echo "Starting blueprint: task='${TASK}' run_id='${RUN_ID}' timeout=${TIMEOUT}s"

exit_code=0
timeout "${TIMEOUT}" python -m lackey || exit_code=$?

# ── Cloud mode: upload artifacts ──────────────────────────────────────────

if [ ! -d /repo ]; then
    echo "Cloud mode: pushing branch to origin"
    git push origin HEAD

    echo "Cloud mode: creating pull request"
    python -m lackey.cloud.pr

    if [ -n "${ARTIFACT_BUCKET:-}" ]; then
        echo "Cloud mode: uploading artifacts to s3://${ARTIFACT_BUCKET}/${RUN_ID}/"
        python -m lackey.cloud.upload
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────

echo "Blueprint finished with exit code ${exit_code}"
exit "${exit_code}"
