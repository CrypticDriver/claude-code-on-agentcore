# Claude Code on Amazon Bedrock AgentCore

[English](README.md) | 简体中文

让 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) **完全运行在 AWS 托管沙箱**（Amazon Bedrock AgentCore Runtime）里，本地只留一个轻量 CLI。本地机器上不执行任何 Agent 进程，也不采集任何本机信息。

**适用场景：**

- **本地零 Agent 足迹** —— 有些企业不允许编码 Agent 在员工电脑上执行（系统信息采集、任意工具执行、数据外泄风险）。用这套方案，Agent 进程、工具调用、对话记录、工作区全部落在企业自己的 AWS 账号里，员工照样获得完整的 Claude Code 体验。
- **无 API Key** —— 端到端 SigV4。客户端用标准 AWS 凭证，运行时用 IAM 执行角色调用 Amazon Bedrock 上的 Claude。
- **会话隔离** —— AgentCore 为每个会话分配独立 microVM，会话之间互不可见。
- **对话持久化** —— 对话记录与工作区通过 AgentCore 托管会话存储（闲置保留 14 天）跨 microVM 销毁存活。今天问一半，明天接着问。
- **按用量计费** —— 模型思考期间不计 CPU 费用。实测单次调用成本 **$0.016–0.04**（含 prompt 缓存）。
- **可选托管 Web 搜索** —— 一条命令接入 [AgentCore Gateway web-search 工具](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html)：Amazon 自营搜索索引，端到端 SigV4，查询不出 AWS。

## 架构

```
 开发者笔记本                         AWS 账号（企业自有）
┌──────────────────┐            ┌─────────────────────────────────────────────────┐
│                  │            │  Amazon Bedrock AgentCore Runtime               │
│   ccr（客户端）   │   SigV4    │ ┌─────────────────────────────────────────────┐ │
│                  │ ─────────► │ │  microVM（每会话一个）                       │ │
│  发送 prompt、    │            │ │ ┌──────────┐  spawn   ┌───────────────────┐ │ │
│  渲染事件流       │ ◄───────── │ │ │ server.js│ ───────► │ claude --print    │ │ │
│                  │  SSE:      │ │ │  :8080   │ ◄─────── │  --output-format  │ │ │
│  ~/.ccr/         │  tool_use, │ │ └────┬─────┘  stream  │  stream-json      │ │ │
│   session.id ────┼─ 粘性路由   │ │      │        -json   │  [--resume <id>]  │ │ │
│   runtime.arn    │            │ │      │                └────────┬──────────┘ │ │
└──────────────────┘            │ │      ▼                         │ IAM 角色   │ │
                                │ │ /mnt/agent-state               ▼            │ │
                                │ │  ├── .claude/        ┌───────────────────┐  │ │
                                │ │  ├── workspace/      │  Amazon Bedrock   │  │ │
                                │ │  └── session-map.json│  （Claude 模型）   │  │ │
                                │ └─│───────────────────┘└───────────────────┘  │ │
                                │   ▼                                           │ │
                                │  托管会话存储（保留 14 天）                     │
                                └─────────────────────────────────────────────────┘
```

**会话桥接** —— 多轮对话跨 microVM 销毁存活的关键机制：

```
客户端发送稳定的 runtimeSessionId（持久化在 ~/.ccr/session.id）
  └► AgentCore 粘性路由到该会话的 microVM
      └► server.js 从存储挂载上读 session-map.json
          ├── 首轮：  spawn claude，从 init 事件捕获 session_id，落盘保存
          └── 后续轮：spawn claude --resume <cc_session_id> → 完整上下文恢复
```

映射表存在**磁盘**（会话存储）而不是内存 —— 内存里的映射会随 microVM 一起销毁，拿不到 Claude Code session id，`--resume` 就无从谈起。

## 快速开始

前置条件：AWS CLI v2、Docker（含 buildx）、Python 3.10+ 和 boto3 ≥ 1.39。凭证需具备 ECR、IAM 角色创建和 `bedrock-agentcore-control` 权限，账号需开通 Claude 的 [Bedrock 模型访问](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)。

### 1. 部署运行时（一条命令）

```bash
./deploy/deploy.sh
```

自动完成：创建 IAM 执行角色、ECR 仓库、构建并推送 ARM64 镜像、创建带会话存储的 AgentCore 运行时。首次约 5 分钟。可通过环境变量定制：

```bash
REGION=us-west-2 MODEL=us.anthropic.claude-opus-4-7 ./deploy/deploy.sh
```

