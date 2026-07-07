#!/usr/bin/env bash
#
# Maintainers only: build and publish the prebuilt public image that
# deploy.sh copies by default. Requires docker buildx and push access
# to the ECR Public repository.
#
#   PUBLIC_IMAGE=public.ecr.aws/<alias>/claude-code-agentcore:latest ./deploy/publish_image.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PUBLIC_IMAGE="${PUBLIC_IMAGE:-public.ecr.aws/f5f0l0w1/claude-code-agentcore:latest}"

aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws >/dev/null

echo "==> building and pushing ${PUBLIC_IMAGE}"
docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --tag "${PUBLIC_IMAGE}" \
  --push \
  runtime/
