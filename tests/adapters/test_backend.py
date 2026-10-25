"""WrapperBackend against a fake Backend (``httpx.MockTransport``): login, token reuse, outcomes, no secrets in logs."""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from structlog.testing import capture_logs

from patient360.adapters.backend import WrapperBackend
from patient360.config.settings import BackendSettings
from patient360.ports import BackendRejected, BackendUnavailable

SETTINGS = BackendSettings(
    base_url="https://wrapper.test", caller_source="SRC", caller_user_name="CALLER", username="user1",
    caller_source_key="KEY-SECRET-123", password="PASSWORD-SECRET-456")
BODY = {"visit": 7, "ai": {"en": "text"}}
OK = {"response": {"code": -1, "message": "Success"}, "data": {"Patient360Loader": {"ID": 21, "Action": "Insert"}}}


def login_reply(token: str = "jwt-1", minutes: int = 480) -> dict:
    return {"response": {"code": -1, "message": "Success"},
            "data": {"CallerCredential": {"Caller": {"TokenExpiredMin": minutes}, "token": token}}}


class FakeWrapper:
    """Records every request; ``post_replies`` is consumed one per post (the last one repeats)."""

    def __init__(self, post_replies=None, login=None):
        self.requests: list[httpx.Request] = []
        self.logins = 0
        self.post_replies = list(post_replies or [httpx.Response(200, json=OK)])
        self.login = login or (lambda n: httpx.Response(200, json=login_reply(f"jwt-{n}")))
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            if request.url.path.endswith("Callerlogin"):
                self.logins += 1
                n = self.logins
            else:
                reply = self.post_replies.pop(0) if len(self.post_replies) > 1 else self.post_replies[0]
        if request.url.path.endswith("Callerlogin"):
            time.sleep(0.01)          # widen the race for the thread test
            return self.login(n)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("Patient360011")]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(fake: FakeWrapper, clock: Clock | None = None) -> WrapperBackend:
    return WrapperBackend(SETTINGS, 5.0, 30.0, transport=httpx.MockTransport(fake), clock=clock or Clock())


def test_exact_requests():
    fake = FakeWrapper()
    make(fake).post_result(BODY)
    login, post = fake.requests
    assert str(login.url) == "https://wrapper.test/API/v2/Auth/Callerlogin" and login.method == "POST"
    assert json.loads(login.content) == {"username": "user1", "password": "PASSWORD-SECRET-456"}
    callers = {"CallerSource": "SRC", "CallerSourceKey": "KEY-SECRET-123", "CallerUserName": "CALLER"}
    for request in (login, post):
        assert {k: request.headers[k] for k in callers} == callers
        assert request.headers["Content-Type"] == "application/json"
    assert "Authorization" not in login.headers
    assert str(post.url) == "https://wrapper.test/API/v2/Patient360/Patient360011"
    assert post.headers["Authorization"] == "Bearer jwt-1"
    assert json.loads(post.content) == BODY


def test_login_once_for_many_posts():
    fake = FakeWrapper()
    backend = make(fake)
    for _ in range(5):
        backend.post_result(BODY)
    assert fake.logins == 1 and len(fake.posts) == 5


def test_token_renewed_when_under_ten_minutes_left():
    fake, clock = FakeWrapper(), Clock()
    backend = make(fake, clock)
    backend.post_result(BODY)
    clock.now += 470 * 60                       # 10 minutes left: still reused
    backend.post_result(BODY)
    assert fake.logins == 1
    clock.now += 60 + 1                         # under 10 minutes left: log in again
    backend.post_result(BODY)
    assert fake.logins == 2
    assert fake.posts[-1].headers["Authorization"] == "Bearer jwt-2"


def test_401_logs_in_once_and_retries_once():
    fake = FakeWrapper(post_replies=[httpx.Response(401), httpx.Response(200, json=OK)])
    make(fake).post_result(BODY)
    assert fake.logins == 2 and len(fake.posts) == 2
    assert fake.posts[1].headers["Authorization"] == "Bearer jwt-2"


def test_second_401_is_unavailable():
    fake = FakeWrapper(post_replies=[httpx.Response(401)])
    with pytest.raises(BackendUnavailable):
        make(fake).post_result(BODY)
    assert fake.logins == 2 and len(fake.posts) == 2


def test_success_code_minus_one_returns_none():
    assert make(FakeWrapper()).post_result(BODY) is None


@pytest.mark.parametrize("code", [0, 1])
def test_other_code_is_unavailable(code):
    reply = httpx.Response(200, json={"response": {"code": code, "message": "x"}})
    with pytest.raises(BackendUnavailable):
        make(FakeWrapper(post_replies=[reply])).post_result(BODY)


