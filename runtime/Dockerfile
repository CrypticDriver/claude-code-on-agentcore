# AgentCore Runtime requires linux/arm64 images.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Claude Code talks to Amazon Bedrock with the runtime's IAM role — no API keys.
# AWS_REGION and ANTHROPIC_MODEL are injected at deploy time (deploy/deploy.sh)
# so the same image works in any region with any Claude model.
ENV CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_ENABLE_TELEMETRY=0

# SigV4 signing deps for the optional Gateway MCP bridge.
WORKDIR /app
RUN npm install --omit=dev --no-audit --no-fund \
      @aws-sdk/credential-provider-node \
      @smithy/signature-v4 \
      @smithy/protocol-http \
      @aws-crypto/sha256-js

COPY --chown=node:node server.js /app/server.js
COPY --chown=node:node mcp-gateway-bridge.js /app/mcp-gateway-bridge.js
COPY --chown=node:node entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER node
EXPOSE 8080
CMD ["/app/entrypoint.sh"]
