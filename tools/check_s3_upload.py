"""Tests the real connection to the AWS S3 buckets: can we write a file into each of them? Prints no secret.

Needs only boto3 (``pip install boto3``), so it can run on any machine inside the network, for example the Azure VM:

    set AWS_REGION=eu-central-1
    set AWS_ENDPOINT_URL_S3=https://bucket.vpce-xxxx.s3.eu-central-1.vpce.amazonaws.com
    set AWS_ACCESS_KEY_ID=...                 (from the AWS team)
    set AWS_SECRET_ACCESS_KEY=...
    python tools/check_s3_upload.py

By default it writes one tiny text file into each of the four buckets, under ``connectivity-test/``, a name that is
never ``01-input.json``, so it never starts the AWS automation. The IAM policy lets urgent, on-demand and
symptom-checker accept only ``*/01-input.json``, so for those three buckets this test is ANSWERED WITH AccessDenied
by design, and only p360-batch (which allows any key) passes. That still proves the network and the keys work.
To prove the permission for a real file, use the second form.

To send ONE real processed file, built exactly as the app does it, for the test the AWS team agrees to (this DOES
start their automation). ``tools/sample_processed_urgent.json`` is a ready file with made-up patient data:

    python tools/check_s3_upload.py --source urgent --lane normal --file tools/sample_processed_urgent.json

To send the whole check at once, 4 correct and 4 wrong files (one pair per bucket; this DOES start their automation):

    python tools/check_s3_upload.py --suite

The key is ``<lane>/<today, UTC>/<id>/01-input.json``; the id is ``p360test-<time>`` unless you give ``--id``.
(``--bucket`` and ``--key`` write to an exact place instead.)
"""
import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

SOURCE_BUCKETS = {"urgent": "S3_BUCKET_URGENT", "on-demand": "S3_BUCKET_ON_DEMAND",
                  "symptom-checker": "S3_BUCKET_SYMPTOM_CHECKER"}      # a batch has its own key: use --bucket and --key
DEFAULT_BUCKETS = {
    "S3_BUCKET_URGENT": "p360-urgent", "S3_BUCKET_ON_DEMAND": "p360-on-demand",
    "S3_BUCKET_SYMPTOM_CHECKER": "p360-symptom-checker", "S3_BUCKET_BATCH": "p360-batch",
}


def client():
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or None
    config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 1},
                    s3={"addressing_style": "path"})            # the endpoint name is not a bucket name
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION"), endpoint_url=endpoint, config=config)


def put(s3, bucket: str, key: str, body: bytes) -> bool:
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body)
    except ClientError as error:
        print(f"FAIL  {bucket}/{key}: AWS answered {error.response['Error']['Code']} "
              f"(HTTP {error.response['ResponseMetadata']['HTTPStatusCode']})")
    except BotoCoreError as error:                                # no connection, DNS, timeout, no credentials
        print(f"FAIL  {bucket}/{key}: {type(error).__name__} (no connection, or no credentials)")
    else:
        print(f"PASS  {bucket}/{key}")
        return True
    return False


def suite(s3, now: datetime) -> int:
    """8 uploads: in each of the 4 buckets one correct file and one with a mistake (``messages`` empty), so AWS
    must answer 4 with 05-output-merged and 4 with a failed file. The batch ones hold 3 lines each. The lane
    alternates, so normal and critical each get both kinds in different buckets."""
    sample = json.loads((Path(__file__).parent / "sample_processed_urgent.json").read_text(encoding="utf-8"))
    sample.pop("recordId")        # a single-visit file is flat (recordId beside the request); a batch line wraps it
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y/%m/%d")
    bad = sample | {"messages": []}
    passed = []
    lanes = ("normal", "critical")
    for i, env_name in enumerate([*SOURCE_BUCKETS.values(), "S3_BUCKET_BATCH"]):
        bucket = os.environ.get(env_name, DEFAULT_BUCKETS[env_name])
        for j, (tag, model_input) in enumerate((("ok", sample), ("bad", bad))):
            lane = lanes[(i + j) % 2]      # both lanes get correct and wrong files
            name = f"p360t-{stamp}-{tag}"
            if env_name == "S3_BUCKET_BATCH":
                plain = {k: v for k, v in model_input.items() if k != "amazon-bedrock-guardrailConfig"} | {
                    "system": [{k: v for k, v in b.items() if k != "cache_control"} for b in model_input["system"]]}
                lines = [json.dumps({"recordId": f"{name}-{n}", "modelInput": plain}) for n in range(3)]
                passed.append(put(s3, bucket, f"{lane}/{day}/{name}/01-input.jsonl", "\n".join(lines).encode()))
            else:
                body = json.dumps({"recordId": name, **model_input}).encode()
                passed.append(put(s3, bucket, f"{lane}/{day}/{name}/01-input.json", body))
    print(f"\nid stamp: {stamp}  (ok = expect 05-output-merged, bad = expect a failed file)")
    print("ALL UPLOADED" if all(passed) else "SOME FAILED")
    return 0 if all(passed) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", action="store_true", help="4 correct + 4 wrong uploads, one pair per bucket")
    parser.add_argument("--bucket")
    parser.add_argument("--key")
    parser.add_argument("--file")
    parser.add_argument("--source", choices=sorted(SOURCE_BUCKETS))
    parser.add_argument("--lane", choices=("normal", "critical"))
    parser.add_argument("--id", help="the visit folder name (default: p360test-<time>)")
    args = parser.parse_args()
    print("endpoint:", os.environ.get("AWS_ENDPOINT_URL_S3") or "(the normal AWS address)",
          "| region:", os.environ.get("AWS_REGION") or "(not set)")
    s3 = client()
    now = datetime.now(UTC)
    if args.suite:
        return suite(s3, now)
    if args.source or args.lane:
        if not (args.source and args.lane and args.file):
            print("Give --source, --lane and --file together.", file=sys.stderr)
            return 2
        bucket = os.environ.get(SOURCE_BUCKETS[args.source], DEFAULT_BUCKETS[SOURCE_BUCKETS[args.source]])
        key = f"{args.lane}/{now:%Y/%m/%d}/{args.id or now.strftime('p360test-%Y%m%dT%H%M%SZ')}/01-input.json"
        return 0 if put(s3, bucket, key, Path(args.file).read_bytes()) else 1
    if args.bucket or args.key or args.file:
        if not (args.bucket and args.key and args.file):
            print("Give --bucket, --key and --file together.", file=sys.stderr)
            return 2
        return 0 if put(s3, args.bucket, args.key, Path(args.file).read_bytes()) else 1
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    results = [put(s3, os.environ.get(name, default), f"connectivity-test/{stamp}.txt",
                   b"patient360 connectivity test, safe to delete\n")
               for name, default in DEFAULT_BUCKETS.items()]
    print("ALL PASS" if all(results) else "SOME FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
