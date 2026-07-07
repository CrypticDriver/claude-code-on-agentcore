# Claude Code on Amazon Bedrock AgentCore

English | [简体中文](README.zh-CN.md)

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) **entirely inside an AWS-managed sandbox** — Amazon Bedrock AgentCore Runtime — and talk to it from a thin local CLI. Nothing executes on, or is collected from, the local machine.

**Why you might want this:**

- **Zero local agent footprint** — organizations that cannot allow a coding agent to execute on employee workstations (system-information collection, arbitrary tool execution, data-exfiltration concerns) get the full Claude Code experience with the agent process, its tools, transcripts, and workspace all inside a company-owned AWS account.
- **No API keys** — SigV4 end to end. The client uses standard AWS credentials; the runtime calls Claude on Amazon Bedrock with its IAM execution role.
- **Session isolation** — AgentCore gives every session a dedicated microVM. Sessions cannot see each other.
- **Persistent conversations** — transcript and workspace survive microVM teardown via AgentCore managed session storage (14-day idle retention). Ask a question today, follow up tomorrow.
- **Consumption billing** — CPU is not billed while the model is thinking. Measured cost: **$0.016–0.04 per invocation** (with prompt caching).
- **Optional managed web search** — one command adds the [AgentCore Gateway web-search tool](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html): Amazon-operated search index, SigV4 all the way, queries never leave AWS.

## Architecture

```
 Developer laptop                    AWS account (yours)
┌──────────────────┐            ┌─────────────────────────────────────────────────┐
│                  │            │  Amazon Bedrock AgentCore Runtime               │
│   ccr (client)   │   SigV4    │ ┌─────────────────────────────────────────────┐ │
│                  │ ─────────► │ │  microVM (one per session)                  │ │
│  sends prompt,   │            │ │ ┌──────────┐  spawn   ┌───────────────────┐ │ │
│  renders stream  │ ◄───────── │ │ │ server.js│ ───────► │ claude --print    │ │ │
│                  │  SSE:      │ │ │  :8080   │ ◄─────── │  --output-format  │ │ │
│  ~/.ccr/         │  tool_use, │ │ └────┬─────┘  stream  │  stream-json      │ │ │
│   session.id ────┼─ sticky    │ │      │        -json   │  [--resume <id>]  │ │ │
│   runtime.arn    │  routing   │ │      │                └────────┬──────────┘ │ │
└──────────────────┘            │ │      ▼                         │ IAM role   │ │
                                │ │ /mnt/agent-state               ▼            │ │
                                │ │  ├── .claude/        ┌───────────────────┐  │ │
                                │ │  ├── workspace/      │  Amazon Bedrock   │  │ │
                                │ │  └── session-map.json│  (Claude models)  │  │ │
                                │ └─│───────────────────┘└───────────────────┘  │ │
                                │   ▼                                           │ │
                                │  managed session storage (14-day retention)   │
                                └─────────────────────────────────────────────────┘
```

**Session bridging** — what makes multi-turn work across microVM teardown:

```
client sends stable runtimeSessionId (persisted in ~/.ccr/session.id)
  └► AgentCore sticky-routes to the session's microVM
      └► server.js looks up session-map.json on the storage mount
          ├── first turn:  spawn claude, capture session_id from init event, save map
          └── later turns: spawn claude --resume <cc_session_id>  → full context restored
```

The map lives **on disk** (session storage), not in memory — an in-memory map dies with the microVM, and without the Claude Code session id there is nothing to `--resume`.

## Quick Start

Three commands: deploy, install, talk.

**Prerequisites:** AWS CLI v2 (with credentials configured) and Python 3.10+ with boto3 ≥ 1.39 — **no Docker needed**. Your credentials need ECR, IAM role creation, and `bedrock-agentcore-control` permissions, and the account needs [Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) for Claude.

```bash
git clone https://github.com/CrypticDriver/claude-code-on-agentcore.git
cd claude-code-on-agentcore
```

### 1. Deploy the runtime (one command)

```bash
./deploy/deploy.sh
```

This creates the IAM execution role and ECR repository, copies the **prebuilt ARM64 image** from ECR Public into your account (AgentCore requires the image in your own private ECR — the copy runs over the registry HTTP API, no Docker daemon involved), and creates the AgentCore runtime with session storage. Takes ~2 minutes. Options via env vars:

```bash
REGION=us-west-2 MODEL=us.anthropic.claude-opus-4-7 ./deploy/deploy.sh
BUILD=local ./deploy/deploy.sh    # build the image from runtime/ yourself (needs Docker buildx)
```

