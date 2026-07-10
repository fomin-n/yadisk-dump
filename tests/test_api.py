from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from yadisk import exceptions as yadisk_exceptions

from yadisk_dump.api import (
    TokenExpiredError,
    YandexDiskAPI,
    retry_after_seconds,
    validate_download_url,
)


def test_api_uses_one_client_per_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []
    barrier = threading.Barrier(2)

    class FakeRequestsSession:
        def __init__(self) -> None:
            self.trust_env = True

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.session = SimpleNamespace(requests_session=FakeRequestsSession())
            created.append(self)

        def close(self) -> None:
            pass

    monkeypatch.setattr("yadisk_dump.api.yadisk.Client", FakeClient)
    api = YandexDiskAPI("secret")

    def get_client() -> object:
        client = api._client()
        barrier.wait()
        assert not client.session.requests_session.trust_env  # type: ignore[attr-defined]
        return client

    with ThreadPoolExecutor(max_workers=2) as pool:
        clients = list(pool.map(lambda _value: get_client(), range(2)))
    assert len(created) == 2
    assert clients[0] is not clients[1]


def test_rate_limit_does_not_consume_an_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    api = YandexDiskAPI("secret")
    monkeypatch.setattr(api, "_client", lambda: object())
    waits: list[float] = []
    monkeypatch.setattr(api, "wait", waits.append)
    calls = 0
    response = SimpleNamespace(
        _response=SimpleNamespace(headers={"Retry-After": "7"})
    )

    def operation(_client: object) -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise yadisk_exceptions.TooManyRequestsError(response=response)
        return "ok"

    assert api._call(operation, attempts=1) == "ok"
    assert calls == 3
    assert waits == [7.0, 7.0]


def test_http_date_retry_after_is_honored_without_date_header() -> None:
    target = datetime.now(timezone.utc) + timedelta(seconds=120)
    response = SimpleNamespace(headers={"Retry-After": format_datetime(target)})
    assert 118 <= retry_after_seconds(response) <= 120


def test_unauthorized_error_is_replaced_with_safe_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = YandexDiskAPI("secret")
    monkeypatch.setattr(api, "_client", lambda: object())

    def operation(_client: object) -> None:
        raise yadisk_exceptions.UnauthorizedError(msg="server detail")

    with pytest.raises(TokenExpiredError, match="token expired"):
        api._call(operation, attempts=1)


@pytest.mark.parametrize(
    "url",
    [
        "http://downloader.disk.yandex.ru/file",
        "https://user:password@downloader.disk.yandex.ru/file",
        "file:///tmp/data",
    ],
)
def test_signed_url_validation_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(Exception):
        validate_download_url(url)