@pytest.mark.parametrize("reply", [
    httpx.Response(500), httpx.Response(503), httpx.Response(429),
    httpx.ReadTimeout("slow"), httpx.ConnectError("down"),
    httpx.Response(200, content=b"<html>not json</html>"),
    httpx.Response(200, json={"unexpected": True}),
])
def test_retryable_failures(reply):
    with pytest.raises(BackendUnavailable):
        make(FakeWrapper(post_replies=[reply])).post_result(BODY)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_4xx_is_rejected(status):
    with pytest.raises(BackendRejected):
        make(FakeWrapper(post_replies=[httpx.Response(status)])).post_result(BODY)


def test_failed_login_is_unavailable_and_next_call_logs_in_again():
    replies = [httpx.Response(500), httpx.Response(200, json={"response": {"code": 3}}),
               httpx.Response(200, content=b"nope"), httpx.Response(200, json=login_reply())]
    fake = FakeWrapper(login=lambda n: replies[n - 1])
    backend = make(fake)
    for _ in range(3):
        with pytest.raises(BackendUnavailable):
            backend.post_result(BODY)
    assert fake.posts == []
    backend.post_result(BODY)
    assert fake.logins == 4 and len(fake.posts) == 1


def test_login_connection_error_is_unavailable():
    def down(n):
        raise httpx.ConnectError("down")
    with pytest.raises(BackendUnavailable):
        make(FakeWrapper(login=down)).post_result(BODY)


def test_many_threads_one_login():
    fake = FakeWrapper()
    backend = make(fake)
    errors = []

    def work():
        try:
            backend.post_result(BODY)
        except Exception as error:      # noqa: BLE001 - collected and asserted below
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and fake.logins == 1 and len(fake.posts) == 20


def test_no_secret_in_logs():
    secrets = ("KEY-SECRET-123", "PASSWORD-SECRET-456", "jwt-", "Bearer", "user1", "visit")
    cases = [
        FakeWrapper(post_replies=[httpx.Response(500)]),
        FakeWrapper(post_replies=[httpx.Response(400)]),
        FakeWrapper(post_replies=[httpx.ReadTimeout("KEY-SECRET-123 jwt-1")]),
        FakeWrapper(post_replies=[httpx.Response(200, json={"response": {"code": 1}})]),
        FakeWrapper(login=lambda n: httpx.Response(500, text="PASSWORD-SECRET-456")),
    ]
    errors = []
    with capture_logs() as logs:
        for fake in cases:
            with pytest.raises((BackendUnavailable, BackendRejected)) as caught:
                make(fake).post_result(BODY)
            errors.append(str(caught.value))
    assert len(logs) == len(cases)
    assert {entry["event"] for entry in logs} == {"backend_unavailable", "backend_rejected", "backend_login_failed"}
    assert all(set(entry) <= {"event", "log_level", "reason"} for entry in logs)
    assert not any(secret in str(logs) + str(errors) for secret in secrets)


def test_the_errors_say_what_went_wrong_in_plain_words():
    def reason(fake):
        with pytest.raises((BackendUnavailable, BackendRejected)) as caught:
            make(fake).post_result(BODY)
        return str(caught.value)

    refused = httpx.Response(200, json={"response": {"code": 1, "message": "Invalid user or password"}})
    assert reason(FakeWrapper(login=lambda n: refused)) == (
        "Login to the wrapper failed: HTTP 200, code 1: Invalid user or password")
    assert reason(FakeWrapper(login=lambda n: httpx.Response(200, json={"response": {"code": -1}, "data": {}}))) == (
        "Login to the wrapper failed: the reply has no token (data.CallerCredential.token)")
    assert reason(FakeWrapper(login=lambda n: httpx.Response(502, text="<html>"))) == (
        "Login to the wrapper failed: HTTP 502, the reply is not the expected JSON")
    assert reason(FakeWrapper(post_replies=[httpx.ReadTimeout("slow")])) == (
        "The wrapper did not accept the result: could not reach the wrapper (ReadTimeout)")
    not_saved = httpx.Response(200, json={"response": {"code": 7, "message": "Visit not found"}})
    assert reason(FakeWrapper(post_replies=[not_saved])) == (
        "The wrapper did not accept the result: HTTP 200, code 7: Visit not found")
    assert reason(FakeWrapper(post_replies=[httpx.Response(400, json={"response": {"message": "Bad body"}})])) == (
        "The wrapper refused the result: HTTP 400: Bad body")
