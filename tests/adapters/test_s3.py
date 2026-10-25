import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from patient360.adapters.s3 import S3Uploader, build_s3_client
from patient360.ports import S3Unavailable

BUCKETS = {"urgent": "b-urgent", "on-demand": "b-on-demand", "symptom-checker": "b-symptom", "batch": "b-batch"}


@pytest.fixture
def s3():
    """A real boto3 client with made-up credentials; Stubber answers instead of the network."""
    client = boto3.client("s3", region_name="eu-west-1", aws_access_key_id="x", aws_secret_access_key="y")
    with Stubber(client) as stubber:
        yield client, stubber


def test_each_source_goes_to_its_own_bucket_as_json(s3):
    client, stubber = s3
    stubber.add_response("put_object", {}, {"Bucket": "b-urgent", "Key": "processed/normal/2026/10/24/k.json",
                                            "Body": b"{}", "ContentType": "application/json"})
    S3Uploader(client, BUCKETS).put("urgent", "processed/normal/2026/10/24/k.json", b"{}")
    stubber.assert_no_pending_responses()


def test_a_batch_file_is_sent_as_json_lines_to_the_batch_bucket(s3):
    client, stubber = s3
    stubber.add_response("put_object", {}, {"Bucket": "b-batch", "Key": "processed/normal-jsonls/2026/10/25/b.jsonl",
                                            "Body": b"{}\n{}", "ContentType": "application/x-ndjson"})
    S3Uploader(client, BUCKETS).put("batch", "processed/normal-jsonls/2026/10/25/b.jsonl", b"{}\n{}")
    stubber.assert_no_pending_responses()


def test_an_s3_error_is_s3_unavailable(s3):
    client, stubber = s3
    stubber.add_client_error("put_object", service_error_code="InternalError", http_status_code=500)
    with pytest.raises(S3Unavailable):
        S3Uploader(client, BUCKETS).put("urgent", "k.json", b"{}")


def test_no_network_is_s3_unavailable():
    client = boto3.client("s3", region_name="eu-west-1", aws_access_key_id="x", aws_secret_access_key="y")

    def no_network(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://s3.example")

    client.meta.events.register("before-call.s3.PutObject", no_network)
    with pytest.raises(S3Unavailable):
        S3Uploader(client, BUCKETS).put("urgent", "k.json", b"{}")


def test_a_missing_bucket_stops_the_app_at_start_up():
    with pytest.raises(ValueError, match="batch"):
        S3Uploader(object(), {name: bucket for name, bucket in BUCKETS.items() if name != "batch"})


def test_a_local_endpoint_can_be_given():
    client = build_s3_client("eu-west-1", "http://127.0.0.1:9000")
    assert client.meta.endpoint_url == "http://127.0.0.1:9000"


def test_the_real_client_is_built_with_short_timeouts():
    client = build_s3_client("eu-west-1")
    assert client.meta.region_name == "eu-west-1" and client.meta.config.connect_timeout == 5
