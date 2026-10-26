"""What an Azure Event Grid "a file was created" event tells us. Pure.

The same event arrives two ways: as a list posted to ``/blob-events`` (local testing), or as the text of a storage
queue message (production: one event per message, sometimes a list).
"""
from __future__ import annotations

from typing import Any

BLOB_CREATED = "Microsoft.Storage.BlobCreated"


def blob_paths_from_events(events: Any) -> list[str]:
    """The path inside the container of every "blob created" event; other event types are ignored.

    ``events`` is one event or a list of them. A body that is not an Event Grid event raises ``KeyError``,
    ``IndexError`` or ``TypeError``: the caller decides what to do with it.
    """
    if isinstance(events, dict):
        events = [events]
    return [event["subject"].split("/blobs/", 1)[1] for event in events if event["eventType"] == BLOB_CREATED]
