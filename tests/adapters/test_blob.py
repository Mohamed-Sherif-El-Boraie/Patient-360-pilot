import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from patient360.adapters import blob
from patient360.adapters.blob import AzureBlobStore
from patient360.ports import BlobUnavailable


class FakeDownload:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class FakeContainer:
    """Behaves like azure's ContainerClient for the two calls we use; ``fails`` is raised by every call."""

    def __init__(self, fails=None):
        self.blobs, self.fails, self.overwrite_flags = {}, fails, []

    def upload_blob(self, name, data, overwrite=False):
        if self.fails:
            raise self.fails
        self.overwrite_flags.append(overwrite)
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("BlobAlreadyExists")
        self.blobs[name] = data

    def download_blob(self, name):
        if self.fails:
            raise self.fails
        if name not in self.blobs:
            raise ResourceNotFoundError("BlobNotFound")
        return FakeDownload(self.blobs[name])


def test_the_emulator_stores_make_missing_containers_and_keep_existing_ones(monkeypatch):
    made = []

    class Container:
        def __init__(self, name):
            self.name = name

        def create_container(self):
            made.append(self.name)
            if exists:
                raise ResourceExistsError("exists")

    class Service:
        def get_container_client(self, name):
            return Container(name)

    exists = False
    monkeypatch.setattr(blob.BlobServiceClient, "from_connection_string", lambda *args, **kwargs: Service())
    assert isinstance(blob.build_blob_stores_from_connection_string("cs", "in"), AzureBlobStore)
    exists = True       # an existing container is kept, not an error
    assert isinstance(blob.build_blob_stores_from_connection_string("cs", "in"), AzureBlobStore)
    assert made == ["in", "in"]


def test_the_managed_identity_store_is_blob_1(monkeypatch):
    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def get_container_client(self, name):
            return name

    monkeypatch.setattr(blob, "BlobServiceClient", Service)
    assert blob.build_blob_stores("https://acct", "in", credential="c")._container == "in"


def test_a_new_file_is_created_and_read_back():
    container = FakeContainer()
    store = AzureBlobStore(container)
    assert store.put("raw/urgent/2026/10/24/k.json", b'{"a": 1}') is True
    assert store.get("raw/urgent/2026/10/24/k.json") == b'{"a": 1}'


def test_a_file_is_never_overwritten_and_that_is_not_a_failure():
    container = FakeContainer()
    store = AzureBlobStore(container)
    store.put("same.json", b"first")
    assert store.put("same.json", b"second") is False
    assert store.get("same.json") == b"first" and set(container.overwrite_flags) == {False}


def test_a_missing_file_is_none():
    assert AzureBlobStore(FakeContainer()).get("nope.json") is None


@pytest.mark.parametrize("error", [ServiceRequestError("no network"), HttpResponseError("server busy"),
                                   ClientAuthenticationError("no permission")])
def test_any_other_problem_is_blob_unavailable(error):
    store = AzureBlobStore(FakeContainer(fails=error))
    with pytest.raises(BlobUnavailable):
        store.put("a.json", b"x")
    with pytest.raises(BlobUnavailable):
        store.get("a.json")
