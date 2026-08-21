"""Small urllib-based HTTP layer shared by tools/wiki/*.

Provides rate limiting, retry/backoff for transient failures only, robots.txt
enforcement against the exact URL being requested, and an injectable
transport (`opener`) so tests never open a real socket.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import urllib.robotparser
from typing import Callable

Opener = Callable[[urllib.request.Request, float], bytes]

# HTTP statuses worth retrying: rate limiting and server-side hiccups.
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class PermanentAcquisitionError(Exception):
    """A request failed in a way that will not resolve on retry."""

    def __init__(self, message: str, *, http_status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.reason = reason


class TransientAcquisitionError(Exception):
    """A request failed transiently and retries were exhausted."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def add_http_args(parser: argparse.ArgumentParser) -> None:
    """Shared CLI flags for tools/wiki/discover.py and fetch.py."""
    parser.add_argument("--delay", type=float, default=1.0, help="minimum seconds between requests (also the retry backoff base)")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="max retry attempts for transient failures")
    parser.add_argument(
        "--user-agent",
        required=True,
        help=(
            "generic, project-identifying User-Agent string, e.g. "
            '"deep-eye-oh-wiki-inventory/0.1 (+research tool; see project repo)". '
            "Never include personal contact info — there is deliberately no default "
            "or environment-variable fallback for this flag."
        ),
    )


def classify_http_error(exc: urllib.error.HTTPError | urllib.error.URLError) -> str:
    """Return "transient" or "permanent" for an HTTP/URL error."""
    if isinstance(exc, urllib.error.HTTPError):
        return "transient" if exc.code in TRANSIENT_HTTP_STATUSES else "permanent"
    return "transient"  # connection/timeout errors are always treated as transient


class RateLimiter:
    """Enforces a minimum delay between successive `wait()` calls."""

    def __init__(self, delay: float, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._delay = delay
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            remaining = self._delay - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
        self._last_call = self._clock()


def _backoff_seconds(attempt: int, base_delay: float) -> float:
    return base_delay * (2 ** (attempt - 1))


def _fetch_bytes(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    retries: int = 3,
    delay: float = 1.0,
    rate_limiter: RateLimiter | None = None,
    robot_parser: urllib.robotparser.RobotFileParser | None = None,
    opener: Opener = default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """GET `url` with the given User-Agent, retrying transient failures
    (429/5xx/connection/timeout) up to `retries` times with exponential
    backoff (base `delay`); permanent failures (4xx other than 429) raise
    immediately without retrying. `robot_parser`, if given, is checked
    against this exact `url` before any request is sent — enforcement
    covers only the actual path being requested, never an unrelated
    representative path. Shared by fetch_json and fetch_robots_txt so both
    get identical retry/classification/robots-enforcement behavior."""
    if robot_parser is not None and not robot_parser.can_fetch(user_agent, url):
        raise SystemExit(f"robots.txt disallows fetching {url} for user-agent {user_agent!r} — refusing to proceed")

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    attempt = 0
    while True:
        attempt += 1
        if rate_limiter is not None:
            rate_limiter.wait()
        try:
            return opener(request, timeout)
        except urllib.error.HTTPError as exc:
            if classify_http_error(exc) == "permanent":
                raise PermanentAcquisitionError(f"permanent HTTP error {exc.code} for {url}", http_status=exc.code, reason=str(exc.reason)) from exc
            if attempt > retries:
                raise TransientAcquisitionError(f"transient HTTP error {exc.code} for {url} after {attempt} attempt(s)", http_status=exc.code) from exc
            sleep(_backoff_seconds(attempt, delay))
        except urllib.error.URLError as exc:
            if attempt > retries:
                raise TransientAcquisitionError(f"transient network error for {url} after {attempt} attempt(s): {exc}") from exc
            sleep(_backoff_seconds(attempt, delay))


def fetch_robots_txt(
    base_url: str,
    *,
    user_agent: str,
    timeout: float,
    retries: int = 3,
    delay: float = 1.0,
    opener: Opener = default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    url = base_url.rstrip("/") + "/robots.txt"
    raw = _fetch_bytes(url, user_agent=user_agent, timeout=timeout, retries=retries, delay=delay, opener=opener, sleep=sleep)
    return raw.decode("utf-8", errors="replace")


def build_robot_parser(
    base_url: str, *, user_agent: str, timeout: float, retries: int = 3, delay: float = 1.0, opener: Opener = default_opener
) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    text = fetch_robots_txt(base_url, user_agent=user_agent, timeout=timeout, retries=retries, delay=delay, opener=opener)
    rp.parse(text.splitlines())
    return rp


def fetch_json(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    retries: int = 3,
    delay: float = 1.0,
    rate_limiter: RateLimiter | None = None,
    robot_parser: urllib.robotparser.RobotFileParser | None = None,
    opener: Opener = default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """GET `url` with the given User-Agent and decode the JSON body. See
    `_fetch_bytes` for retry/backoff/robots-enforcement behavior."""
    raw = _fetch_bytes(
        url, user_agent=user_agent, timeout=timeout, retries=retries, delay=delay, rate_limiter=rate_limiter, robot_parser=robot_parser, opener=opener, sleep=sleep
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PermanentAcquisitionError(f"non-JSON response from {url}") from exc
