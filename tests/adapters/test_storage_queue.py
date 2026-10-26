import base64

from structlog.testing import capture_logs

from patient360.adapters.storage_queue import StorageQueueEventQueue, build_event_queue
from patient360.ports import EventMessage

EVENT = b'{"eventType": "Microsoft.Storage.BlobCreated", "subject": "/blobs/x"}'


class FakeMessage:
    """What the Azure SDK gives for a queue message."""

    def __init__(self, content, dequeue_count=1):
        self.content, self.dequeue_count = content, dequeue_count


class FakeQueueClient:
    def __init__(self, messages=()):
        self.messages, self.calls, self.closed = list(messages), [], False

    def receive_messages(self, messages_per_page, max_messages, visibility_timeout):
        self.calls.append(("receive", messages_per_page, max_messages, visibility_timeout))
        return iter(self.messages)

    def delete_message(self, message):
        self.calls.append(("delete", message))

    def send_message(self, content):
        if getattr(self, "poison_is_broken", False):
            raise RuntimeError("poison queue missing")
        self.calls.append(("send", content))

    def update_message(self, message, visibility_timeout):
        self.calls.append(("hide", message, visibility_timeout))

    def close(self):
        self.closed = True


class FakeCredential:
    def get_token(self, *scopes, **kwargs):
        raise NotImplementedError          # only its shape matters: the queue is never called


def make(*messages, **options):
    queue, poison = FakeQueueClient(messages), FakeQueueClient()
    return StorageQueueEventQueue(queue, poison, **options), queue, poison


def test_plain_json_and_base64_messages_are_both_read_and_the_message_is_kept_to_delete_it_later():
    plain, encoded = FakeMessage(EVENT.decode()), FakeMessage(base64.b64encode(EVENT).decode())
    adapter, queue, _ = make(plain, encoded, max_messages=4, visibility_seconds=120)
    assert adapter.receive() == [EventMessage(body=EVENT, handle=plain), EventMessage(body=EVENT, handle=encoded)]
    assert queue.calls == [("receive", 4, 4, 120)]


def test_complete_deletes_the_message():
    message = FakeMessage(EVENT.decode())
    adapter, queue, _ = make()
    adapter.complete(EventMessage(body=EVENT, handle=message))
    assert queue.calls == [("delete", message)]


def test_dead_letter_copies_the_message_to_the_poison_queue_and_deletes_it():
    message = FakeMessage("not an event")
    adapter, queue, poison = make()
    adapter.dead_letter(EventMessage(body=b"not an event", handle=message), "bad body")
    assert poison.calls == [("send", "not an event")] and queue.calls == [("delete", message)]


def test_a_message_read_too_many_times_goes_to_the_poison_queue_instead_of_being_returned():
    tired, fresh = FakeMessage(EVENT.decode(), dequeue_count=11), FakeMessage(EVENT.decode(), dequeue_count=10)
    adapter, queue, poison = make(tired, fresh, max_deliveries=10)
    assert [m.handle for m in adapter.receive()] == [fresh]
    assert poison.calls == [("send", EVENT.decode())] and ("delete", tired) in queue.calls


def test_text_that_is_neither_json_nor_base64_is_passed_on_so_the_consumer_can_reject_it():
    message = FakeMessage("hello world!")
    adapter, _, _ = make(message)
    assert adapter.receive() == [EventMessage(body=b"hello world!", handle=message)]


def test_close_closes_both_queues():
    adapter, queue, poison = make()
    adapter.close()
    assert queue.closed and poison.closed


def test_the_queue_is_built_from_the_azurite_connection_string_and_from_the_blob_url():
    local = build_event_queue(
        "blob-events", connection_string="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey="
        "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;")
    assert local._queue.queue_name == "blob-events" and local._poison.queue_name == "blob-events-poison"
    azure = build_event_queue("blob-events", account_url="https://p360stg.blob.core.windows.net",
                              credential=FakeCredential())
    assert azure._queue.url.startswith("https://p360stg.queue.core.windows.net/blob-events")
    assert azure._poison.queue_name == "blob-events-poison"


def test_a_broken_poison_queue_does_not_stop_the_good_messages_of_the_same_read():
    tired, fresh = FakeMessage(EVENT.decode(), dequeue_count=999), FakeMessage(EVENT.decode())
    adapter, queue, poison = make(tired, fresh)
    poison.poison_is_broken = True
    with capture_logs() as logs:
        received = adapter.receive()
    assert [m.handle for m in received] == [fresh] and ("delete", tired) not in queue.calls
    assert logs == [{"event": "event_message_poison_failed", "error_type": "RuntimeError", "log_level": "error"}]


def test_a_message_is_not_given_up_for_a_long_outage_and_its_retry_wait_grows():
    adapter, queue, poison = make()
    for tries, hidden in [(1, 120), (2, 240), (3, 480), (6, 3600), (40, 3600)]:
        adapter.abandon(EventMessage(body=EVENT, handle=FakeMessage(EVENT.decode(), dequeue_count=tries)))
        assert queue.calls[-1][0] == "hide" and queue.calls[-1][2] == hidden
    # 49 reads is still a live message with the default limit; read 51 is the first one given up
    live, dead = FakeMessage(EVENT.decode(), dequeue_count=49), FakeMessage(EVENT.decode(), dequeue_count=51)
    adapter, queue, poison = make(live, dead)
    assert [m.handle for m in adapter.receive()] == [live] and poison.calls == [("send", EVENT.decode())]
