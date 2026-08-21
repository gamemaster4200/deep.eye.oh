"""Exercises discover.py's namespace resolution, paginated page-list
merging, and redirect flagging/resolution against fixture responses."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _http
import _mediawiki
import discover

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wiki" / "api_responses"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_discover_all_pages_merges_paginated_namespace_results(monkeypatch):
    allpages_responses = [_load("allpages_page1.json"), _load("allpages_page2_continue.json")]

    def fake_fetch_json(url, **kwargs):
        return allpages_responses.pop(0)

    monkeypatch.setattr(_mediawiki._http, "fetch_json", fake_fetch_json)

    namespaces = {-1: "Special", 0: "(Main)"}
    pages = discover.discover_all_pages("https://example.fandom.com", namespaces)

    assert len(pages) == 3  # Special (ns<0) skipped entirely
    titles = {p["title"] for p in pages}
    assert titles == {"Basic", "Destroyer", "Overlord Redirect"}
    overlord_redirect = next(p for p in pages if p["title"] == "Overlord Redirect")
    assert overlord_redirect["is_redirect"] is True
    basic = next(p for p in pages if p["title"] == "Basic")
    assert basic["is_redirect"] is False
    assert basic["categories"] == ["Category:Tanks", "Category:Tier 1"]
    assert basic["namespace_name"] == "(Main)"


def test_resolve_redirects_fills_target_title_and_pageid(monkeypatch):
    resolution_response = _load("redirects_resolution.json")

    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: resolution_response)

    pages = [
        {"pageid": 101, "title": "Basic", "is_redirect": False},
        {"pageid": 103, "title": "Overlord Redirect", "is_redirect": True},
    ]
    discover.resolve_redirects("https://example.fandom.com", pages)

    basic = pages[0]
    redirect_page = pages[1]
    assert basic["redirect_target_title"] is None
    assert basic["redirect_target_pageid"] is None
    assert redirect_page["redirect_target_title"] == "Overlord"
    assert redirect_page["redirect_target_pageid"] == 104


def test_robots_5xx_unreachable_fails_closed_and_records_failure(tmp_path, monkeypatch):
    """robots.txt being genuinely unreachable (5xx/network, after retries)
    must refuse to proceed and record the attempt -- never silently skip
    the robots.txt check and never raise an unhandled traceback."""

    def fake_check_robots(base_url, *, user_agent, timeout):
        raise _http.TransientAcquisitionError("robots.txt unreachable (500) after 4 attempt(s)", http_status=500)

    monkeypatch.setattr(discover._http, "check_robots", fake_check_robots)

    out_path = tmp_path / "pages_index.json"
    with pytest.raises(SystemExit):
        discover._check_robots_or_fail_closed("https://example.fandom.com", user_agent="ua/1", timeout=5.0, out_path=out_path)

    log_path = out_path.parent / "acquisition_attempt_log.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["target_resource"] == "robots.txt"
    assert record["outcome"] == "unreachable_failed_closed"
    assert record["http_status"] == 500
    assert not out_path.exists()  # no pages_index.json written -- nothing fabricated


def test_robots_4xx_is_unavailable_and_acquisition_proceeds(monkeypatch):
    """Per RFC 9309 2.3.1.3, a 4xx robots.txt response is "unavailable" --
    the crawler MAY proceed with no restrictions. This must NOT raise and
    must return parser=None (no restriction enforced)."""

    def fake_check_robots(base_url, *, user_agent, timeout):
        return {"url": base_url + "/robots.txt", "outcome": "unavailable_permitted", "http_status": 403, "reason": "HTTP 403", "checked_at": "2026-01-01T00:00:00Z", "parser": None}

    monkeypatch.setattr(discover._http, "check_robots", fake_check_robots)

    result = discover._check_robots_or_fail_closed("https://example.fandom.com", user_agent="ua/1", timeout=5.0, out_path=Path("unused.json"))
    assert result["outcome"] == "unavailable_permitted"
    assert result["parser"] is None


def test_build_pages_index_shape():
    namespaces = {0: "(Main)", 14: "Category"}
    pages = [{"pageid": 1, "title": "X"}]
    robots_status = {"url": "https://example.fandom.com/robots.txt", "outcome": "obeyed", "http_status": 200, "reason": None, "checked_at": "2026-01-01T00:00:00Z", "parser": object()}
    index = discover.build_pages_index("https://example.fandom.com", namespaces, pages, robots_status)
    assert index["format_version"] == 1
    assert index["wiki_base_url"] == "https://example.fandom.com"
    assert index["namespaces"] == {"0": "(Main)", "14": "Category"}
    assert index["pages"] == pages
    assert index["robots_txt_status"]["outcome"] == "obeyed"
    assert "parser" not in index["robots_txt_status"]  # not JSON-serializable, and not provenance-relevant
    assert "generated_at" in index
