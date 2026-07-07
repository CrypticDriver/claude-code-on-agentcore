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
ROLE_NAME="${ROLE_NAME:-AgentCoreClaudeCodeRuntimeRole}"

# --- output helpers -----------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=''; DIM=''; GREEN=''; CYAN=''; RESET=''
fi
step() { printf '\n%s %s\n' "${CYAN}${BOLD}[$1/3]${RESET}" "${BOLD}$2${RESET}"; }
ok()   { printf '  %s %s\n' "${GREEN}✓${RESET}" "$1"; }
note() { printf '    %s\n' "$1"; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO}:${TAG}"

printf '%s\n' "${BOLD}Claude Code on AgentCore${RESET}"
printf '  %-8s %s\n' "region"  "${REGION}"
printf '  %-8s %s\n' "model"   "${MODEL}"
printf '  %-8s %s\n' "runtime" "${RUNTIME_NAME}"
if [ "${BUILD}" = "local" ]; then
  printf '  %-8s %s\n' "image" "built locally from runtime/"
else
  printf '  %-8s %s%s%s\n' "image" "prebuilt ${DIM}" "${SOURCE_IMAGE}" "${RESET}"
fi

# --- 1. IAM execution role (idempotent) ---------------------------------------
step 1 "IAM execution role"
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
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
  note "waiting for role propagation..."
  sleep 10
  ok "created ${ROLE_NAME}"
else
  ok "${ROLE_NAME} ${DIM}(already exists)${RESET}"
fi
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Optional: let the runtime call the Gateway MCP endpoint (web search).
if [ -n "${GATEWAY_MCP_URL}" ]; then
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
  ok "granted InvokeGateway ${DIM}(Gateway web search)${RESET}"
fi

# --- 2. Container image --------------------------------------------------------
step 2 "Container image"
aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" \
       --image-scanning-configuration scanOnPush=true >/dev/null

if [ "${BUILD}" = "local" ]; then
  aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null
  note "building linux/arm64 image (first build takes a few minutes)..."
  docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    --tag "${IMAGE}" \
    --push \
    runtime/
  ok "built and pushed ${IMAGE}"
else
  SOURCE_IMAGE="${SOURCE_IMAGE}" REGION="${REGION}" REPO="${REPO}" TAG="${TAG}" \
  python3 deploy/copy_image.py
fi

# --- 3. AgentCore Runtime ------------------------------------------------------
step 3 "AgentCore Runtime"
REGION="${REGION}" RUNTIME_NAME="${RUNTIME_NAME}" IMAGE="${IMAGE}" \
ROLE_ARN="${ROLE_ARN}" MODEL="${MODEL}" GATEWAY_MCP_URL="${GATEWAY_MCP_URL}" \
python3 deploy/create_runtime.py

ARN="$(cat .runtime_arn)"
printf '\n%s %s\n' "${GREEN}${BOLD}✓ Deployed${RESET}" "${DIM}in $((SECONDS / 60))m $((SECONDS % 60))s${RESET}"
printf '  %s\n' "${ARN}"
printf '\n%s\n' "${BOLD}Next${RESET}"
printf '  %s   %s\n' './client/install.sh' "${DIM}# installs the ccr client and wires up this runtime${RESET}"
printf '  %s\n\n' 'ccr "Say hello"'
