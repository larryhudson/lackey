# Lackey build & push helpers
#
# Usage:
#   make build-base                  Build minion-base for amd64
#   make build-app                   Build app image from example/Dockerfile.minion
#   make push                        Force-push app image to ECR
#   make build-and-push              Build both images and push to ECR
#
# Override defaults:
#   make build-app APP_DOCKERFILE=path/to/Dockerfile.minion APP_IMAGE=my-minion:latest
#   make push ECR_REPO=my-ecr-repo

# Load .env if present (- prefix ignores missing file)
-include .env
export

PLATFORM       ?= linux/amd64
BASE_IMAGE     ?= minion-base:latest
APP_IMAGE      ?= minion-example:latest
APP_DOCKERFILE ?= example/Dockerfile.minion

# ECR settings (override or set via env)
ECR_REGISTRY   ?= $(LACKEY_ECR_REGISTRY)
ECR_REPO       ?= lackey-minion
AWS_REGION     ?= eu-central-1

.PHONY: build-base build-app push build-and-push

build-base:
	docker build --platform $(PLATFORM) -t $(BASE_IMAGE) .

build-app: build-base
	docker build --platform $(PLATFORM) -t $(APP_IMAGE) -f $(APP_DOCKERFILE) .

push:
	@if [ -z "$(ECR_REGISTRY)" ]; then \
		echo "ERROR: Set LACKEY_ECR_REGISTRY or ECR_REGISTRY"; exit 1; \
	fi
	uv run python -c "from lackey.cloud.ecr import ensure_image_in_ecr; ensure_image_in_ecr('$(APP_IMAGE)', '$(ECR_REGISTRY)', '$(ECR_REPO)', '$(AWS_REGION)', force=True)"

build-and-push: build-app push
