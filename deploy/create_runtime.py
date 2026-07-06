#!/usr/bin/env python3
"""
Create or update the AgentCore Runtime via boto3.

boto3 is required (not the aws CLI): older AWS CLI builds silently drop
`--filesystem-configurations`, leaving the runtime without session storage
and breaking multi-turn persistence.

Inputs via environment: REGION, RUNTIME_NAME, IMAGE, ROLE_ARN, MODEL.
Writes the runtime ARN to .runtime_arn in the repo root.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import boto3

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

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)


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
    if existing:
        print(f"==> updating runtime {existing}")
        print("    NOTE: a runtime version update wipes managed session storage.")
        ctl.update_agent_runtime(agentRuntimeId=existing, **kwargs)
        return existing
    print("==> creating runtime")
    resp = ctl.create_agent_runtime(agentRuntimeName=NAME, **kwargs)
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
