# Cloud Deployment Guide

This guide walks you through deploying Lackey's cloud backend on AWS. It's split into phases so failures are easy to isolate — each phase builds on the previous one.

Throughout this guide, replace the placeholders with your own values:

| Placeholder | Example | Where to find it |
|---|---|---|
| `YOUR_AWS_ACCOUNT_ID` | `123456789012` | `aws sts get-caller-identity --query Account --output text` |
| `YOUR_REGION` | `us-east-1` | Your preferred AWS region |
| `YOUR_BUCKET_NAME` | `lackey-artifacts-123456789012` | You choose this (must be globally unique) |
| `YOUR_GITHUB_APP_ID` | `12345` | GitHub App settings page |
| `YOUR_INSTALLATION_ID` | `67890` | GitHub App installations page |
| `YOUR_GITHUB_ORG/REPO` | `acme/my-project` | The repo you want Lackey to work on |

---

## Phase 1: Local backend (no AWS needed)

**Prerequisites:** Docker running locally, a git repo to test against, an Anthropic API key.

### 1a. Build the Docker image

```sh
make build-base    # builds minion-base:latest
```

### 1b. Create a `.env` file

```sh
ANTHROPIC_API_KEY=sk-ant-...
LACKEY_REPO=.
LACKEY_IMAGE=minion-base:latest
```

### 1c. Run via the CLI

```sh
lackey run "add a hello world test"
```

**Verify:**
- Container starts and clones repo from bind mount
- Scoper agent explores codebase
- Executor agent writes code
- Output dir created at `/tmp/lackey/<run_id>/`
- CLI prints outcome, runtime, branch, and artifact path

---

## Phase 2: AWS prerequisites

### 2a. Install and configure the AWS CLI

```sh
brew install awscli          # or your preferred install method
aws configure                # set region and credentials
aws sts get-caller-identity  # verify it works — note your account ID
```

### 2b. Create an S3 bucket for artifacts

```sh
aws s3 mb s3://YOUR_BUCKET_NAME --region YOUR_REGION
```

**Verify** with a round-trip test:

```sh
mkdir -p /tmp/lackey-upload-test
echo '{"outcome":"success"}' > /tmp/lackey-upload-test/run_summary.json
echo 'hello from lackey' > /tmp/lackey-upload-test/log.txt
```

```python
from pathlib import Path
from lackey.cloud.upload import upload_artifacts
from lackey.cloud.s3 import download_artifacts

# Upload
upload_artifacts(Path('/tmp/lackey-upload-test'), 'YOUR_BUCKET_NAME', 'test-roundtrip')

# Download
dest = Path('/tmp/lackey-download-test')
download_artifacts('YOUR_BUCKET_NAME', 'test-roundtrip', dest, region='YOUR_REGION')

# Verify
assert (dest / 'run_summary.json').read_text().strip() == '{"outcome":"success"}'
assert (dest / 'log.txt').read_text().strip() == 'hello from lackey'
```

Clean up:
```sh
aws s3 rm s3://YOUR_BUCKET_NAME/test-roundtrip/ --recursive
```

### 2c. Create an ECR repository

```sh
aws ecr create-repository --repository-name lackey-minion --region YOUR_REGION
```

**Verify** by pushing the local image:

```python
from lackey.cloud.ecr import ensure_image_in_ecr

uri = ensure_image_in_ecr(
    'minion-base:latest',
    'YOUR_AWS_ACCOUNT_ID.dkr.ecr.YOUR_REGION.amazonaws.com',
    'lackey-minion',
    'YOUR_REGION',
)
```

Running it a second time should print "already exists in ECR".

### 2d. Create a GitHub App

1. Go to **GitHub Settings > Developer settings > GitHub Apps > New GitHub App**
2. Name it something like `lackey-YOUR_ORG`
3. Set **permissions**:
   - **Contents**: read & write (clone repo, push branches)
   - **Pull requests**: read & write (create PRs)
   - **Metadata**: read (get default branch)
4. Generate a **private key** and download the `.pem` file
5. Install the app on the target repo(s) — note the **Installation ID** from the URL

### 2e. Store secrets in AWS Secrets Manager

**GitHub App private key:**
```sh
aws secretsmanager create-secret \
  --name lackey/github-app-key \
  --secret-string "file:///path/to/your-app.private-key.pem" \
  --region YOUR_REGION
```

