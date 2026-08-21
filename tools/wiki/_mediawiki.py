"""MediaWiki action=query request builders and continuation handling, built
on top of _http.fetch_json. Targets the Fandom/MediaWiki `api.php` action
API with formatversion=2 JSON output.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlencode

import _http

BASE_PARAMS = {"format": "json", "formatversion": "2"}

# Non-bot accounts may batch up to 50 titles/pageids per call.
MAX_BATCH_SIZE = 50


def build_api_url(base_url: str, params: dict) -> str:
    query = dict(BASE_PARAMS)
    query.update(params)
    return f"{base_url.rstrip('/')}/api.php?{urlencode(query)}"


def query(base_url: str, params: dict, **fetch_kwargs) -> dict:
    url = build_api_url(base_url, {"action": "query", **params})
    return _http.fetch_json(url, **fetch_kwargs)


def query_continued(base_url: str, params: dict, **fetch_kwargs) -> Iterator[dict]:
    """Yield each successive action=query response, following the API's
    `continue` token until it stops returning one."""
    request_params = dict(params)
    while True:
        response = query(base_url, request_params, **fetch_kwargs)
        yield response
        cont = response.get("continue")
        if not cont:
            return
        request_params = dict(params)
        request_params.update(cont)


def chunked(items: list, size: int = MAX_BATCH_SIZE) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def siteinfo_params() -> dict:
    return {"meta": "siteinfo", "siprop": "namespaces|general"}


def parse_namespaces(siteinfo_response: dict) -> dict[int, str]:
    """Map namespace id -> namespace name, from a real siteinfo response.
    Never hardcoded — Fandom wikis add custom namespaces beyond the
    standard MediaWiki set (Message Wall, Board, etc.)."""
    namespaces = siteinfo_response["query"]["namespaces"]
    result: dict[int, str] = {}
    for ns_id_str, info in namespaces.items():
        ns_id = int(ns_id_str)
        name = info.get("name") or info.get("canonical") or ""
        result[ns_id] = name if name else "(Main)"
    return result


def allpages_params(namespace_id: int, *, limit: int = 500) -> dict:
    return {
        "generator": "allpages",
        "gapnamespace": namespace_id,
        "gaplimit": limit,
        "prop": "info|categories",
        "inprop": "url",
        "cllimit": 500,
    }


def parse_pages(response: dict) -> list[dict]:
    """Extract the `query.pages` list from an action=query response
    (formatversion=2: always a flat list, missing pages carry `missing`)."""
    return response.get("query", {}).get("pages", [])


def redirect_resolution_params(titles: list[str]) -> dict:
    return {"titles": "|".join(titles), "redirects": 1, "prop": "info", "inprop": "url"}


def parse_redirect_resolution(response: dict) -> dict[str, dict]:
    """Map source redirect title -> {"to_title", "to_pageid"} (pageid is
    None if the target page isn't present in this response's `pages`)."""
    q = response.get("query", {})
    redirects = q.get("redirects", [])
    pages_by_title = {p["title"]: p for p in q.get("pages", []) if "missing" not in p}
    result: dict[str, dict] = {}
    for r in redirects:
        target = pages_by_title.get(r["to"])
        result[r["from"]] = {
            "to_title": r["to"],
            "to_pageid": target["pageid"] if target else None,
        }
    return result


def revisions_params(pageids: list[int]) -> dict:
    return {
        "pageids": "|".join(str(p) for p in pageids),
        "prop": "revisions|categories|info",
        "rvprop": "ids|timestamp|content|contentmodel",
        "rvslots": "main",
        "inprop": "url",
        "cllimit": 500,
    }