> AgentCore Runtime is available in us-east-1, us-west-2, eu-west-1, ap-southeast-1 (check [current availability](https://docs.aws.amazon.com/general/latest/gr/bedrock-agentcore.html)).

### 2. Install the client (one command)

```bash
./client/install.sh
```

Puts `ccr` on your PATH and — if step 1 ran in the same checkout — configures the runtime ARN automatically. On another machine, set it manually: `export CC_AGENTCORE_RUNTIME_ARN=<arn>`.

### 3. Talk to it

```bash
ccr                          # interactive session — just type prompts
```

The very first turn cold-starts a sandbox microVM and initializes Claude Code, so expect ~30 s; later turns take a few seconds. One-off prompts work too, and separate shell commands share one conversation:

```bash
ccr "Write a fibonacci function in Python and save it to fib.py"
ccr "My name is Ada."
ccr "What's my name?"        # → "Ada" (transcript resumed inside the sandbox)
ccr --new-session            # start a fresh conversation
```

## Optional: managed web search (AgentCore Gateway)

Claude Code's built-in WebSearch calls out through Anthropic's search backend. If you want search queries to stay inside AWS, wire in the fully managed [Gateway web-search tool](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html) instead — an Amazon-operated index exposed over MCP, invoked with the runtime's IAM role:

```bash
python3 deploy/setup_web_search.py          # creates gateway + web-search target, prints the MCP URL
GATEWAY_MCP_URL=<printed url> ./deploy/deploy.sh   # redeploy with the gateway wired in
```

Inside the sandbox, `mcp-gateway-bridge.js` bridges Claude Code's stdio MCP transport to the Gateway's SigV4-authenticated HTTP endpoint. Claude Code then sees an `mcp__gateway__web-search-tool___WebSearch` tool (query + maxResults 1–25, results with title/URL/snippet/date). Verified end to end.

Notes:

- The web-search connector is currently **us-east-1 only**.
- **boto3 does not yet know the `connector` target type** (service model lags the API) — `create_gateway_target` fails parameter validation. `setup_web_search.py` works around this with a raw SigV4-signed REST call.
- Redeploying the runtime version **wipes session storage** (existing conversations reset) — same caveat as any image update.

## Measured performance

Collected against us-east-1 with Claude Opus (5 concurrent cold sessions, then warm re-invocations):

| Metric | Cold | Warm |
|---|---|---|
| Time to first byte (p50 / p95) | 3.18s / 3.51s | ~20% faster |
| End-to-end wall (p50 / p95) | 6.44s / 13.41s | model-bound |
| Cost per invocation | $0.02–0.04 | $0.016 (prompt cache) |
| Throttling observed | none | none |

Wall-time variance comes from the model pool, not AgentCore.

## Operational notes

- **One session-storage mount per runtime** (API-enforced). Lay out everything under it with subdirectories, as `server.js` does.
- **Runtime version updates wipe session storage** — documented behavior. Warn users / snapshot first.
- **Use boto3 / SDKs to deploy, not the AWS CLI** — older CLI builds silently drop `--filesystem-configurations`, and multi-turn quietly breaks.
- **Client region must match the runtime ARN's region** — a mismatch fails with a misleading `No endpoint or agent found with qualifier 'DEFAULT'` error. The client parses the region from the ARN to avoid this.
- **Session lifecycle**: microVM idles out after 15 min, max lifetime 8 h, session state retained 14 days. Conversations resume transparently across all three thanks to the on-disk session map.

## Production hardening checklist

This repo is a working reference, not a finished enterprise product. Before broad rollout:

- [ ] **Permission handling** — the container runs `claude --dangerously-skip-permissions`. Replace with an explicit permission handler (e.g., an MCP permission server) plus an audit trail of every tool call.
- [ ] **Per-user identity** — all sessions share the runtime execution role. Use AgentCore Identity outbound auth for per-user git credentials and per-user Bedrock cost attribution.
- [ ] **Workspace sync** — files Claude Code creates live in the sandbox. Add git-mediated sync if users need them locally.
- [ ] **Network mode** — the deploy script uses `PUBLIC` for simplicity; production should use VPC networking.
- [ ] **Auth** — SigV4 only here; AgentCore also supports OAuth (JWT bearer) if your users don't have AWS credentials.

## Repository layout

| Path | What it is |
|---|---|
| `runtime/` | Container image: Dockerfile, entrypoint, the SSE bridge server, and the Gateway MCP bridge |
| `deploy/` | One-click deploy: `deploy.sh` (IAM + ECR + image), `copy_image.py` (Docker-free image copy), `create_runtime.py` (boto3), `setup_web_search.py` (optional Gateway web search) |
| `client/` | `ccr` CLI and its installer |

## Protocol

The container speaks the standard AgentCore HTTP contract, streaming SSE:

```
POST /invocations   { "prompt": string, "reset"?: boolean }
  → text/event-stream
     event: start        { agentcore_session_id, resumed_cc_session_id, prompt_len }
     event: cc-session   { cc_session_id }
     event: cc           <one JSONL line from claude --output-format stream-json>
     event: error        { message }
     event: done         { exit_code, events, stderr? }
```

Any SSE-capable client works — the bundled `ccr` is ~200 lines of Python, and the protocol is easy to wrap in other tools.

## License

MIT — see [LICENSE](LICENSE).