**Anthropic API key:**
```sh
aws secretsmanager create-secret \
  --name lackey/anthropic-api-key \
  --secret-string "YOUR_ANTHROPIC_API_KEY" \
  --region YOUR_REGION
```

**Verify** the GitHub token minting:

```python
from lackey.cloud.github_token import get_github_app_private_key, mint_installation_token
import httpx

private_key = get_github_app_private_key('lackey/github-app-key', 'YOUR_REGION')
token = mint_installation_token(
    app_id='YOUR_GITHUB_APP_ID',
    private_key=private_key,
    installation_id='YOUR_INSTALLATION_ID',
)

r = httpx.get('https://api.github.com/installation/repositories',
              headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github+json'})
repos = [repo['full_name'] for repo in r.json().get('repositories', [])]
print(repos)  # should list your installed repo(s)
```

---

## Phase 3: ECS infrastructure

### 3a. Create the ECS cluster

```sh
aws ecs create-cluster --cluster-name lackey --region YOUR_REGION
```

### 3b. Create CloudWatch log group

```sh
aws logs create-log-group --log-group-name /ecs/lackey-minion --region YOUR_REGION
```

### 3c. Create IAM roles

You need two roles:

**Execution role** (lets ECS pull the image and write logs):

```sh
aws iam create-role \
  --role-name lackey-ecs-execution \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam attach-role-policy \
  --role-name lackey-ecs-execution \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

**Task role** (lets the running container push to S3):

```sh
aws iam create-role \
  --role-name lackey-ecs-task \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

aws iam put-role-policy \
  --role-name lackey-ecs-task \
  --policy-name lackey-s3-artifacts \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::YOUR_BUCKET_NAME",
        "arn:aws:s3:::YOUR_BUCKET_NAME/*"
      ]
    }]
  }'
```

### 3d. Set up networking

```sh
# List default VPC subnets — note the IDs
aws ec2 describe-subnets \
  --filters "Name=default-for-az,Values=true" \
  --query 'Subnets[].SubnetId' \
  --region YOUR_REGION \
  --output text

# Get the default VPC ID
VPC_ID=$(aws ec2 describe-vpcs \
  --filters "Name=isDefault,Values=true" \
  --query 'Vpcs[0].VpcId' \
  --region YOUR_REGION \
  --output text)

# Create security group (outbound-only — no inbound needed)
aws ec2 create-security-group \
  --group-name lackey-ecs-tasks \
  --description "Lackey ECS tasks - outbound only" \
  --vpc-id "$VPC_ID" \
  --region YOUR_REGION
```

The default outbound rule (0.0.0.0/0) is sufficient — the container needs to reach GitHub (clone/push) and the Anthropic API.

Note down the **subnet IDs** and **security group ID** for the next steps.

### 3e. Register the ECS task definition

Save this as `/tmp/lackey-task-def.json`, replacing the placeholders:

```json
{
  "family": "lackey-minion",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "4096",
  "executionRoleArn": "arn:aws:iam::YOUR_AWS_ACCOUNT_ID:role/lackey-ecs-execution",
  "taskRoleArn": "arn:aws:iam::YOUR_AWS_ACCOUNT_ID:role/lackey-ecs-task",
  "containerDefinitions": [
    {
      "name": "minion",
      "image": "YOUR_AWS_ACCOUNT_ID.dkr.ecr.YOUR_REGION.amazonaws.com/lackey-minion:latest",
      "essential": true,
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/lackey-minion",
          "awslogs-region": "YOUR_REGION",
          "awslogs-stream-prefix": "minion"
        }
      }
    }
  ]
}
```

```sh
aws ecs register-task-definition \
  --cli-input-json file:///tmp/lackey-task-def.json \
  --region YOUR_REGION
```

---

## Phase 4: End-to-end cloud run

### 4a. Configure environment variables

Add the cloud vars to your `.env` file:

```sh
# AWS
AWS_REGION=YOUR_REGION
LACKEY_ECR_REGISTRY=YOUR_AWS_ACCOUNT_ID.dkr.ecr.YOUR_REGION.amazonaws.com
LACKEY_ECS_CLUSTER=lackey
LACKEY_ECS_TASK_DEF=lackey-minion
LACKEY_ECS_SUBNETS=subnet-aaa,subnet-bbb    # from step 3d
LACKEY_ECS_SG=sg-xxx                          # from step 3d
LACKEY_ARTIFACT_BUCKET=YOUR_BUCKET_NAME

