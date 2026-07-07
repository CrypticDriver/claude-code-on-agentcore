#!/usr/bin/env python3
"""
Docker-free image copy: prebuilt public image -> your private ECR.

AgentCore Runtime requires the container image to live in a private ECR
repository in your own account and region, so a public image cannot be
referenced directly. This script copies it there using only the Docker
Registry HTTP API v2 (boto3 + stdlib) — no Docker daemon needed.

Inputs via environment:
  SOURCE_IMAGE  e.g. public.ecr.aws/<alias>/claude-code-agentcore:latest
  REGION        target ECR region
  REPO          target ECR repository name
  TAG           target tag
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import boto3

SOURCE_IMAGE = os.environ["SOURCE_IMAGE"]
REGION = os.environ["REGION"]
REPO = os.environ["REPO"]
TAG = os.environ.get("TAG", "latest")

CHUNK = 8 * 1024 * 1024

MANIFEST_TYPES = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])


def make_ssl_context() -> ssl.SSLContext:
    """python.org builds of Python on macOS ship without CA certs wired up;
    fall back to certifi's bundle or the one botocore ships (boto3 is a hard
    prerequisite, so at least one of these is always available)."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"] > 0:
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    import botocore
    bundle = os.path.join(os.path.dirname(botocore.__file__), "cacert.pem")
    if os.path.exists(bundle):
        return ssl.create_default_context(cafile=bundle)
    return ctx


SSL_CONTEXT = make_ssl_context()


def http(url: str, method: str = "GET", headers: dict | None = None,
         data=None, stream: bool = False):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    resp = urllib.request.urlopen(req, context=SSL_CONTEXT)
    return resp if stream else (resp.status, dict(resp.headers), resp.read())


# --- source: public registry (anonymous bearer token) -------------------------

def parse_source(image: str) -> tuple[str, str, str]:
    host, rest = image.split("/", 1)
    repo, _, tag = rest.rpartition(":")
    if not repo:
        repo, tag = rest, "latest"
    return host, repo, tag


SRC_HOST, SRC_REPO, SRC_TAG = parse_source(SOURCE_IMAGE)
_src_token: str | None = None


def src_headers(accept: str = MANIFEST_TYPES) -> dict:
    global _src_token
    if _src_token is None:
        # public.ecr.aws issues anonymous pull tokens from /token/
        _, _, body = http(f"https://{SRC_HOST}/token/?scope=repository:{SRC_REPO}:pull")
        _src_token = json.loads(body)["token"]
    return {"Authorization": f"Bearer {_src_token}", "Accept": accept}


def src_manifest(reference: str) -> tuple[dict, str, bytes]:
    _, headers, body = http(
        f"https://{SRC_HOST}/v2/{SRC_REPO}/manifests/{reference}",
        headers=src_headers())
    return json.loads(body), headers.get("Content-Type", ""), body


# --- target: private ECR (basic auth from boto3) ------------------------------

session = boto3.Session()
ecr = session.client("ecr", region_name=REGION)
ACCOUNT_ID = session.client("sts").get_caller_identity()["Account"]
DST_HOST = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"

auth = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
DST_AUTH = {"Authorization": f"Basic {auth}"}


def dst_blob_exists(digest: str) -> bool:
    try:
        http(f"https://{DST_HOST}/v2/{REPO}/blobs/{digest}", method="HEAD", headers=DST_AUTH)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def copy_blob(digest: str, size: int) -> None:
    if dst_blob_exists(digest):
        print(f"    blob {digest[:19]}… exists ({size} bytes)")
        return
    print(f"    blob {digest[:19]}… copying ({size} bytes)")
    src = http(f"https://{SRC_HOST}/v2/{SRC_REPO}/blobs/{digest}",
               headers=src_headers(accept="*/*"), stream=True)

    _, headers, _ = http(f"https://{DST_HOST}/v2/{REPO}/blobs/uploads/",
                         method="POST", headers=DST_AUTH, data=b"")
    location = headers["Location"]
    if location.startswith("/"):
        location = f"https://{DST_HOST}{location}"

    offset = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            break
        h = dict(DST_AUTH)
        h.update({
            "Content-Type": "application/octet-stream",
            "Content-Range": f"{offset}-{offset + len(chunk) - 1}",
            "Content-Length": str(len(chunk)),
        })
        _, headers, _ = http(location, method="PATCH", headers=h, data=chunk)
        location = headers["Location"]
        if location.startswith("/"):
            location = f"https://{DST_HOST}{location}"
        offset += len(chunk)

    sep = "&" if "?" in location else "?"
    http(f"{location}{sep}digest={digest}", method="PUT",
         headers={**DST_AUTH, "Content-Length": "0"}, data=b"")


def put_manifest(reference: str, media_type: str, body: bytes) -> None:
    http(f"https://{DST_HOST}/v2/{REPO}/manifests/{reference}", method="PUT",
         headers={**DST_AUTH, "Content-Type": media_type}, data=body)


def copy_image_manifest(manifest: dict, media_type: str, raw: bytes, reference: str) -> None:
    blobs = [manifest["config"]] + manifest["layers"]
    for blob in blobs:
        copy_blob(blob["digest"], blob.get("size", 0))
    put_manifest(reference, media_type, raw)


def main() -> None:
    print(f"==> copy {SOURCE_IMAGE}")
    print(f"    ->  {DST_HOST}/{REPO}:{TAG}")

    manifest, media_type, raw = src_manifest(SRC_TAG)

    if manifest.get("mediaType", media_type).endswith(("list.v2+json", "index.v1+json")):
        # Multi-arch index: AgentCore needs linux/arm64.
        arm = next((m for m in manifest["manifests"]
                    if m.get("platform", {}).get("architecture") == "arm64"
                    and m.get("platform", {}).get("os") == "linux"), None)
        if not arm:
            sys.exit("error: source image has no linux/arm64 manifest")
        child, child_type, child_raw = src_manifest(arm["digest"])
        copy_image_manifest(child, child_type, child_raw, arm["digest"])
        put_manifest(TAG, media_type, raw)
    else:
        copy_image_manifest(manifest, media_type, raw, TAG)

    print(f"==> done: {DST_HOST}/{REPO}:{TAG}")


if __name__ == "__main__":
    main()
