"""Azure Blob Storage behind the ``BlobStore`` port: one store per container (input = Blob 1, output = Blob 2)."""
from __future__ import annotations

from typing import Any

import structlog
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

from patient360.ports import BlobUnavailable

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


def build_blob_stores(
    account_url: str, input_container: str, output_container: str, credential: Any = None,
) -> tuple[AzureBlobStore, AzureBlobStore]:
    """``(Blob 1, Blob 2)``. Signs in with the App Service's managed identity (or the developer's ``az login``) unless a
    credential is given. Short timeouts and two retries, so a Blob outage is noticed in seconds, not minutes."""
    service = BlobServiceClient(account_url, credential=credential or DefaultAzureCredential(),
                                connection_timeout=5, read_timeout=30, retry_total=2)
    return (AzureBlobStore(service.get_container_client(input_container)),
            AzureBlobStore(service.get_container_client(output_container)))
