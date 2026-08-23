"""Exercises _mediawiki.py's request builders and continuation handling
against fixture JSON responses -- no real sockets."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _mediawiki

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wiki" / "api_responses"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_build_api_url_includes_format_and_action():
    url = _mediawiki.build_api_url("https://example.fandom.com", {"action": "query", "meta": "siteinfo"})
    assert url.startswith("https://example.fandom.com/api.php?")
    assert "format=json" in url
    assert "formatversion=2" in url
    assert "action=query" in url
    assert "meta=siteinfo" in url


def test_allpages_params_shape():
    params = _mediawiki.allpages_params(0, limit=10)
    assert params["generator"] == "allpages"
    assert params["gapnamespace"] == 0
    assert params["gaplimit"] == 10
    assert "categories" in params["prop"]


def test_revisions_params_requests_content_but_not_user_or_comment():
    params = _mediawiki.revisions_params([1, 2, 3])
    assert params["pageids"] == "1|2|3"
    assert "content" in params["rvprop"]
    assert "user" not in params["rvprop"]
    assert "comment" not in params["rvprop"]
    assert params["rvslots"] == "main"


def test_chunked_splits_into_batches_of_max_size():
    items = list(range(120))
    batches = list(_mediawiki.chunked(items, size=50))
    assert [len(b) for b in batches] == [50, 50, 20]
    assert sum(batches, []) == items


def test_parse_namespaces_resolves_real_names_including_main():
    siteinfo = _load("siteinfo_namespaces.json")
    namespaces = _mediawiki.parse_namespaces(siteinfo)
    assert namespaces[0] == "(Main)"
    assert namespaces[14] == "Category"
    assert namespaces[1200] == "Message Wall"
    assert -1 in namespaces  # Special is present but callers filter it out


def test_query_continued_follows_continue_token(monkeypatch):
    page1 = _load("allpages_page1.json")
    page2 = _load("allpages_page2_continue.json")
    responses = [page1, page2]
    seen_params = []

    def fake_fetch_json(url, **kwargs):
        seen_params.append(url)
        return responses.pop(0)

    monkeypatch.setattr(_mediawiki._http, "fetch_json", fake_fetch_json)

    results = list(_mediawiki.query_continued("https://example.fandom.com", _mediawiki.allpages_params(0)))
    assert len(results) == 2
    all_pages = [p for r in results for p in _mediawiki.parse_pages(r)]
    assert {p["pageid"] for p in all_pages} == {101, 102, 103}
    # second call must have included the continue token from the first response
    assert "gapcontinue" in seen_params[1]


def test_parse_redirect_resolution_maps_from_to_and_pageid():
    response = _load("redirects_resolution.json")
    resolution = _mediawiki.parse_redirect_resolution(response)
    assert resolution == {"Overlord Redirect": {"to_title": "Overlord", "to_pageid": 104}}
