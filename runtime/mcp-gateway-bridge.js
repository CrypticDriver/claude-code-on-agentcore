/**
 * stdio <-> AgentCore Gateway MCP bridge.
 *
 * Claude Code speaks MCP over stdio (newline-delimited JSON-RPC); an
 * AgentCore Gateway speaks MCP over streamable HTTP with SigV4 auth.
 * This bridge forwards each JSON-RPC message to the Gateway endpoint,
 * signing with the runtime's IAM role (same default credential chain
 * Claude Code already uses for Bedrock) — no API keys, no OAuth.
 *
 * Configuration: GATEWAY_MCP_URL, e.g.
 *   https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
 */
const https = require('node:https');
const readline = require('node:readline');

const { defaultProvider } = require('@aws-sdk/credential-provider-node');
const { SignatureV4 } = require('@smithy/signature-v4');
const { HttpRequest } = require('@smithy/protocol-http');
const { Sha256 } = require('@aws-crypto/sha256-js');

const GATEWAY_MCP_URL = process.env.GATEWAY_MCP_URL;
if (!GATEWAY_MCP_URL) {
  process.stderr.write('mcp-gateway-bridge: GATEWAY_MCP_URL is not set\n');
  process.exit(1);
}

const url = new URL(GATEWAY_MCP_URL);
// Hostname: <gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com
const regionMatch = url.hostname.match(/\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com$/);
if (!regionMatch) {
  process.stderr.write(`mcp-gateway-bridge: cannot parse region from ${url.hostname}\n`);
  process.exit(1);
}

const signer = new SignatureV4({
  credentials: defaultProvider(),
  region: regionMatch[1],
  service: 'bedrock-agentcore',
  sha256: Sha256,
});

async function post(body) {
  const request = new HttpRequest({
    method: 'POST',
    protocol: 'https:',
    hostname: url.hostname,
    path: url.pathname,
    headers: {
      host: url.hostname,
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
    },
    body,
  });
  const signed = await signer.sign(request);

  return new Promise((resolve, reject) => {
    const req = https.request(
      { method: 'POST', hostname: url.hostname, path: url.pathname, headers: signed.headers },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => resolve({
          status: res.statusCode,
          contentType: res.headers['content-type'] || '',
          body: Buffer.concat(chunks).toString('utf8'),
        }));
      }
    );
    req.on('error', reject);
    req.end(body);
  });
}

// The Gateway may answer as plain JSON or as an SSE stream; extract every
// JSON-RPC message either way.
function extractMessages({ contentType, body }) {
  if (contentType.includes('text/event-stream')) {
    const messages = [];
    for (const line of body.split('\n')) {
      if (line.startsWith('data:')) {
        const data = line.slice(5).trim();
        if (data) messages.push(data);
      }
    }
    return messages;
  }
  const trimmed = body.trim();
  return trimmed ? [trimmed] : [];
}

async function forward(line) {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }

  try {
    const res = await post(line);
    if (res.status >= 400) throw new Error(`gateway returned HTTP ${res.status}: ${res.body.slice(0, 300)}`);
    for (const out of extractMessages(res)) {
      process.stdout.write(out + '\n');
    }
  } catch (err) {
    // Notifications (no id) expect no response; only requests need an error reply.
    if (msg.id !== undefined && msg.id !== null) {
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0',
        id: msg.id,
        error: { code: -32000, message: String(err && err.message || err) },
      }) + '\n');
    } else {
      process.stderr.write(`mcp-gateway-bridge: ${err && err.message || err}\n`);
    }
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
let chain = Promise.resolve();
rl.on('line', (line) => {
  if (!line.trim()) return;
  chain = chain.then(() => forward(line)); // preserve request order
});
rl.on('close', () => { chain.finally(() => process.exit(0)); });
