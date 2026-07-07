#!/usr/bin/env python3
"""
Create or update the AgentCore Runtime.

boto3 is required (not the aws CLI): older AWS CLI builds silently drop
`--filesystem-configurations`, leaving the runtime without session storage
and breaking multi-turn persistence. Older boto3 releases don't know the
parameter either — in that case the create/update call is sent as a raw
SigV4-signed REST request instead, so any boto3 works.

Inputs via environment: REGION, RUNTIME_NAME, IMAGE, ROLE_ARN, MODEL.
Writes the runtime ARN to .runtime_arn in the repo root.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tlsctx import SSL_CONTEXT

REPO_ROOT = Path(__file__).parent.parent
REGION = os.environ["REGION"]
NAME = os.environ["RUNTIME_NAME"]
IMAGE = os.environ["IMAGE"]
ROLE_ARN = os.environ["ROLE_ARN"]
MODEL = os.environ["MODEL"]

# AgentCore allows at most ONE sessionStorage mount per runtime; the
# container lays out HOME, workspace and the session map under it.
FS_CONFIGS = [{"sessionStorage": {"mountPath": "/mnt/agent-state"}}]

ENV_VARS = {
    "AWS_REGION": REGION,
    "ANTHROPIC_MODEL": MODEL,
}
# Optional: AgentCore Gateway MCP endpoint (see deploy/setup_web_search.py).
if os.environ.get("GATEWAY_MCP_URL"):
    ENV_VARS["GATEWAY_MCP_URL"] = os.environ["GATEWAY_MCP_URL"]

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
GREEN, DIM, RESET = ("\033[32m", "\033[2m", "\033[0m") if TTY else ("", "", "")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def note(msg: str) -> None:
    print(f"    {msg}")


def status_line(msg: str) -> None:
    if TTY:
        print(f"\r\033[2K    {msg}", end="", flush=True)
    else:
        print(f"    {msg}")


def status_done() -> None:
    if TTY:
        print("\r\033[2K", end="", flush=True)


session = boto3.Session()
ctl = session.client("bedrock-agentcore-control", region_name=REGION)


def sdk_supports_session_storage() -> bool:
    shape = ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape
    return "filesystemConfigurations" in shape.members


def rest_put(path: str, body: dict) -> dict:
    """Raw SigV4 fallback for boto3 releases without filesystemConfigurations."""
    host = f"bedrock-agentcore-control.{REGION}.amazonaws.com"
    url = f"https://{host}{path}"
    data = json.dumps(body)
    req = AWSRequest(method="PUT", url=url, data=data,
                     headers={"Content-Type": "application/json", "Host": host})
    SigV4Auth(session.get_credentials().get_frozen_credentials(),
              "bedrock-agentcore", REGION).add_auth(req)
    http_req = urllib.request.Request(url, data=data.encode(),
                                      headers=dict(req.headers), method="PUT")
    with urllib.request.urlopen(http_req, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def find_existing() -> str | None:
    resp = ctl.list_agent_runtimes()
    for rt in resp.get("agentRuntimes", []):
        if rt.get("agentRuntimeName") == NAME:
            return rt["agentRuntimeId"]
    return None


def deploy() -> str:
    kwargs = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": IMAGE}},
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        roleArn=ROLE_ARN,
        environmentVariables=ENV_VARS,
        filesystemConfigurations=FS_CONFIGS,
    )
    existing = find_existing()
    use_sdk = sdk_supports_session_storage()
    if not use_sdk:
        note(f"{DIM}this boto3 predates filesystemConfigurations; using a "
             f"SigV4 REST call instead (consider: pip install -U boto3){RESET}")
    if existing:
        note(f"updating existing runtime {existing}")
        note(f"{DIM}note: a runtime version update wipes managed session storage{RESET}")
        if use_sdk:
            ctl.update_agent_runtime(agentRuntimeId=existing, **kwargs)
        else:
            rest_put(f"/runtimes/{existing}/", kwargs)
        return existing
    note("creating runtime...")
    if use_sdk:
        resp = ctl.create_agent_runtime(agentRuntimeName=NAME, **kwargs)
    else:
        resp = rest_put("/runtimes/", {"agentRuntimeName": NAME, **kwargs})
    return resp["agentRuntimeId"]


def main() -> None:
    runtime_id = deploy()

    for i in range(60):
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            status_done()
            arn = rt["agentRuntimeArn"]
            (REPO_ROOT / ".runtime_arn").write_text(arn + "\n")
            ok(f"runtime READY {DIM}(session storage mounted at /mnt/agent-state){RESET}")
            return
        if status.endswith("_FAILED"):
            status_done()
            print(json.dumps(rt, default=str, indent=2), file=sys.stderr)
            sys.exit(1)
        status_line(f"waiting for READY... {DIM}(status={status}, {i * 5}s){RESET}")
        time.sleep(5)
    status_done()
    print("timeout waiting for READY", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
