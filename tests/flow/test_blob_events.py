import json

from structlog.testing import capture_logs

from patient360.flow.blob_events import BlobEventConsumer
from patient360.ports import EventMessage

PATH = "aws-output-merged/urgent/normal/2026/10/25/abc.json"


def created(path=PATH):
    return {"eventType": "Microsoft.Storage.BlobCreated", "subject": f"/blobServices/default/containers/x/blobs/{path}"}


class FakeQueue:
    def __init__(self, *bodies):
        self.waiting = [EventMessage(body=body if isinstance(body, bytes) else json.dumps(body).encode(), handle=i)
                        for i, body in enumerate(bodies)]
        self.settled, self.closed, self.dead_letter_fails, self.complete_fails = [], False, False, None

    def receive(self):
        messages, self.waiting = self.waiting, []
        return messages

    def complete(self, message):
        if self.complete_fails == message.handle:
            raise RuntimeError("lost the message lock")
        self.settled.append(("complete", message.handle))

    def abandon(self, message):
        self.settled.append(("abandon", message.handle))

    def dead_letter(self, message, reason):
        if self.dead_letter_fails:
            raise RuntimeError("poison queue missing")
        self.settled.append(("dead_letter", message.handle, reason))

    def close(self):
        self.closed = True


class SpyReceiver:
    def __init__(self, error=None):
        self.paths, self.error = [], error

    def receive(self, path):
        self.paths.append(path)
        if self.error:
            raise self.error


def test_a_file_event_is_saved_and_the_message_completed():
    queue, receiver = FakeQueue(created()), SpyReceiver()
    assert BlobEventConsumer(queue, receiver).run_once() == 1
    assert receiver.paths == [PATH] and queue.settled == [("complete", 0)]


def test_one_message_may_hold_several_events_and_other_event_types_are_skipped():
    other = {"eventType": "Microsoft.Storage.BlobDeleted", "subject": "/blobs/x"}
    queue, receiver = FakeQueue([created("a.json"), other, created("b.json")], other), SpyReceiver()
    BlobEventConsumer(queue, receiver).run_once()
    assert receiver.paths == ["a.json", "b.json"] and queue.settled == [("complete", 0), ("complete", 1)]


def test_an_empty_queue_does_nothing():
    queue, receiver = FakeQueue(), SpyReceiver()
    assert BlobEventConsumer(queue, receiver).run_once() == 0 and receiver.paths == [] and queue.settled == []


def test_a_message_that_is_not_an_event_is_dead_lettered_without_logging_its_body():
    queue, receiver = FakeQueue(b"not json", {"hello": "world"}, ["text"]), SpyReceiver()
    with capture_logs() as logs:
        BlobEventConsumer(queue, receiver).run_once()
    assert receiver.paths == []
    assert [entry[0] for entry in queue.settled] == ["dead_letter"] * 3
    assert logs == [{"event": "blob_event_unreadable", "log_level": "warning"}] * 3


def test_when_saving_fails_the_message_goes_back_to_the_queue():
    queue, receiver = FakeQueue(created()), SpyReceiver(error=RuntimeError("patient 123 secret"))
    with capture_logs() as logs:
        BlobEventConsumer(queue, receiver).run_once()
    assert queue.settled == [("abandon", 0)]
    assert logs == [{"event": "blob_event_failed", "error_type": "RuntimeError", "log_level": "error"}]


def test_closing_the_consumer_closes_the_queue():
    queue = FakeQueue()
    BlobEventConsumer(queue, SpyReceiver()).close()
    assert queue.closed


def test_one_message_that_cannot_be_settled_does_not_stop_the_others_of_the_same_read():
    queue, receiver = FakeQueue(b"not json", created("a.json"), created("b.json")), SpyReceiver()
    queue.dead_letter_fails, queue.complete_fails = True, 1               # message 0 and message 1 cannot be settled
    with capture_logs() as logs:
        assert BlobEventConsumer(queue, receiver).run_once() == 3
    assert receiver.paths == ["a.json", "b.json"] and queue.settled == [("complete", 2)]
    assert [e["event"] for e in logs] == ["blob_event_unreadable", "blob_event_settle_failed",
                                          "blob_event_settle_failed"]
    assert all(set(e) <= {"event", "log_level", "error_type"} for e in logs)
