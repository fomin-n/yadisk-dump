"""Minimal read-only wrapper around the official Yandex.Disk SDK."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import requests
import yadisk
from yadisk import exceptions as yadisk_exceptions

API_BASE_URL = "https://cloud-api.yandex.net"
DEFAULT_TIMEOUT = (10.0, 60.0)
MAX_ATTEMPTS = 5
MAX_BACKOFF = 16.0

T = TypeVar("T")


class ApiError(RuntimeError):
    """Base class for safe, token-free API errors."""


class TokenExpiredError(ApiError):
    """Raised when Yandex rejects the OAuth token."""


class RemoteMissingError(ApiError):
    """Raised when a remote resource disappeared during a run."""


class TransientApiError(ApiError):
    """Raised after retryable API failures exhaust their attempts."""


class OperationCancelled(ApiError):
    """Raised when the shared cancellation event is set."""


@dataclass(frozen=True, slots=True)
class DiskAccount:
    """Account information displayed after token validation."""

    login: str
    total_space: int
    used_space: int


class YandexDiskAPI:
    """Provide only token checks, listings, disk info, and download links."""

    def __init__(self, token: str, stop_event: threading.Event | None = None) -> None:
        """Create a read-only API facade with per-thread clients and sessions."""
        self._token = token
        self.stop_event = stop_event or threading.Event()
        self._local = threading.local()

    def check_token(self) -> bool:
        """Return whether the configured token is accepted by Yandex."""
        return bool(self._call(lambda client: client.check_token(), attempts=MAX_ATTEMPTS))

    def get_disk_info(self) -> DiskAccount:
        """Return login and quota information for the configured account."""
        info = self._call(lambda client: client.get_disk_info(), attempts=MAX_ATTEMPTS)
        user = getattr(info, "user", None)
        return DiskAccount(
            login=str(getattr(user, "login", "unknown") or "unknown"),
            total_space=int(getattr(info, "total_space", 0) or 0),
            used_space=int(getattr(info, "used_space", 0) or 0),
        )

    def listdir(self, path: str) -> list[Any]:
        """List a directory completely; the SDK performs pagination internally."""
        return self._call(
            lambda client: list(client.listdir(path, max_items=None)),
            attempts=MAX_ATTEMPTS,
        )

    def get_download_link(self, path: str, *, attempts: int = MAX_ATTEMPTS) -> str:
        """Request a fresh temporary download link for one remote file."""
        return str(
            self._call(lambda client: client.get_download_link(path), attempts=attempts)
        )

    def transfer_session(self) -> requests.Session:
        """Return the current thread's proxy-free byte-transfer session."""
        session = getattr(self._local, "transfer_session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.headers.update({"Accept-Encoding": "identity"})
            self._local.transfer_session = session
        return session

    def wait(self, seconds: float) -> None:
        """Wait interruptibly, raising when cancellation is requested."""
        if self.stop_event.wait(max(0.0, seconds)):
            raise OperationCancelled("operation cancelled")

    def close_current_thread(self) -> None:
        """Close API and transfer sessions owned by the current thread."""
        client = getattr(self._local, "client", None)
        if client is not None:
            client.close()
            del self._local.client
        session = getattr(self._local, "transfer_session", None)
        if session is not None:
            session.close()
            del self._local.transfer_session

    def _client(self) -> yadisk.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = yadisk.Client(
                token=self._token,
                session="requests",
                default_args={"n_retries": 0, "timeout": DEFAULT_TIMEOUT},
            )
            # The documented RequestsSession exposes its underlying requests session.
            client.session.requests_session.trust_env = False
            self._local.client = client
        return client

    def _call(self, operation: Any, *, attempts: int) -> T:
        attempt = 0
        while attempt < attempts:
            if self.stop_event.is_set():
                raise OperationCancelled("operation cancelled")
            try:
                return operation(self._client())
            except yadisk_exceptions.UnauthorizedError as error:
                raise TokenExpiredError(
                    "token expired — run yadisk-dump to re-authenticate"
                ) from error
            except yadisk_exceptions.PathNotFoundError as error:
                raise RemoteMissingError("remote file no longer exists") from error
            except yadisk_exceptions.TooManyRequestsError as error:
                self.wait(retry_after_seconds(error, default=60.0))
                continue
            except (
                yadisk_exceptions.GoneError,
                yadisk_exceptions.RetriableYaDiskError,
                yadisk_exceptions.RequestError,
            ) as error:
                attempt += 1
                if attempt >= attempts:
                    raise TransientApiError("Yandex API request failed after retries") from error
                self.wait(min(2 ** (attempt - 1), MAX_BACKOFF))
            except yadisk_exceptions.YaDiskError as error:
                raise ApiError("Yandex API rejected the read-only request") from error
        raise TransientApiError("Yandex API request failed after retries")


def validate_download_url(url: str) -> str:
    """Validate a signed HTTPS URL and return its normalized hostname."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ApiError("Yandex returned an unsafe download link")
    return parsed.hostname.rstrip(".").lower()


def retry_after_seconds(error_or_response: Any, *, default: float = 60.0) -> float:
    """Parse a safe Retry-After delay from an SDK exception or HTTP response."""
    response = getattr(error_or_response, "response", error_or_response)
    headers = getattr(response, "headers", None)
    if headers is None:
        wrapped = getattr(response, "_response", None)
        headers = getattr(wrapped, "headers", {})
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(value))
            now = (
                parsedate_to_datetime(headers.get("Date"))
                if headers.get("Date")
                else datetime.now(target.tzinfo or timezone.utc)
            )
            return max(0.0, (target - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return default
