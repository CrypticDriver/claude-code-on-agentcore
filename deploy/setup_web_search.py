#!/usr/bin/env python3
"""
One-click setup for the AgentCore Gateway managed Web Search tool.

Creates (idempotently):
  1. a Gateway service role allowed to invoke the managed web-search tool
  2. an MCP Gateway with AWS_IAM (SigV4) inbound auth
  3. a gateway target backed by the managed `web-search` connector

Prints the Gateway MCP URL. Pass it to deploy.sh as GATEWAY_MCP_URL to give
Claude Code inside the runtime an Amazon-operated web search tool (queries
never leave AWS).

NOTE: the `connector` target type is newer than the current boto3 service
model, so create_gateway_target is called via a raw SigV4-signed REST request
instead of the SDK method. Revisit once boto3 catches up.

Usage:  REGION=us-east-1 python3 deploy/setup_web_search.py
        (web-search connector is currently available in us-east-1 only)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tlsctx import SSL_CONTEXT

REGION = os.environ.get("REGION", "us-east-1")
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "claude-code-web-search")
ROLE_NAME = os.environ.get("GATEWAY_ROLE_NAME", "AgentCoreGatewayWebSearchRole")
TARGET_NAME = "web-search-tool"

session = boto3.Session()
ACCOUNT_ID = session.client("sts").get_caller_identity()["Account"]
iam = session.client("iam")
ctl = session.client("bedrock-agentcore-control", region_name=REGION)


def ensure_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/*"},
            },
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
             "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/*"},
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch",
             "Resource": f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1"},
        ],
    }
    try:
        arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="AgentCore Gateway service role for the managed web-search connector",
        )["Role"]["Arn"]
        print(f"==> created role {arn}")
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print(f"==> role exists: {arn}")
        created = False
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="web-search", PolicyDocument=json.dumps(policy))
    if created:
        print("==> waiting for role propagation")
        time.sleep(10)
    return arn


def ensure_gateway(role_arn: str) -> dict:
    for gw in ctl.list_gateways().get("items", []):
        if gw.get("name") == GATEWAY_NAME:
            print(f"==> gateway exists: {gw['gatewayId']}")
            return ctl.get_gateway(gatewayIdentifier=gw["gatewayId"])
    resp = ctl.create_gateway(
        name=GATEWAY_NAME,
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="AWS_IAM",
    )
    print(f"==> created gateway {resp['gatewayId']}")
    return resp


def wait_gateway(gateway_id: str) -> dict:
    for _ in range(60):
        gw = ctl.get_gateway(gatewayIdentifier=gateway_id)
        if gw["status"] == "READY":
            return gw
        if gw["status"].endswith("FAILED"):
            sys.exit(f"gateway failed: {gw.get('statusReasons')}")
        time.sleep(5)
    sys.exit("timeout waiting for gateway READY")


def rest_call(method: str, path: str, body: dict | None = None) -> dict:
    """Raw SigV4 call — needed while boto3 lacks the `connector` target type."""
    host = f"bedrock-agentcore-control.{REGION}.amazonaws.com"
    url = f"https://{host}{path}"
    data = json.dumps(body) if body is not None else None
    req = AWSRequest(method=method, url=url, data=data,
                     headers={"Content-Type": "application/json", "Host": host})
    SigV4Auth(session.get_credentials().get_frozen_credentials(),
              "bedrock-agentcore", REGION).add_auth(req)
    http_req = urllib.request.Request(
        url, data=data.encode() if data else None, headers=dict(req.headers), method=method)
    with urllib.request.urlopen(http_req, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read())


def ensure_target(gateway_id: str) -> str:
    existing = rest_call("GET", f"/gateways/{gateway_id}/targets/")
    for t in existing.get("items", []):
        if t.get("name") == TARGET_NAME:
            print(f"==> target exists: {t['targetId']}")
            return t["targetId"]
    resp = rest_call("POST", f"/gateways/{gateway_id}/targets/", {
        "name": TARGET_NAME,
        "targetConfiguration": {"mcp": {"connector": {
            "source": {"connectorId": "web-search"},
            "configurations": [{"name": "WebSearch", "parameterValues": {}}],
        }}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    })
    print(f"==> created target {resp['targetId']}")
    return resp["targetId"]


def wait_target(gateway_id: str, target_id: str) -> None:
    for _ in range(60):
        t = rest_call("GET", f"/gateways/{gateway_id}/targets/{target_id}")
        if t["status"] == "READY":
            return
        if t["status"].endswith("FAILED"):
            sys.exit(f"target failed: {t.get('statusReasons')}")
        time.sleep(5)
    sys.exit("timeout waiting for target READY")


def main() -> None:
    print(f"==> region: {REGION}  account: {ACCOUNT_ID}")
    role_arn = ensure_role()
    gw = ensure_gateway(role_arn)
    gw = wait_gateway(gw["gatewayId"])
    target_id = ensure_target(gw["gatewayId"])
    wait_target(gw["gatewayId"], target_id)
    print()
    print(f"==> Web search gateway READY: {gw['gatewayUrl']}")
    print("==> Wire it into the runtime:")
    print(f"    GATEWAY_MCP_URL={gw['gatewayUrl']} ./deploy/deploy.sh")


if __name__ == "__main__":
    main()
