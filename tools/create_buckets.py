"""Makes the four S3 buckets in the local MinIO server.

Run by the `minio-setup` service of docker-compose.yml with our own image: the MinIO image has no client tool, and the
client image is not downloadable without a login. It waits until MinIO answers, so the start order does not matter.
"""
import os
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

BUCKET_VARIABLES = ("S3_BUCKET_URGENT", "S3_BUCKET_ON_DEMAND", "S3_BUCKET_SYMPTOM_CHECKER", "S3_BUCKET_BATCH")


def main() -> int:
    client = boto3.client("s3", region_name=os.environ["AWS_REGION"], endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"])
    for _ in range(40):
        try:
            existing = {bucket["Name"] for bucket in client.list_buckets()["Buckets"]}
            break
        except (BotoCoreError, ClientError):
            print("waiting for minio", flush=True)
            time.sleep(2)
    else:
        print("minio did not answer", file=sys.stderr)
        return 1
    for variable in BUCKET_VARIABLES:
        bucket = os.environ[variable]
        if bucket not in existing:
            client.create_bucket(Bucket=bucket)
            print("created", bucket, flush=True)
    print("buckets ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
