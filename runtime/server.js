/**
 * Claude Code runtime server for Amazon Bedrock AgentCore.
 *
 * Implements the AgentCore HTTP contract (GET /ping, POST /invocations on
 * port 8080) and bridges each invocation to a headless Claude Code run:
 *
 *   POST /invocations   { "prompt": string, "reset"?: boolean }
 *     -> text/event-stream
 *        event: start        { agentcore_session_id, resumed_cc_session_id, prompt_len }
 *        event: cc-session   { cc_session_id }
 *        event: cc           <one JSONL line from `claude --output-format stream-json`>
 *        event: error        { message }
 *        event: done         { exit_code, events, stderr? }
 *
 * Multi-turn: the AgentCore session id (sticky per microVM) is mapped to a
 * Claude Code session id, persisted on managed session storage, and replayed
 * via `--resume` on subsequent invocations — so conversations survive
 * microVM teardown and stop/resume cycles.
 */
const http = require('node:http');
const fs = require('node:fs');
const { spawn } = require('node:child_process');

const PORT = 8080;
const HOST = '0.0.0.0';

// Optional: expose an AgentCore Gateway (e.g. the managed web-search tool) to
// Claude Code as an MCP server. The stdio<->HTTP bridge signs with the
// runtime's IAM role — see mcp-gateway-bridge.js.
const GATEWAY_MCP_URL = process.env.GATEWAY_MCP_URL || '';
const MCP_CONFIG_FILE = '/tmp/mcp-gateway.json';

let mcpConfigPrepared = false;
function prepareMcpConfigOnce() {
  if (mcpConfigPrepared || !GATEWAY_MCP_URL) return;
  fs.writeFileSync(MCP_CONFIG_FILE, JSON.stringify({
    mcpServers: {
      gateway: { command: 'node', args: ['/app/mcp-gateway-bridge.js'] },
    },
  }));
  mcpConfigPrepared = true;
  console.log('[mcp] gateway bridge enabled:', GATEWAY_MCP_URL);
}

// AgentCore allows at most ONE managed sessionStorage mount per runtime.
// Everything that must persist lives under a single /mnt/agent-state mount.
const HOME_DIR = '/mnt/agent-state';
const WORK_DIR = '/mnt/agent-state/workspace';
const SESSION_MAP_FILE = '/mnt/agent-state/session-map.json';

let mountsPrepared = false;
function prepareMountsOnce() {
  if (mountsPrepared) return;
  for (const d of [HOME_DIR, `${HOME_DIR}/.claude`, WORK_DIR]) {
    try { fs.mkdirSync(d, { recursive: true }); } catch (e) {
      console.warn(`[mount] mkdir ${d} failed:`, e.message);
    }
  }
  mountsPrepared = true;
  console.log('[mount] prepared', HOME_DIR, WORK_DIR);
}

// Persist the agentcore->cc session map to disk. An in-memory map dies with
// the microVM; without it, --resume has nothing to resume after stop/resume.
function loadSessionMap() {
  try {
    return new Map(Object.entries(JSON.parse(fs.readFileSync(SESSION_MAP_FILE, 'utf8'))));
  } catch (e) {
    if (e.code !== 'ENOENT') console.warn('[session-map] load failed:', e.message);
    return new Map();
  }
}
function saveSessionMap(map) {
  try {
    fs.writeFileSync(SESSION_MAP_FILE, JSON.stringify(Object.fromEntries(map)));
  } catch (e) {
    console.warn('[session-map] save failed:', e.message);
  }
}

let ccSessionByAgentCore = null;
function getSessionMap() {
  if (ccSessionByAgentCore === null) ccSessionByAgentCore = loadSessionMap();
  return ccSessionByAgentCore;
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (e) { reject(e); }
    });
    req.on('error', reject);
  });
}

function writeSse(res, event, data) {
  const payload = typeof data === 'string' ? data : JSON.stringify(data);
  if (event) res.write(`event: ${event}\n`);
  for (const line of payload.split('\n')) {
    res.write(`data: ${line}\n`);
  }
  res.write('\n');
}

