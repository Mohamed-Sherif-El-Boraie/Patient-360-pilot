"""Reading Event Grid's "a file was created" events from the queue and handing each file to the ``OutputReceiver``.

Event Grid cannot push to a private app, so it writes the events into a queue and we read the queue ourselves. A
message is removed only after its files are saved. If that fails it goes back to the queue and is delivered again;
saving the same AWS answer twice is safe.
"""
from __future__ import annotations

import json

import structlog

from patient360.domain.common.blob_events import blob_paths_from_events
from patient360.flow.results import OutputReceiver
from patient360.ports import EventMessage, EventQueue

log = structlog.get_logger()


class BlobEventConsumer:
    def __init__(self, queue: EventQueue, receiver: OutputReceiver) -> None:
        self._queue, self._receiver = queue, receiver

    def run_once(self) -> int:
        """Handle the messages that are waiting (the queue waits a few seconds for some). Returns how many."""
        messages = self._queue.receive()
        for message in messages:
            self._handle(message)
        return len(messages)

    def close(self) -> None:
        self._queue.close()

    def _handle(self, message: EventMessage) -> None:
        try:
            paths = blob_paths_from_events(json.loads(message.body))
        except (ValueError, KeyError, IndexError, TypeError):
            log.warning("blob_event_unreadable")           # never log the body
            self._settle(self._queue.dead_letter, message, "not an Event Grid blob event")
            return
        try:
            for path in paths:
                self._receiver.receive(path)
        except Exception as error:                         # the database or Blob is down: try the message again later
            log.error("blob_event_failed", error_type=type(error).__name__)
            self._settle(self._queue.abandon, message)
            return
        self._settle(self._queue.complete, message)

    @staticmethod
    def _settle(action, message: EventMessage, *args: str) -> None:
        """Complete, abandon or dead-letter one message. If that fails, the message comes back by itself later, and the
        other messages of the same read are still handled."""
        try:
            action(message, *args)
        except Exception as error:
            log.error("blob_event_settle_failed", error_type=type(error).__name__)
