"""Checks the S3 layout of all four buckets against the local docker compose stack. Prints no secret.

Run (the stack must be up):  uv run python tools/check_s3_layout.py
It plays the Backend, and AWS once (the symptom check needs the visit's result first: the answer is
posted to the app's /api/v1/patient360/ai-results, as the Function app does). It uses new ids on every run.
"""
import json
import re
import sys
import time
from pathlib import Path

import boto3
import httpx

APP, FAKE_S3 = "http://127.0.0.1:8000", "http://127.0.0.1:9000"
API_KEY = {"X-Api-Key": "dev-inbound-key"}
VISITS, SYMPTOMS, RESULTS = (f"{APP}/api/v1/patient360/{name}" for name in ("visits", "visits-symptoms", "ai-results"))
SAMPLES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "samples"
DAY = r"\d{4}/\d{2}/\d{2}"
RUN = int(time.time()) * 10                # new ids on every run, so nothing is a duplicate

# bucket -> the key every file in it must match (the bucket is the source, so the key has no source name)
EXPECTED = {
    "p360-urgent": rf"processed/normal/{DAY}/00-input/[0-9a-f]{{32}}\.json",
    "p360-on-demand": rf"processed/critical/{DAY}/00-input/[0-9a-f]{{32}}\.json",
    "p360-symptom-checker": rf"processed/critical/{DAY}/00-input/[0-9a-f]{{32}}\.json",
}
BATCH = {lane: rf"processed/{lane}/{DAY}/({lane}_\d+)/00-input/\1\.jsonl" for lane in ("normal", "critical")}
BATCH_SIZE = 100            # the compose stack's BATCH_SIZE: one batch is sent when this many visits of a lane wait

s3 = boto3.client("s3", endpoint_url=FAKE_S3, region_name="eu-west-1",
                  aws_access_key_id="dev-fake-key", aws_secret_access_key="dev-fake-secret")
failed = []
before = {}


def sample(name, **changes):
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8")) | changes


def post(url, body, want=202):
    reply = httpx.post(url, json=body, headers=API_KEY, timeout=30)
    if reply.status_code != want:
        sys.exit(f"FAIL: {url} answered {reply.status_code}, expected {want}: {reply.text[:200]}")


def keys_in(bucket):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="processed/").get("Contents", [])]


def wait_for_new_keys(bucket, count=1, seconds=90):
    """The keys that were not in the bucket when this run started: files of earlier runs stay in the buckets."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if len(found := [k for k in keys_in(bucket) if k not in before[bucket]]) >= count:
            return found
        time.sleep(2)
    return [k for k in keys_in(bucket) if k not in before[bucket]]


def check(bucket, pattern, found):
    ok = bool(found) and all(re.fullmatch(pattern, key) for key in found)
    print(f"{'PASS' if ok else 'FAIL'}  {bucket}: {found[-1] if found else 'nothing arrived'}")
    if not ok:
        failed.append(bucket)


def main():
    before.update({bucket: set(keys_in(bucket)) for bucket in (*EXPECTED, "p360-batch")})

    post(VISITS, sample("urgent_mild", visit_id=RUN + 1, patient_id=RUN + 1))
    visit = sample("on_demand_critical", source=4, visit_id=RUN + 2, patient_id=RUN + 2)
    post(VISITS, visit)
    for bucket in ("p360-urgent", "p360-on-demand"):
        check(bucket, EXPECTED[bucket], wait_for_new_keys(bucket))

    on_demand_key = wait_for_new_keys("p360-on-demand")[0]
    answer = {"summary": {"en": "s", "ar": "AR-s"}, "simple_summary": {"en": "s", "ar": "AR-s"}, "critical_alerts": [],
              "note": {"en": "n", "ar": "AR-n"}, "recommendations": []}
    # processed/<lane>/<y>/<M>/<d>/00-input/<key>.json
    _, lane, year, month, day, _input, file_name = on_demand_key.split("/")
    record = {"recordId": file_name.removesuffix(".json"), "type": "message", "role": "assistant",
              "model": "simulated-model", "content": [{"type": "text", "text": json.dumps(answer)}], "metadata": {}}
    path = f"{lane}/{year}/{month}/{day}/{record['recordId']}/05-output-merged.json"
    reply = httpx.post(RESULTS, content=json.dumps(record).encode(), timeout=30,
                       headers=API_KEY | {"X-Source": "on-demand", "X-Blob-Path": path})
    if reply.status_code != 200:
        sys.exit(f"FAIL: {RESULTS} answered {reply.status_code}: {reply.text[:200]}")
    post(SYMPTOMS, sample("symptom_check", visit_id=visit["visit_id"], patient_id=visit["patient_id"]))
    check("p360-symptom-checker", EXPECTED["p360-symptom-checker"], wait_for_new_keys("p360-symptom-checker"))

    for lane, name in (("normal", "batch_normal"), ("critical", "batch_critical")):
        print(f"posting {BATCH_SIZE} {lane} batch visits ...")
        for number in range(BATCH_SIZE):
            post(VISITS, sample(name, visit_id=RUN + 1000 + (0 if lane == "normal" else 500) + number,
                                patient_id=RUN + 1000 + (0 if lane == "normal" else 500) + number))
    found = wait_for_new_keys("p360-batch", count=2, seconds=120)
    for lane, pattern in BATCH.items():
        check(f"p360-batch ({lane})", pattern, [k for k in found if k.startswith(f"processed/{lane}/")])

    print("ALL PASS" if not failed else f"FAILED: {', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
