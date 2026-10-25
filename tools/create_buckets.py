"""Makes the four S3 buckets in the local fake S3 (the `s3` service of docker-compose.yml).

Run by the `s3-setup` service with our own image. It waits until the fake S3 answers, so the start order is free.
"""
import os
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

BUCKET_VARIABLES = ("S3_BUCKET_URGENT", "S3_BUCKET_ON_DEMAND", "S3_BUCKET_SYMPTOM_CHECKER", "S3_BUCKET_BATCH")


def main() -> int:
    client = boto3.client("s3", region_name=os.environ["AWS_REGION"], endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"])
    for _ in range(90):
        try:
            existing = {bucket["Name"] for bucket in client.list_buckets()["Buckets"]}
            break
        except (BotoCoreError, ClientError):      # also while moto is still installing
            print("waiting for the fake S3", flush=True)
            time.sleep(2)
    else:
        print("the fake S3 did not answer", file=sys.stderr)
        return 1
    region = os.environ["AWS_REGION"]
    for variable in BUCKET_VARIABLES:
        bucket = os.environ[variable]
        if bucket not in existing:
            client.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
            print("created", bucket, flush=True)
    print("buckets ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
