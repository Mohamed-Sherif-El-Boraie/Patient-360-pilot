import pytest

from patient360.domain.common.blob_events import blob_paths_from_events

PATH = "aws-output-merged/urgent/normal/2026/10/25/abc.json"


def event(path=PATH, kind="Microsoft.Storage.BlobCreated", container="ai-output"):
    return {"eventType": kind, "subject": f"/blobServices/default/containers/{container}/blobs/{path}",
            "data": {"api": "PutBlob"}}


def test_the_path_is_what_follows_blobs_in_the_subject():
    assert blob_paths_from_events([event()]) == [PATH]


def test_one_event_or_a_list_of_events_both_work():
    assert blob_paths_from_events(event()) == [PATH]
    assert blob_paths_from_events([event(), event("aws-output-merged/batch/normal-jsonls/2026/10/25/b.jsonl")]) == [
        PATH, "aws-output-merged/batch/normal-jsonls/2026/10/25/b.jsonl"]


def test_other_event_types_are_ignored():
    assert blob_paths_from_events([event(kind="Microsoft.Storage.BlobDeleted")]) == []


@pytest.mark.parametrize("bad", [[{"eventType": "Microsoft.Storage.BlobCreated"}], ["text"], 5,
                                 [{"eventType": "Microsoft.Storage.BlobCreated", "subject": "no-blobs-here"}]])
def test_a_body_that_is_not_an_event_raises(bad):
    with pytest.raises((KeyError, IndexError, TypeError)):
        blob_paths_from_events(bad)
