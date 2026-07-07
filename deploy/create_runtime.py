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
    with urllib.request.urlopen(http_req) as resp:
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
        print("==> NOTE: this boto3 predates filesystemConfigurations; "
              "using a SigV4 REST call instead (consider: pip install -U boto3)")
    if existing:
        print(f"==> updating runtime {existing}")
        print("    NOTE: a runtime version update wipes managed session storage.")
        if use_sdk:
            ctl.update_agent_runtime(agentRuntimeId=existing, **kwargs)
        else:
            rest_put(f"/runtimes/{existing}/", kwargs)
        return existing
    print("==> creating runtime")
    if use_sdk:
        resp = ctl.create_agent_runtime(agentRuntimeName=NAME, **kwargs)
    else:
        resp = rest_put("/runtimes/", {"agentRuntimeName": NAME, **kwargs})
    return resp["agentRuntimeId"]


def main() -> None:
    print(f"==> region: {REGION}  name: {NAME}")
    print(f"==> image:  {IMAGE}")
    print(f"==> env:    {json.dumps(ENV_VARS)}")

    runtime_id = deploy()

    print("==> waiting for READY", flush=True)
    for _ in range(60):
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            arn = rt["agentRuntimeArn"]
            (REPO_ROOT / ".runtime_arn").write_text(arn + "\n")
            print(f"==> READY: {arn}")
            print(f"==> session storage: {json.dumps(rt.get('filesystemConfigurations'))}")
            return
        if status.endswith("_FAILED"):
            print(json.dumps(rt, default=str, indent=2), file=sys.stderr)
            sys.exit(1)
        print(f"    status={status}")
        time.sleep(5)
    print("timeout waiting for READY", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