function runClaudeStream({ prompt, resumeCcSessionId, res, onCcSessionId, onExit }) {
  const args = [
    '--print',
    '--dangerously-skip-permissions', // see README: replace with a permission handler for production
    '--output-format', 'stream-json',
    '--verbose',
  ];
  if (GATEWAY_MCP_URL) {
    // --mcp-config is variadic; the prompt goes via stdin so it is never
    // swallowed as an extra config path.
    args.push('--mcp-config', MCP_CONFIG_FILE);
  }
  if (resumeCcSessionId) {
    args.push('--resume', resumeCcSessionId);
  }

  const child = spawn('claude', args, {
    cwd: WORK_DIR,
    env: { ...process.env, HOME: HOME_DIR },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  child.stdin.end(prompt);

  let stdoutBuf = '';
  let stderrBuf = '';
  let eventCount = 0;

  const handleLine = (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    eventCount += 1;

    // Capture the CC session id from the init event so the next turn can --resume.
    if (!resumeCcSessionId) {
      try {
        const evt = JSON.parse(trimmed);
        if (evt.type === 'system' && evt.subtype === 'init' && evt.session_id) {
          onCcSessionId(evt.session_id);
        }
      } catch { /* not JSON, forward raw */ }
    }

    writeSse(res, 'cc', trimmed);
  };

  child.stdout.on('data', (chunk) => {
    stdoutBuf += chunk.toString('utf8');
    let idx;
    while ((idx = stdoutBuf.indexOf('\n')) !== -1) {
      handleLine(stdoutBuf.slice(0, idx));
      stdoutBuf = stdoutBuf.slice(idx + 1);
    }
  });

  child.stderr.on('data', (chunk) => {
    stderrBuf += chunk.toString('utf8');
  });

  child.on('error', (err) => {
    writeSse(res, 'error', { message: String(err && err.message || err) });
    onExit(1);
  });

  child.on('close', (code) => {
    if (stdoutBuf.trim()) {
      handleLine(stdoutBuf);
    }
    writeSse(res, 'done', {
      exit_code: code,
      events: eventCount,
      stderr: stderrBuf ? stderrBuf.slice(-2000) : undefined,
    });
    onExit(code);
  });

  return child;
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/ping') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'Healthy' }));
      return;
    }

    if (req.method === 'POST' && req.url === '/invocations') {
      const body = await readJsonBody(req);
      const prompt = typeof body.prompt === 'string' ? body.prompt : '';
      if (!prompt) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'missing "prompt" string in request body' }));
        return;
      }

      const agentCoreSessionId =
        req.headers['x-amzn-bedrock-agentcore-runtime-session-id'] || null;
      const forceReset = body.reset === true;

      prepareMountsOnce();
      prepareMcpConfigOnce();
      const sessionMap = getSessionMap();

      let resumeCcSessionId = null;
      if (agentCoreSessionId && !forceReset) {
        resumeCcSessionId = sessionMap.get(agentCoreSessionId) || null;
      }

      console.log(
        `[invoke] agentcore=${agentCoreSessionId || 'none'} ` +
        `resume=${resumeCcSessionId || 'none'} prompt=${prompt.length}ch`
      );

      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
      });

      writeSse(res, 'start', {
        agentcore_session_id: agentCoreSessionId,
        resumed_cc_session_id: resumeCcSessionId,
        prompt_len: prompt.length,
      });

      const started = Date.now();
      const child = runClaudeStream({
        prompt,
        resumeCcSessionId,
        res,
        onCcSessionId: (ccSessionId) => {
          if (agentCoreSessionId) {
            sessionMap.set(agentCoreSessionId, ccSessionId);
            saveSessionMap(sessionMap);
            console.log(`[invoke] mapped ${agentCoreSessionId} -> ${ccSessionId}`);
          }
          writeSse(res, 'cc-session', { cc_session_id: ccSessionId });
        },
        onExit: (code) => {
          console.log(`[invoke] done code=${code} in ${Date.now() - started}ms`);
          res.end();
        },
      });

      req.on('close', () => {
        if (!child.killed) {
          console.log('[invoke] client disconnected, killing claude');
          child.kill('SIGTERM');
        }
      });
      return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found', method: req.method, url: req.url }));
  } catch (err) {
    console.error('[error]', err);
    if (!res.headersSent) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: String(err && err.message || err) }));
    } else {
      res.end();
    }
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[server] listening on http://${HOST}:${PORT}`);
});
