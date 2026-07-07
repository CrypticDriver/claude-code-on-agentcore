#!/usr/bin/env bash
#
# One-click deploy: ECR repo + ARM64 image + IAM role + AgentCore Runtime.
#
# By default no Docker is needed: the prebuilt public image is copied into
# your private ECR over the registry HTTP API (AgentCore requires the image
# to live in your own account's ECR). Set BUILD=local to build from source
# instead (requires docker with buildx), e.g. after editing runtime/.
#
# Usage:
#   ./deploy/deploy.sh                      # defaults (us-east-1, Sonnet), no Docker
#   REGION=us-west-2 ./deploy/deploy.sh
#   MODEL=us.anthropic.claude-opus-4-7 ./deploy/deploy.sh
#   BUILD=local ./deploy/deploy.sh          # build the image yourself
#   GATEWAY_MCP_URL=https://...  ./deploy/deploy.sh   # optional: Gateway web search
#                                                     # (create it first with deploy/setup_web_search.py)
#
# Requirements: aws CLI v2, python3 + boto3 >= 1.39 (and docker with buildx
# only when BUILD=local). The caller's AWS credentials need permissions for
# ECR, IAM (role create), and bedrock-agentcore-control.
set -euo pipefail

cd "$(dirname "$0")/.."

REGION="${REGION:-us-east-1}"
RUNTIME_NAME="${RUNTIME_NAME:-claude_code_runtime}"
REPO="${REPO:-claude-code-agentcore}"
TAG="${TAG:-latest}"
MODEL="${MODEL:-us.anthropic.claude-sonnet-4-6}"
GATEWAY_MCP_URL="${GATEWAY_MCP_URL:-}"
BUILD="${BUILD:-prebuilt}"
SOURCE_IMAGE="${SOURCE_IMAGE:-public.ecr.aws/f5f0l0w1/claude-code-agentcore:latest}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO}:${TAG}"
ROLE_NAME="${ROLE_NAME:-AgentCoreClaudeCodeRuntimeRole}"

echo "==> region:  ${REGION}"
echo "==> image:   ${IMAGE}"
echo "==> model:   ${MODEL}"
echo "==> runtime: ${RUNTIME_NAME}"

# --- 1. IAM execution role (idempotent) -------------------------------------
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "==> creating IAM role ${ROLE_NAME}"
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' >/dev/null
  aws iam put-role-policy --role-name "${ROLE_NAME}" \
    --policy-name runtime-permissions \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {
          \"Sid\": \"BedrockInvoke\",
          \"Effect\": \"Allow\",
          \"Action\": [\"bedrock:InvokeModel\", \"bedrock:InvokeModelWithResponseStream\"],
          \"Resource\": \"*\"
        },
        {
          \"Sid\": \"EcrPull\",
          \"Effect\": \"Allow\",
          \"Action\": [\"ecr:GetDownloadUrlForLayer\", \"ecr:BatchGetImage\"],
          \"Resource\": \"arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/${REPO}\"
        },
        {
          \"Sid\": \"EcrAuth\",
          \"Effect\": \"Allow\",
          \"Action\": \"ecr:GetAuthorizationToken\",
          \"Resource\": \"*\"
        },
        {
          \"Sid\": \"Logs\",
          \"Effect\": \"Allow\",
          \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\", \"logs:DescribeLogGroups\", \"logs:DescribeLogStreams\"],
          \"Resource\": \"arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/*\"
        },
        {
          \"Sid\": \"Telemetry\",
          \"Effect\": \"Allow\",
          \"Action\": [\"xray:PutTraceSegments\", \"xray:PutTelemetryRecords\", \"cloudwatch:PutMetricData\"],
          \"Resource\": \"*\"
        }
      ]
    }" >/dev/null
  echo "==> waiting for role propagation"
  sleep 10
else
  echo "==> IAM role ${ROLE_NAME} exists"
fi
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Optional: let the runtime call the Gateway MCP endpoint (web search).
if [ -n "${GATEWAY_MCP_URL}" ]; then
  echo "==> granting InvokeGateway to ${ROLE_NAME}"
  aws iam put-role-policy --role-name "${ROLE_NAME}" \
    --policy-name gateway-invoke \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Action\": \"bedrock-agentcore:InvokeGateway\",
        \"Resource\": \"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:gateway/*\"
      }]
    }" >/dev/null
fi

# --- 2. ECR repo + ARM64 image ----------------------------------------------
aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" \
       --image-scanning-configuration scanOnPush=true >/dev/null

if [ "${BUILD}" = "local" ]; then
  echo "==> ecr login"
  aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null

  echo "==> building linux/arm64 image (first build takes a few minutes)"
  docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    --tag "${IMAGE}" \
    --push \
    runtime/
else
  echo "==> copying prebuilt image into your ECR (no Docker needed)"
  SOURCE_IMAGE="${SOURCE_IMAGE}" REGION="${REGION}" REPO="${REPO}" TAG="${TAG}" \
  python3 deploy/copy_image.py
fi

# --- 3. AgentCore Runtime (boto3 — needs filesystemConfigurations) -----------
echo "==> creating/updating AgentCore runtime"
REGION="${REGION}" RUNTIME_NAME="${RUNTIME_NAME}" IMAGE="${IMAGE}" \
ROLE_ARN="${ROLE_ARN}" MODEL="${MODEL}" GATEWAY_MCP_URL="${GATEWAY_MCP_URL}" \
python3 deploy/create_runtime.py

echo
echo "==> Done. Configure the client:"
echo "    export CC_AGENTCORE_RUNTIME_ARN=\$(cat .runtime_arn)"
echo "    ./client/bin/ccr \"Say hello\""
