"""Exercises _http.py's retry/backoff, robots.txt enforcement, and required
user-agent handling against a fake opener -- no real sockets."""

import sys
import urllib.error
import urllib.robotparser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _http


class _FakeOpener:
    """Callable opener stand-in: pops the next scripted result (bytes to
    return, or an exception to raise) each call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _sleeps(recorder):
    def sleep(seconds):
        recorder.append(seconds)

    return sleep


def test_fetch_json_success_first_try():
    opener = _FakeOpener([b'{"ok": true}'])
    result = _http.fetch_json("https://example.test/api.php", user_agent="ua/1", timeout=5.0, opener=opener)
    assert result == {"ok": True}
    assert len(opener.calls) == 1


def test_fetch_json_retries_transient_then_succeeds():
    err = urllib.error.HTTPError("https://example.test/api.php", 503, "Service Unavailable", {}, None)
    opener = _FakeOpener([err, b'{"ok": true}'])
    sleeps = []
    result = _http.fetch_json(
        "https://example.test/api.php", user_agent="ua/1", timeout=5.0, retries=3, delay=0.1, opener=opener, sleep=_sleeps(sleeps)
    )
    assert result == {"ok": True}
    assert len(opener.calls) == 2
    assert sleeps == [0.1]  # exponential backoff base for attempt 1


def test_fetch_json_permanent_failure_never_retried():
    err = urllib.error.HTTPError("https://example.test/api.php", 404, "Not Found", {}, None)
    opener = _FakeOpener([err])
    with pytest.raises(_http.PermanentAcquisitionError):
        _http.fetch_json("https://example.test/api.php", user_agent="ua/1", timeout=5.0, retries=3, opener=opener)
    assert len(opener.calls) == 1


def test_fetch_json_transient_exhausts_retries():
    err = urllib.error.HTTPError("https://example.test/api.php", 500, "Server Error", {}, None)
    opener = _FakeOpener([err, err, err])
    sleeps = []
    with pytest.raises(_http.TransientAcquisitionError):
        _http.fetch_json(
            "https://example.test/api.php", user_agent="ua/1", timeout=5.0, retries=2, delay=0.01, opener=opener, sleep=_sleeps(sleeps)
        )
    assert len(opener.calls) == 3  # initial attempt + 2 retries
    assert sleeps == [0.01, 0.02]  # exponential backoff


def test_fetch_json_malformed_json_is_permanent():
    opener = _FakeOpener([b"not json"])
    with pytest.raises(_http.PermanentAcquisitionError):
        _http.fetch_json("https://example.test/api.php", user_agent="ua/1", timeout=5.0, opener=opener)


def test_rate_limiter_enforces_minimum_delay():
    clock = {"t": 0.0}
    sleeps = []

    def fake_clock():
        return clock["t"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    limiter = _http.RateLimiter(1.0, sleep=fake_sleep, clock=fake_clock)
    limiter.wait()  # first call: no sleep
    clock["t"] += 0.2  # only 0.2s elapsed
    limiter.wait()  # should sleep the remaining 0.8s
    assert sleeps == [0.8]


def test_robots_txt_disallowed_path_raises_system_exit():
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /api.php"])
    opener = _FakeOpener([b'{"ok": true}'])
    with pytest.raises(SystemExit):
        _http.fetch_json("https://example.test/api.php?action=query", user_agent="ua/1", timeout=5.0, robot_parser=rp, opener=opener)
    assert opener.calls == []  # never actually requested


def test_robots_txt_allowed_path_proceeds():
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(["User-agent: *", "Allow: /"])
    opener = _FakeOpener([b'{"ok": true}'])
    result = _http.fetch_json("https://example.test/api.php?action=query", user_agent="ua/1", timeout=5.0, robot_parser=rp, opener=opener)
    assert result == {"ok": True}


def test_check_robots_obeyed_on_success():
    opener = _FakeOpener([b"User-agent: *\nDisallow: /private/\n"])
    result = _http.check_robots("https://example.test", user_agent="ua/1", timeout=5.0, opener=opener)
    assert result["outcome"] == "obeyed"
    assert result["http_status"] == 200
    assert result["parser"] is not None
    assert result["parser"].can_fetch("ua/1", "https://example.test/private/x") is False
    assert result["parser"].can_fetch("ua/1", "https://example.test/api.php") is True


def test_check_robots_4xx_is_unavailable_permitted_no_retry():
    err = urllib.error.HTTPError("https://example.test/robots.txt", 403, "Forbidden", {}, None)
    opener = _FakeOpener([err])
    result = _http.check_robots("https://example.test", user_agent="ua/1", timeout=5.0, retries=3, opener=opener)
    assert result["outcome"] == "unavailable_permitted"
    assert result["http_status"] == 403
    assert result["parser"] is None
    assert len(opener.calls) == 1  # RFC 9309: 4xx is definitive, no retry needed


def test_check_robots_5xx_fails_closed_after_retries():
    err = urllib.error.HTTPError("https://example.test/robots.txt", 503, "Service Unavailable", {}, None)
    opener = _FakeOpener([err, err, err])
    sleeps = []
    with pytest.raises(_http.TransientAcquisitionError):
        _http.check_robots("https://example.test", user_agent="ua/1", timeout=5.0, retries=2, delay=0.01, opener=opener, sleep=_sleeps(sleeps))
    assert len(opener.calls) == 3


def test_check_robots_network_error_fails_closed_after_retries():
    opener = _FakeOpener([urllib.error.URLError("connection refused")])
    with pytest.raises(_http.TransientAcquisitionError):
        _http.check_robots("https://example.test", user_agent="ua/1", timeout=5.0, retries=0, opener=opener)


def test_fetch_head_returns_result_on_success():
    def fake_head_opener(request, timeout):
        assert request.get_method() == "HEAD"
        return _http.HeadResult(200, {"Last-Modified": "Fri, 12 Jun 2026 14:18:41 GMT", "Content-Length": "23053825"})

    result = _http.fetch_head("https://s3.amazonaws.com/dump.7z", user_agent="ua/1", timeout=5.0, opener=fake_head_opener)
    assert result.status == 200
    assert result.get("last-modified") == "Fri, 12 Jun 2026 14:18:41 GMT"  # case-insensitive lookup


def test_fetch_head_returns_none_on_failure():
    def fake_head_opener(request, timeout):
        raise urllib.error.HTTPError("https://s3.amazonaws.com/dump.7z", 404, "Not Found", {}, None)

    result = _http.fetch_head("https://s3.amazonaws.com/dump.7z", user_agent="ua/1", timeout=5.0, opener=fake_head_opener)
    assert result is None


def test_user_agent_flag_is_required_with_no_default():
    parser = __import__("argparse").ArgumentParser()
    _http.add_http_args(parser)
    with pytest.raises(SystemExit):
        parser.parse_args([])  # missing --user-agent
    args = parser.parse_args(["--user-agent", "some-agent/1.0"])
    assert args.user_agent == "some-agent/1.0"
    assert args.delay == 1.0
    assert args.timeout == 15.0
    assert args.retries == 3
