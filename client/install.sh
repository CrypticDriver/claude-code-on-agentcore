#!/usr/bin/env bash
#
# One-click client install: puts `ccr` on PATH and checks prerequisites.
#
#   ./client/install.sh
#   ccr "Say hello"
set -euo pipefail

cd "$(dirname "$0")"

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

if ! command -v python3 >/dev/null; then
  echo "error: python3 is required" >&2
  exit 1
fi

if ! python3 -c "import boto3" 2>/dev/null; then
  echo "==> installing boto3 (user site)"
  python3 -m pip install --user --quiet boto3 \
    || { python3 -m ensurepip --user && python3 -m pip install --user --quiet boto3; }
fi

python3 - <<'EOF'
import boto3
if not hasattr(boto3.Session(), 'client'):
    raise SystemExit(1)
c = boto3.Session().client('bedrock-agentcore', region_name='us-east-1')
EOF

mkdir -p "${BIN_DIR}"
install -m 0755 bin/ccr "${BIN_DIR}/ccr"
echo "==> installed ${BIN_DIR}/ccr"

case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo "==> NOTE: add ${BIN_DIR} to your PATH:"
     echo "    export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# If deploy.sh already ran in this checkout, wire the runtime ARN automatically.
if [ -f ../.runtime_arn ]; then
  mkdir -p "$HOME/.ccr"
  cp ../.runtime_arn "$HOME/.ccr/runtime.arn"
  echo "==> runtime ARN configured from .runtime_arn:"
  echo "    $(cat "$HOME/.ccr/runtime.arn")"
  echo
  echo "Next:"
  echo "  ccr                  # interactive session"
  echo "  ccr \"Say hello\"      # or one-off prompts"
else
  echo
  echo "Next:"
  echo "  export CC_AGENTCORE_RUNTIME_ARN=<your runtime ARN>   # or: echo <arn> > ~/.ccr/runtime.arn"
  echo "  ccr                  # interactive session"
fi
