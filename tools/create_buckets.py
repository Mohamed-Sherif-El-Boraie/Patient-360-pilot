"""Makes the four S3 buckets in the local fake S3 and Blob 1's container in Azurite.

Run by the `s3-setup` service with our own image. It waits until the fake S3 answers, so the start order is free.
"""
import os
import sys
import time

import boto3
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.storage.blob import BlobServiceClient
from botocore.exceptions import BotoCoreError, ClientError

BUCKET_VARIABLES = ("S3_BUCKET_URGENT", "S3_BUCKET_ON_DEMAND", "S3_BUCKET_SYMPTOM_CHECKER", "S3_BUCKET_BATCH")


def make_container() -> None:
    """Blob 1's container (BLOB_INPUT_CONTAINER). In Azure it is made once by hand (docs/wiring-and-deployment.md)."""
    connection, name = os.environ.get("AZURE_STORAGE_CONNECTION_STRING"), os.environ.get("BLOB_INPUT_CONTAINER")
    if not (connection and name):
        return
    for _ in range(30):
        try:
            BlobServiceClient.from_connection_string(connection).create_container(name)
            print("created container", name, flush=True)
        except ResourceExistsError:
            pass
        except AzureError:                  # Azurite is not ready yet
            print("waiting for Azurite", flush=True)
            time.sleep(2)
            continue
        break


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
    make_container()
    return 0


if __name__ == "__main__":
    sys.exit(main())
