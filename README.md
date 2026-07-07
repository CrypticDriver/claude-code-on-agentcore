# Claude Code on Amazon Bedrock AgentCore

English | [简体中文](README.zh-CN.md)

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) **entirely inside an AWS-managed sandbox** — Amazon Bedrock AgentCore Runtime — and talk to it from a thin local CLI. Nothing executes on, or is collected from, the local machine.

**Why you might want this:**

- **Zero local agent footprint** — organizations that cannot allow a coding agent to execute on employee workstations (system-information collection, arbitrary tool execution, data-exfiltration concerns) get the full Claude Code experience with the agent process, its tools, transcripts, and workspace all inside a company-owned AWS account.
- **No API keys** — SigV4 end to end. The client uses standard AWS credentials; the runtime calls Claude on Amazon Bedrock with its IAM execution role.
- **Session isolation** — AgentCore gives every session a dedicated microVM. Sessions cannot see each other.
- **Persistent conversations** — transcript and workspace survive microVM teardown via AgentCore managed session storage (14-day idle retention). Ask a question today, follow up tomorrow.
- **Consumption billing** — CPU is not billed while the model is thinking. Measured cost: **$0.016–0.04 per invocation** (with prompt caching).

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

## Quickstart

Prerequisites: AWS CLI v2, Docker (with buildx), Python 3.10+ with boto3 ≥ 1.39. Your credentials need ECR, IAM role creation, and `bedrock-agentcore-control` permissions, and the account needs [Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) for Claude.

### 1. Deploy the runtime (one command)

```bash
./deploy/deploy.sh
```

This creates the IAM execution role, ECR repository, builds and pushes the ARM64 image, and creates the AgentCore runtime with session storage. Takes ~5 minutes on first run. Options via env vars:

```bash
REGION=us-west-2 MODEL=us.anthropic.claude-opus-4-7 ./deploy/deploy.sh
```

> AgentCore Runtime is available in us-east-1, us-west-2, eu-west-1, ap-southeast-1 (check [current availability](https://docs.aws.amazon.com/general/latest/gr/bedrock-agentcore.html)).

### 2. Install the client (one command)

```bash
./client/install.sh
export CC_AGENTCORE_RUNTIME_ARN=$(cat .runtime_arn)
```

### 3. Talk to it

```bash
ccr "Write a fibonacci function in Python and save it to fib.py"

# Multi-turn — separate shell commands share one conversation
ccr "My name is Ada."
ccr "What's my name?"        # → "Ada" (transcript resumed inside the sandbox)

ccr --new-session "Start over."
ccr                          # interactive REPL
```

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
| `runtime/` | Container image: Dockerfile, entrypoint, and the SSE bridge server |
| `deploy/` | One-click deploy: `deploy.sh` (IAM + ECR + image) and `create_runtime.py` (boto3) |
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
