"""Azure Blob Storage behind the ``BlobStore`` port: Blob 1 (input, raw, processed, rejected)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

from patient360.ports import BlobStore, BlobUnavailable

log = structlog.get_logger()


class AzureBlobStore:
    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    def put(self, path: str, data: bytes) -> bool:
        """Create the file only if it is absent: ``False`` means "already stored", which is not a failure.
        Any other problem (no network, a 5xx, no permission) is ``BlobUnavailable``: the caller uses PostgreSQL."""
        try:
            self._container.upload_blob(path, data, overwrite=False)
        except ResourceExistsError:
            return False
        except AzureError as error:
            log.warning("blob_unavailable", error_type=type(error).__name__)
            raise BlobUnavailable from error
        return True

    def get(self, path: str) -> bytes | None:
        try:
            return self._container.download_blob(path).readall()
        except ResourceNotFoundError:
            return None
        except AzureError as error:
            log.warning("blob_unavailable", error_type=type(error).__name__)
            raise BlobUnavailable from error

    def list(self, prefix: str) -> list[str]:
        try:
            return [blob.name for blob in self._container.list_blobs(name_starts_with=prefix)]
        except AzureError as error:
            log.warning("blob_unavailable", error_type=type(error).__name__)
            raise BlobUnavailable from error

    def delete(self, path: str) -> None:
        try:
            self._container.delete_blob(path)
        except ResourceNotFoundError:
            return
        except AzureError as error:
            log.warning("blob_unavailable", error_type=type(error).__name__)
            raise BlobUnavailable from error

    def for_source(self, source: str) -> BlobStore:
        """One container only (the local emulator): the source becomes a folder, so two sources never share a name."""
        return _InFolder(self, source)


class _InFolder:
    def __init__(self, store: BlobStore, folder: str) -> None:
        self._store, self._prefix = store, folder + "/"

    def put(self, path: str, data: bytes) -> bool:
        return self._store.put(self._prefix + path, data)

    def get(self, path: str) -> bytes | None:
        return self._store.get(self._prefix + path)

    def list(self, prefix: str) -> list[str]:
        return [name[len(self._prefix):] for name in self._store.list(self._prefix + prefix)]

    def delete(self, path: str) -> None:
        self._store.delete(self._prefix + path)

    def for_source(self, source: str) -> BlobStore:
        return _InFolder(self._store, self._prefix + source)


class RoutedBlobStore:
    """``p360-raw`` for everything that is ours alone (raw, rejected, pending), and one container per source for the
    files that sit next to AWS's (``for_source``). A source whose container is not configured is ``BlobUnavailable``:
    the caller then uses PostgreSQL, as it does when Blob is down."""

    def __init__(self, raw: BlobStore, by_source: Mapping[str, BlobStore]) -> None:
        self._raw, self._by_source = raw, by_source

    def put(self, path: str, data: bytes) -> bool:
        return self._raw.put(path, data)

    def get(self, path: str) -> bytes | None:
        return self._raw.get(path)

    def list(self, prefix: str) -> list[str]:
        return self._raw.list(prefix)

    def delete(self, path: str) -> None:
        self._raw.delete(path)

    def for_source(self, source: str) -> BlobStore:
        if source not in self._by_source:
            log.warning("source_container_not_configured", source=source)
            raise BlobUnavailable
        return self._by_source[source]


def build_blob_stores(account_url: str, input_container: str, credential: Any = None) -> AzureBlobStore:
    """Blob 1. Signs in with the App Service's managed identity (or the developer's ``az login``) unless a credential
    is given. Short timeouts and two retries, so a Blob outage is noticed in seconds, not minutes."""
    service = BlobServiceClient(account_url, credential=credential or DefaultAzureCredential(),
                                connection_timeout=5, read_timeout=30, retry_total=2)
    return AzureBlobStore(service.get_container_client(input_container))


def build_output_store(container_url: str) -> AzureBlobStore:
    """A container opened with its SAS URL (create, write and read: the token is in the URL, so it is a secret)."""
    return AzureBlobStore(ContainerClient.from_container_url(container_url, connection_timeout=5, read_timeout=30,
                                                             retry_total=2))


def build_blob_stores_from_connection_string(connection_string: str, input_container: str) -> AzureBlobStore:
    """Same as ``build_blob_stores`` for the local Azurite emulator, which starts empty: a missing container is made."""
    service = BlobServiceClient.from_connection_string(connection_string, connection_timeout=5, read_timeout=30,
                                                       retry_total=2)
    container = service.get_container_client(input_container)
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    return AzureBlobStore(container)
