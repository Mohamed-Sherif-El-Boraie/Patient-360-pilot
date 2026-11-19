import pytest

from patient360.adapters.blob import RoutedBlobStore
from patient360.ports import BlobUnavailable
from tests.fakes import FakeBlobs


def test_ours_alone_goes_to_p360_raw_and_the_processed_file_to_the_container_of_its_source():
    raw, urgent = FakeBlobs(), FakeBlobs()
    blobs = RoutedBlobStore(raw, {"urgent": urgent})
    blobs.put("raw-jsons/urgent/2026/10/24/k.json", b"raw")
    blobs.for_source("urgent").put("normal/2026/10/24/k/02-processed.json", b"processed")
    assert list(raw.files) == ["raw-jsons/urgent/2026/10/24/k.json"]
    assert list(urgent.files) == ["normal/2026/10/24/k/02-processed.json"]


def test_a_source_without_a_container_is_blob_unavailable_so_the_caller_uses_postgresql():
    blobs = RoutedBlobStore(FakeBlobs(), {"urgent": FakeBlobs()})
    with pytest.raises(BlobUnavailable):
        blobs.for_source("batch")