> AgentCore Runtime 目前可用区域：us-east-1、us-west-2、eu-west-1、ap-southeast-1（以[官方文档](https://docs.aws.amazon.com/general/latest/gr/bedrock-agentcore.html)为准）。

### 2. 安装客户端（一条命令）

```bash
./client/install.sh
export CC_AGENTCORE_RUNTIME_ARN=$(cat .runtime_arn)
```

### 3. 开始对话

```bash
ccr "写一个 Python 的 fibonacci 函数，保存到 fib.py"

# 多轮对话 —— 不同的 shell 命令共享同一个会话
ccr "我叫 Ada。"
ccr "我叫什么名字？"          # → "Ada"（沙箱内对话记录已恢复）

ccr --new-session "重新开始。"
ccr                          # 交互式 REPL
```

## 可选：托管 Web 搜索（AgentCore Gateway）

Claude Code 自带的 WebSearch 走 Anthropic 的搜索后端。若要求搜索查询不出 AWS，可以改接全托管的 [Gateway web-search 工具](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html) —— Amazon 自营索引，以 MCP 协议暴露，用运行时 IAM 角色调用：

```bash
python3 deploy/setup_web_search.py          # 创建 gateway + web-search target，打印 MCP URL
GATEWAY_MCP_URL=<打印的 URL> ./deploy/deploy.sh   # 带上 gateway 重新部署
```

沙箱内由 `mcp-gateway-bridge.js` 把 Claude Code 的 stdio MCP 传输桥接到 Gateway 的 SigV4 认证 HTTP 端点。之后 Claude Code 就能看到 `mcp__gateway__web-search-tool___WebSearch` 工具（query + maxResults 1–25，结果含标题/URL/摘要/日期）。已端到端验证。

注意事项：

- web-search 连接器目前**仅 us-east-1 可用**。
- **boto3 尚不认识 `connector` target 类型**（服务模型落后于 API）—— `create_gateway_target` 会直接参数校验失败。`setup_web_search.py` 用 SigV4 签名的原始 REST 调用绕过。
- 更新运行时版本会**清空会话存储**（已有对话重置）—— 与任何镜像更新相同的注意事项。

## 实测性能

在 us-east-1 用 Claude Opus 采集（5 个并发冷启动会话 + 热复用）：

| 指标 | 冷启动 | 热复用 |
|---|---|---|
| 首字节时间（p50 / p95） | 3.18s / 3.51s | 快约 20% |
| 端到端耗时（p50 / p95） | 6.44s / 13.41s | 取决于模型 |
| 单次调用成本 | $0.02–0.04 | $0.016（prompt 缓存命中） |
| 是否观察到节流 | 无 | 无 |

耗时波动来自模型池，不是 AgentCore。

## 运维要点

- **每运行时仅一个会话存储挂载**（API 强制）。所有需持久化的内容放在同一挂载下按子目录切分，参考 `server.js` 的布局。
- **运行时版本更新会清空会话存储** —— 官方文档明确的行为。升级镜像前先通知用户 / 做好快照。
- **部署要用 boto3 / SDK，不要用 AWS CLI** —— 旧版 CLI 会静默丢弃 `--filesystem-configurations`，多轮对话会悄悄失效。
- **客户端 region 必须与运行时 ARN 的 region 一致** —— 不一致会报误导性错误 `No endpoint or agent found with qualifier 'DEFAULT'`。客户端直接从 ARN 解析 region，规避这个坑。
- **会话生命周期**：microVM 闲置 15 分钟停机、最长存活 8 小时、会话状态保留 14 天。得益于落盘的会话映射表，三种情况下对话都能透明恢复。

## 生产化清单

本仓库是可运行的参考实现，不是成品企业级产品。大规模推广前需要补齐：

- [ ] **权限控制** —— 容器内目前是 `claude --dangerously-skip-permissions`。需替换为显式权限处理器（如 MCP 权限服务），并对每次工具调用留审计记录。
- [ ] **用户级身份** —— 所有会话共用一个运行时执行角色。用 AgentCore Identity 出站认证实现用户级 git 凭证和 Bedrock 分账。
- [ ] **工作区同步** —— Claude Code 创建的文件在沙箱里。若用户需要拉回本地，加一层 git 中介同步。
- [ ] **网络模式** —— 部署脚本为简化使用 `PUBLIC`，生产应走 VPC。
- [ ] **认证方式** —— 这里只用了 SigV4；如果用户没有 AWS 凭证，AgentCore 也支持 OAuth（JWT bearer）。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `runtime/` | 容器镜像：Dockerfile、entrypoint、SSE 桥接服务、Gateway MCP 桥 |
| `deploy/` | 一键部署：`deploy.sh`（IAM + ECR + 镜像）、`create_runtime.py`（boto3）、`setup_web_search.py`（可选 Gateway Web 搜索） |
| `client/` | `ccr` 命令行客户端及安装脚本 |

## 协议

容器实现标准 AgentCore HTTP 契约，以 SSE 流式返回：

```
POST /invocations   { "prompt": string, "reset"?: boolean }
  → text/event-stream
     event: start        { agentcore_session_id, resumed_cc_session_id, prompt_len }
     event: cc-session   { cc_session_id }
     event: cc           <claude --output-format stream-json 的一行 JSONL>
     event: error        { message }
     event: done         { exit_code, events, stderr? }
```

任何支持 SSE 的客户端都能对接 —— 自带的 `ccr` 只有约 200 行 Python，协议也很容易封装进其他工具。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
