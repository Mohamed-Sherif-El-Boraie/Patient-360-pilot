"""Plays the Backend and AWS against the local docker compose stack (see tools/README.md). Prints no secret.

Run:  uv run python tools/simulate.py
"""
import json
import sys
import time
from pathlib import Path

import boto3
import httpx
from azure.storage.blob import BlobServiceClient

APP, WRAPPER, MINIO = "http://127.0.0.1:8000", "http://127.0.0.1:8081", "http://127.0.0.1:9000"
API_KEY = {"X-Api-Key": "dev-inbound-key"}
# The public Azurite emulator account and key (documented by Microsoft, not a secret).
AZURITE = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT"
           "50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")
SAMPLE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "samples" / "urgent_mild.json"


def fail(message):
    print(f"FAIL: {message}")
    sys.exit(1)


def wait_for(what, check, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if (found := check()) is not None:
            return found
        time.sleep(2)
    fail(f"timed out waiting for {what}")


def both(text):
    return {"en": text, "ar": f"AR-{text}"}


def main():
    blobs = BlobServiceClient.from_connection_string(AZURITE)
    for name in ("ai-input", "ai-output"):      # the app does not create them
        if not blobs.get_container_client(name).exists():
            blobs.create_container(name)
    visit = json.loads(SAMPLE.read_text(encoding="utf-8"))
    wrapper_before = len(httpx.get(f"{WRAPPER}/received").json())

    print("1. Backend posts an urgent visit")
    reply = httpx.post(f"{APP}/api/v1/patient360/visits", json=visit, headers=API_KEY, timeout=30)
    if reply.status_code != 202:
        fail(f"expected 202, got {reply.status_code}")
    print("   accepted (202)")

    print("2. Waiting for the processed file in MinIO bucket p360-urgent")
    s3 = boto3.client("s3", endpoint_url=MINIO, region_name="eu-west-1",
                      aws_access_key_id="devminio", aws_secret_access_key="devminio-secret")

    def find_key():
        listing = s3.list_objects_v2(Bucket="p360-urgent", Prefix="processed/")
        return next((o["Key"] for o in listing.get("Contents", [])), None)

    key = wait_for("the file in MinIO", find_key)
    _, lane, year, month, day, name = key.split("/")       # processed/<lane>/<yyyy>/<MM>/<dd>/<key>.json
    record_id = name.removesuffix(".json")
    print(f"   found, lane={lane}")

    print("3. Playing AWS: writing the answer to Azurite ai-output")
    text = json.dumps({"summary": both("summary"), "simple_summary": both("simple"), "critical_alerts": [],
                       "note": both("note"), "recommendations": []})
    record = {"recordId": record_id, "type": "message", "role": "assistant", "model": "simulated-model",
              "content": [{"type": "text", "text": text}], "metadata": {}}
    path = f"aws-output-merged/urgent/{lane}/{year}/{month}/{day}/{name}"
    blobs.get_blob_client("ai-output", path).upload_blob(json.dumps(record).encode(), overwrite=True)

    print("4. Playing Event Grid: telling the app the file is there")
    event = [{"eventType": "Microsoft.Storage.BlobCreated", "data": {},
              "subject": f"/blobServices/default/containers/ai-output/blobs/{path}"}]
    reply = httpx.post(f"{APP}/api/v1/patient360/blob-events", json=event, headers=API_KEY, timeout=30)
    print(f"   app answered {reply.status_code}")
    if reply.status_code >= 300:
        fail("blob-events was refused")

    print("5. Waiting for the result at the mock Backend wrapper")
    def new_results():
        results = httpx.get(f"{WRAPPER}/received").json()
        return results if len(results) > wrapper_before else None

    got = wait_for("the result at the wrapper", new_results)
    print(f"PASS: the wrapper received {len(got) - wrapper_before} result(s)")


if __name__ == "__main__":
    main()
