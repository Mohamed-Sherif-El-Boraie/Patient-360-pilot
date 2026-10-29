"""Plays the Backend and AWS against the local docker compose stack (see tools/README.md). Prints no secret.

Run:  uv run python tools/simulate.py
"""
import json
import sys
import time
from pathlib import Path

import boto3
import httpx

APP, WRAPPER, FAKE_S3 = "http://127.0.0.1:8000", "http://127.0.0.1:8081", "http://127.0.0.1:9000"
API_KEY = {"X-Api-Key": "dev-inbound-key"}
SOURCE = "urgent"                       # the sample visit is urgent
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
    run = int(time.time()) * 10      # new ids on every run, so a repeat is not answered "duplicate"
    visit = json.loads(SAMPLE.read_text(encoding="utf-8")) | {"visit_id": run + 1, "patient_id": run + 1}
    wrapper_before = len(httpx.get(f"{WRAPPER}/received").json())
    s3 = boto3.client("s3", endpoint_url=FAKE_S3, region_name="eu-west-1",
                      aws_access_key_id="dev-fake-key", aws_secret_access_key="dev-fake-secret")

    def urgent_keys():
        listing = s3.list_objects_v2(Bucket="p360-urgent", Prefix="processed/")
        return {o["Key"] for o in listing.get("Contents", [])}

    old_keys = urgent_keys()      # files of earlier runs stay in the bucket

    print("1. Backend posts an urgent visit")
    reply = httpx.post(f"{APP}/api/v1/patient360/visits", json=visit, headers=API_KEY, timeout=30)
    if reply.status_code != 202:
        fail(f"expected 202, got {reply.status_code}: {reply.text[:200]}")
    print("   accepted (202)")

    print("2. Waiting for the processed file in the fake S3 bucket p360-urgent")

    def find_key():
        return next(iter(urgent_keys() - old_keys), None)

    key = wait_for("the file in the fake S3", find_key)
    # processed/<lane>/<yyyy>/<MM>/<dd>/00-input/<key>.json
    _, lane, year, month, day, _input, name = key.split("/")
    record_id = name.removesuffix(".json")
    print(f"   found, lane={lane}")

    print("3. Playing AWS and the Function app: posting the answer file to the app")
    text = json.dumps({"summary": both("summary"), "simple_summary": both("simple"), "critical_alerts": [],
                       "note": both("note"), "recommendations": []})
    record = {"recordId": record_id, "type": "message", "role": "assistant", "model": "simulated-model",
              "content": [{"type": "text", "text": text}], "metadata": {}}
    # <lane>/<yyyy>/<MM>/<dd>/<idempotency_key>/05-output-merged.json (the key is the recordId)
    answer_path = f"{lane}/{year}/{month}/{day}/{record_id}/05-output-merged.json"
    sent = httpx.post(f"{APP}/api/v1/patient360/ai-results", content=json.dumps(record).encode(), timeout=30,
                      headers=API_KEY | {"X-Source": SOURCE, "X-Blob-Path": answer_path})
    if sent.status_code != 200 or sent.json().get("outcome") != "saved":
        fail(f"expected 200 saved, got {sent.status_code}: {sent.text[:200]}")
    print(f"   posted: {answer_path} -> saved")

    print("4. Waiting for the result at the mock Backend wrapper")
    def new_results():
        results = httpx.get(f"{WRAPPER}/received").json()
        return results if len(results) > wrapper_before else None

    got = wait_for("the result at the wrapper", new_results)
    print(f"PASS: the wrapper received {len(got) - wrapper_before} result(s)")


if __name__ == "__main__":
    main()
