"""The Backend wrapper behind the ``BackendClient`` port: log in once, keep the token, post one result per call."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, NoReturn

import httpx
import structlog

from patient360.config.settings import BackendSettings
from patient360.ports import BackendRejected, BackendUnavailable

log = structlog.get_logger()

LOGIN_PATH = "/API/v2/Auth/Callerlogin"
RESULT_PATH = "/API/v2/Patient360/Patient360011"
SUCCESS_CODE = -1                 # the Backend's own success marker in ``response.code``
DEFAULT_TOKEN_MINUTES = 480       # used only if the login reply does not say
RENEW_BEFORE_S = 10 * 60          # log in again when less than this is left of the token's life
MAX_MESSAGE_CHARS = 200           # how much of the wrapper's own message we keep in an error


class WrapperBackend:
    def __init__(self, settings: BackendSettings, connect_timeout_s: float, read_timeout_s: float, *,
                 transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings, self._clock = settings, clock
        timeout = httpx.Timeout(connect=connect_timeout_s, read=read_timeout_s, write=read_timeout_s,
                                pool=connect_timeout_s)
        self._client = httpx.Client(base_url=settings.base_url, timeout=timeout, transport=transport)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def close(self) -> None:
        self._client.close()

    def post_result(self, body: dict[str, Any]) -> None:
        token = self._valid_token()
        response = self._post(body, token)
        if response.status_code == 401:       # the token was refused: log in once and retry this one post once
            response = self._post(body, self._valid_token(refused=token))
        self._check(response)

    def _caller_headers(self) -> dict[str, str]:
        s = self._settings
        return {"CallerSource": s.caller_source, "CallerSourceKey": s.caller_source_key,
                "CallerUserName": s.caller_user_name, "Content-Type": "application/json"}

    def _post_headers(self, token: str) -> dict[str, str]:
        # ASSUMPTION: the Backend has not confirmed this call's auth header; Bearer plus Caller headers is our guess.
        return {**self._caller_headers(), "Authorization": f"Bearer {token}"}

    def _valid_token(self, refused: str | None = None) -> str:
        """The saved token, or a new one if it is missing, nearly expired or was just refused. One login at a time:
        a thread that waited finds the new token and reuses it."""
        with self._lock:
            if (self._token is None or self._token == refused
                    or self._expires_at - self._clock() < RENEW_BEFORE_S):
                self._login()
            assert self._token is not None
            return self._token

    def _login(self) -> None:
        """Log in with the username and password; the reply holds the Bearer token. A failure raises
        ``BackendUnavailable`` with a readable reason (status, code and the wrapper's own message; never a secret)."""
        s = self._settings
        self._token = None
        try:
            response = self._client.post(LOGIN_PATH, headers=self._caller_headers(),
                                         json={"username": s.username, "password": s.password})
        except httpx.HTTPError as error:
            self._login_failed(f"could not reach the wrapper ({type(error).__name__})")
        status = response.status_code
        try:
            data = response.json()
            code = data["response"]["code"]
        except Exception:
            self._login_failed(f"HTTP {status}, the reply is not the expected JSON")
        if status != 200 or code != SUCCESS_CODE:
            self._login_failed(f"HTTP {status}, code {code}: {_wrapper_message(response)}")
        try:
            credential = data["data"]["CallerCredential"]
            token = credential["token"]
            minutes = credential["Caller"].get("TokenExpiredMin", DEFAULT_TOKEN_MINUTES)
            expires_at = self._clock() + float(minutes) * 60
        except Exception:
            self._login_failed("the reply has no token (data.CallerCredential.token)")
        if not token:
            self._login_failed("the reply has an empty token")
        self._token, self._expires_at = token, expires_at

    @staticmethod
    def _login_failed(reason: str) -> NoReturn:
        message = f"Login to the wrapper failed: {reason}"
        log.warning("backend_login_failed", reason=reason)
        raise BackendUnavailable(message) from None

    def _post(self, body: dict[str, Any], token: str) -> httpx.Response:
        try:
            return self._client.post(RESULT_PATH, headers=self._post_headers(token), json=body)
        except httpx.HTTPError as error:      # timeout, no connection, a broken stream
            self._unavailable(f"could not reach the wrapper ({type(error).__name__})")

    def _check(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 429 or status >= 500 or status == 401:
            self._unavailable(f"HTTP {status}: {_wrapper_message(response)}")
        if status >= 400:
            reason = f"HTTP {status}: {_wrapper_message(response)}"
            log.warning("backend_rejected", reason=reason)
            raise BackendRejected(f"The wrapper refused the result: {reason}")
        try:
            code = response.json()["response"]["code"]
        except Exception:
            self._unavailable(f"HTTP {status}, the reply is not the expected JSON")
        if code != SUCCESS_CODE:              # play safe: any other code is "not success", so the sender retries
            self._unavailable(f"HTTP {status}, code {code}: {_wrapper_message(response)}")

    @staticmethod
    def _unavailable(reason: str) -> NoReturn:
        log.warning("backend_unavailable", reason=reason)
        raise BackendUnavailable(f"The wrapper did not accept the result: {reason}") from None


def _wrapper_message(response: httpx.Response) -> str:
    """The wrapper's own ``response.message``, short; empty if the reply has none."""
    try:
        return str(response.json()["response"]["message"])[:MAX_MESSAGE_CHARS]
    except Exception:
        return ""
