"""An Azure Storage queue behind the ``EventQueue`` port: where Event Grid writes its "a file was created" events.

The queue lives in the storage account we already use. The app reads it with its managed identity (role "Storage Queue
Data Contributor" on the queue and on the poison queue, as in Microsoft's Queue storage quickstart). A storage
queue has no dead-letter queue of its own, so this adapter keeps one: ``<queue>-poison``.

A message that is read is hidden from other readers for ``VISIBILITY_SECONDS``. Deleting it is "done". "Try again"
hides it for longer each time (2, 4, 8 ... minutes, at most an hour), so a long outage of the database or Blob is waited
out; after ``MAX_DELIVERIES`` reads (about two days) it goes to the poison queue. Unreadable messages go there at once.
Messages in the poison queue are moved back with ``tools/replay_poison.py``.
"""
from __future__ import annotations

import base64
from typing import Any

import structlog
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient

from patient360.ports import EventMessage

log = structlog.get_logger()

MAX_MESSAGES = 10          # per receive call
VISIBILITY_SECONDS = 300   # how long a message we read stays hidden: time to save a large batch file
MAX_DELIVERIES = 50        # reads of one message before it is given up on
MAX_HIDE_SECONDS = 3600    # the longest a message waits before its next try


def _body(content: str) -> bytes:
    """Event Grid may write the event as plain JSON or as base64 of it: both are read."""
    text = content.strip()
    if text[:1] in ("{", "["):
        return text.encode()
    try:
        return base64.b64decode(text, validate=True)
    except ValueError:
        return text.encode()           # not base64 either: the consumer finds it unreadable and dead-letters it


class StorageQueueEventQueue:
    def __init__(self, queue: Any, poison: Any, *, max_messages: int = MAX_MESSAGES,
                 visibility_seconds: int = VISIBILITY_SECONDS, max_deliveries: int = MAX_DELIVERIES) -> None:
        self._queue, self._poison = queue, poison
        self._max_messages, self._visibility, self._max_deliveries = max_messages, visibility_seconds, max_deliveries

    def receive(self) -> list[EventMessage]:
        messages = self._queue.receive_messages(messages_per_page=self._max_messages, max_messages=self._max_messages,
                                                visibility_timeout=self._visibility)
        received = []
        for message in messages:
            if message.dequeue_count > self._max_deliveries:
                try:
                    self._to_poison(message, "delivered too many times")
                except Exception as error:          # a broken poison queue must not stop the good messages of this read
                    log.error("event_message_poison_failed", error_type=type(error).__name__)
            else:
                received.append(EventMessage(body=_body(message.content), handle=message))
        return received

    def complete(self, message: EventMessage) -> None:
        self._queue.delete_message(message.handle)

    def abandon(self, message: EventMessage) -> None:
        """Try again later: hide it for 2, 4, 8 ... minutes (at most an hour), by how often it was read."""
        wait = min(60 * 2 ** message.handle.dequeue_count, MAX_HIDE_SECONDS)
        self._queue.update_message(message.handle, visibility_timeout=wait)

    def dead_letter(self, message: EventMessage, reason: str) -> None:
        self._to_poison(message.handle, reason)

    def close(self) -> None:
        self._queue.close()
        self._poison.close()

    def _to_poison(self, message: Any, reason: str) -> None:
        self._poison.send_message(message.content)         # the body is never logged
        self._queue.delete_message(message)
        log.warning("event_message_given_up", reason=reason)


def build_event_queue(queue_name: str, *, connection_string: str | None = None, account_url: str | None = None,
                      credential: Any = None) -> StorageQueueEventQueue:
    """From the Azurite connection string (local), or from the blob account URL with the managed identity (Azure:
    ``https://x.blob.core.windows.net`` -> ``https://x.queue.core.windows.net``)."""
    poison_name = f"{queue_name}-poison"
    if connection_string:
        return StorageQueueEventQueue(QueueClient.from_connection_string(connection_string, queue_name),
                                      QueueClient.from_connection_string(connection_string, poison_name))
    assert account_url, "an account URL or a connection string is needed"
    queue_url = account_url.replace(".blob.", ".queue.")
    credential = credential or DefaultAzureCredential()
    return StorageQueueEventQueue(QueueClient(queue_url, queue_name, credential=credential),
                                  QueueClient(queue_url, poison_name, credential=credential))
