"""Checks the S3 layout of all four buckets against the local docker compose stack. Prints no secret.

Run (the stack must be up):  uv run python tools/check_s3_layout.py
It plays the Backend, and AWS once (the symptom check needs the visit's result first). It uses new ids on every run.
"""
import json
import re
import sys
import time
from pathlib import Path

import boto3
import httpx
from azure.storage.blob import BlobServiceClient

APP, FAKE_S3 = "http://127.0.0.1:8000", "http://127.0.0.1:9000"
API_KEY = {"X-Api-Key": "dev-inbound-key"}
VISITS, SYMPTOMS, EVENTS = (f"{APP}/api/v1/patient360/{name}" for name in ("visits", "visits-symptoms", "blob-events"))
AZURITE = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT"
           "50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")
SAMPLES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "samples"
DAY = r"\d{4}/\d{2}/\d{2}"
RUN = int(time.time()) * 10                # new ids on every run, so nothing is a duplicate

# bucket -> the key every file in it must match (the bucket is the source, so the key has no source name)
EXPECTED = {
    "p360-urgent": rf"processed/normal/{DAY}/[0-9a-f]{{32}}\.json",
    "p360-on-demand": rf"processed/critical/{DAY}/[0-9a-f]{{32}}\.json",
    "p360-symptom-checker": rf"processed/critical/{DAY}/[0-9a-f]{{32}}\.json",
}
BATCH = {lane: rf"processed/{lane}-jsonls/{DAY}/[^/]+\.jsonl" for lane in ("normal", "critical")}
BATCH_SIZE = 100            # the compose stack's BATCH_SIZE: one batch is sent when this many visits of a lane wait

s3 = boto3.client("s3", endpoint_url=FAKE_S3, region_name="eu-west-1",
                  aws_access_key_id="dev-fake-key", aws_secret_access_key="dev-fake-secret")
failed = []


def sample(name, **changes):
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8")) | changes


def post(url, body, want=202):
    reply = httpx.post(url, json=body, headers=API_KEY, timeout=30)
    if reply.status_code != want:
        sys.exit(f"FAIL: {url} answered {reply.status_code}, expected {want}: {reply.text[:200]}")


def keys_in(bucket):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="processed/").get("Contents", [])]


def wait_for_keys(bucket, count=1, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if len(found := keys_in(bucket)) >= count:
            return found
        time.sleep(2)
    return keys_in(bucket)


def check(bucket, pattern, found):
    ok = bool(found) and all(re.fullmatch(pattern, key) for key in found)
    print(f"{'PASS' if ok else 'FAIL'}  {bucket}: {found[-1] if found else 'nothing arrived'}")
    if not ok:
        failed.append(bucket)


def main():
    blobs = BlobServiceClient.from_connection_string(AZURITE)
    for name in ("ai-input", "ai-output"):
        if not blobs.get_container_client(name).exists():
            blobs.create_container(name)
    before = {bucket: set(keys_in(bucket)) for bucket in (*EXPECTED, "p360-batch")}

    post(VISITS, sample("urgent_mild", visit_id=RUN + 1, patient_id=RUN + 1))
    visit = sample("on_demand_critical", source=4, visit_id=RUN + 2, patient_id=RUN + 2)
    post(VISITS, visit)
    for bucket in ("p360-urgent", "p360-on-demand"):
        check(bucket, EXPECTED[bucket], [k for k in wait_for_keys(bucket) if k not in before[bucket]])

    on_demand_key = next(k for k in keys_in("p360-on-demand") if k not in before["p360-on-demand"])
    answer = {"summary": {"en": "s", "ar": "AR-s"}, "simple_summary": {"en": "s", "ar": "AR-s"}, "critical_alerts": [],
              "note": {"en": "n", "ar": "AR-n"}, "recommendations": []}
    record = {"recordId": on_demand_key.rsplit("/", 1)[1].removesuffix(".json"), "type": "message", "role": "assistant",
              "model": "simulated-model", "content": [{"type": "text", "text": json.dumps(answer)}], "metadata": {}}
    path = "aws-output-merged/on-demand/" + on_demand_key.removeprefix("processed/")
    blobs.get_blob_client("ai-output", path).upload_blob(json.dumps(record).encode(), overwrite=True)
    post(EVENTS, [{"eventType": "Microsoft.Storage.BlobCreated", "data": {},
                   "subject": f"/blobServices/default/containers/ai-output/blobs/{path}"}], want=200)
    post(SYMPTOMS, sample("symptom_check", visit_id=visit["visit_id"], patient_id=visit["patient_id"]))
    check("p360-symptom-checker", EXPECTED["p360-symptom-checker"], wait_for_keys("p360-symptom-checker"))

    for lane, name in (("normal", "batch_normal"), ("critical", "batch_critical")):
        print(f"posting {BATCH_SIZE} {lane} batch visits ...")
        for number in range(BATCH_SIZE):
            post(VISITS, sample(name, visit_id=RUN + 1000 + (0 if lane == "normal" else 500) + number,
                                patient_id=RUN + 1000 + (0 if lane == "normal" else 500) + number))
    found = [k for k in wait_for_keys("p360-batch", count=2, seconds=120) if k not in before["p360-batch"]]
    for lane, pattern in BATCH.items():
        check(f"p360-batch ({lane})", pattern, [k for k in found if f"/{lane}-jsonls/" in k])

    print("ALL PASS" if not failed else f"FAILED: {', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