# GitHub App
LACKEY_GITHUB_APP_ID=YOUR_GITHUB_APP_ID
LACKEY_GITHUB_APP_PRIVATE_KEY_SECRET=lackey/github-app-key
LACKEY_GITHUB_INSTALLATION_ID=YOUR_INSTALLATION_ID

# Secrets
LACKEY_ANTHROPIC_SECRET=lackey/anthropic-api-key

# Repo + image
LACKEY_REPO=YOUR_GITHUB_ORG/REPO
LACKEY_IMAGE=minion-base:latest
```

### 4b. Run it

```sh
lackey run "add a hello world test" --cloud
```

**Expected output in order:**
1. "Image ... already exists in ECR" (or pushes if rebuilt)
2. "Minting GitHub App installation token..."
3. "Fetching Anthropic API key from Secrets Manager..."
4. "Launched ECS task: arn:aws:ecs:..."
5. Status transitions on stderr: PROVISIONING -> PENDING -> RUNNING -> STOPPED
6. "Downloading artifacts to /tmp/lackey/..."
7. Final summary with outcome, branch, artifact path, S3 prefix

**Verify side effects:**
- Branch pushed to GitHub: `git ls-remote origin | grep minion/`
- Artifacts in S3: `aws s3 ls s3://YOUR_BUCKET_NAME/<run_id>/`
- Artifacts downloaded locally: `ls /tmp/lackey/<run_id>/`
- `run_summary.json` has correct outcome and branch name

### 4c. Dry run without LLM calls

To test the full cloud plumbing without burning Anthropic tokens, temporarily add `'LACKEY_STUBS': '1'` to the `env_overrides` dict in `CloudBackend.launch()`, then repeat step 4b.

The ECS task should run, produce `run_summary.json` with `outcome: success`, and land artifacts in S3 — all without any LLM calls.

---

## Troubleshooting

### ECS task fails to start

```sh
aws ecs describe-tasks --cluster lackey --tasks TASK_ARN \
  --query 'tasks[0].{status:lastStatus,reason:stoppedReason,containers:containers[0].{exitCode:exitCode,reason:reason}}' \
  --region YOUR_REGION
```

Common reasons:
- **CannotPullContainerError** — ECR image URI is wrong or the execution role can't pull
- **ResourceNotFoundException** — task definition doesn't exist
- **Essential container exited** — check CloudWatch logs at `/ecs/lackey-minion`

### Missing cloud env vars

```sh
unset LACKEY_ECR_REGISTRY
lackey run "test" --cloud 2>&1
```

You should get a clear `KeyError` pointing to the missing var, not a confusing traceback.

### Timeout handling

```sh
lackey run "do something complex" --timeout 5
```

The container should be killed after 5 seconds, the CLI should exit non-zero, and the result should say `error`.

---

## Cleanup

If you want to tear everything down:

| Resource | How to delete |
|---|---|
| S3 bucket | `aws s3 rb s3://YOUR_BUCKET_NAME --force --region YOUR_REGION` |
| ECR repo | `aws ecr delete-repository --repository-name lackey-minion --force --region YOUR_REGION` |
| Secrets Manager (GitHub key) | `aws secretsmanager delete-secret --secret-id lackey/github-app-key --region YOUR_REGION` |
| Secrets Manager (Anthropic key) | `aws secretsmanager delete-secret --secret-id lackey/anthropic-api-key --region YOUR_REGION` |
| ECS cluster | `aws ecs delete-cluster --cluster lackey --region YOUR_REGION` |
| IAM execution role | `aws iam detach-role-policy --role-name lackey-ecs-execution --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy && aws iam delete-role --role-name lackey-ecs-execution` |
| IAM task role | `aws iam delete-role-policy --role-name lackey-ecs-task --policy-name lackey-s3-artifacts && aws iam delete-role --role-name lackey-ecs-task` |
| Security group | `aws ec2 delete-security-group --group-name lackey-ecs-tasks --region YOUR_REGION` |
| CloudWatch log group | `aws logs delete-log-group --log-group-name /ecs/lackey-minion --region YOUR_REGION` |
| ECS task definition | Task defs can't be deleted, only deregistered: `aws ecs deregister-task-definition --task-definition lackey-minion:1 --region YOUR_REGION` |
| GitHub App | Delete at `https://github.com/settings/apps/YOUR_APP_NAME` |
