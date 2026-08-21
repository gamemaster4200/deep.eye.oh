"""Official Fandom database-dump acquisition backend: an alternative raw-
source backend to the page-by-page MediaWiki API (see fetch.py), preferred
when a sufficiently fresh dump is available -- one sanctioned bulk artifact
instead of thousands of live API calls. Produces the same per-page shape
fetch.py's API backend produces (see fetch.py's build_raw_page_record), so
inventory.py never needs to know which backend produced a given snapshot.

Dump discovery never scrapes a rendered wiki page: the dump's well-known
URL is derived from the wiki's `wikiid` (a standard MediaWiki siteinfo
field, obtained via the ordinary API), following Fandom's published S3
naming convention (as documented on every wiki's Special:Statistics page:
"Database dumps can be used as a personal backup ... or for maintenance
bots" -- https://s3.amazonaws.com/wikia_xml_dumps/<a>/<ab>/<wikiid>_pages_
<variant>.xml.7z). Only the "current" variant (latest revision per page,
no history) is used -- this project has no use for full revision history.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

import hashlib
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator

import py7zr

import _http
import _mediawiki

DEFAULT_DUMP_VARIANT = "current"


def dump_dbname_key(base_url: str, **fetch_kwargs) -> str:
    """The wiki's short dump-database name (MediaWiki's `wikiid` siteinfo
    field), used to derive the dump's URL. `fetch_kwargs` are forwarded to
    _mediawiki.query (user_agent/timeout/retries/delay/rate_limiter/
    robot_parser/opener) -- this one call goes through api.php, already
    known reachable, never a rendered `/wiki/...` page."""
    response = _mediawiki.query(base_url, {"meta": "siteinfo", "siprop": "general"}, **fetch_kwargs)
    wikiid = response.get("query", {}).get("general", {}).get("wikiid")
    if not wikiid:
        raise _http.PermanentAcquisitionError("siteinfo response did not include a wikiid; cannot derive a dump URL")
    return wikiid


def derive_dump_url(wikiid: str, *, variant: str = DEFAULT_DUMP_VARIANT) -> str:
    return f"https://s3.amazonaws.com/wikia_xml_dumps/{wikiid[0]}/{wikiid[:2]}/{wikiid}_pages_{variant}.xml.7z"


def probe_dump(url: str, *, user_agent: str, timeout: float, opener=_http.default_head_opener) -> dict | None:
    """HEAD-probe the dump URL. None if unusable; otherwise a dict with
    whatever freshness/identity metadata the response exposes."""
    result = _http.fetch_head(url, user_agent=user_agent, timeout=timeout, opener=opener)
    if result is None or result.status != 200:
        return None
    return {
        "url": url,
        "http_status": result.status,
        "last_modified": result.get("Last-Modified"),
        "content_length": result.get("Content-Length"),
        "etag": result.get("ETag"),
    }


def is_fresh_enough(last_modified: str | None, *, max_age_days: int) -> bool:
    """Unknown/unparseable Last-Modified is treated as NOT fresh enough --
    fail toward the API fallback rather than trusting unknown staleness."""
    if not last_modified:
        return False
    try:
        dt = parsedate_to_datetime(last_modified)
    except (TypeError, ValueError):
        return False
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= timedelta(days=max_age_days)


def download_and_extract(url: str, *, archive_out_path: Path, extract_dir: Path, **fetch_kwargs) -> tuple[Path, str]:
    """Downloads the dump archive to `archive_out_path` (kept as the
    immutable raw artifact alongside the snapshot) and extracts its XML
    member into `extract_dir`. Returns (extracted_xml_path, archive_
    sha256). `fetch_kwargs` are forwarded to _http.fetch_bytes."""
    archive_bytes = _http.fetch_bytes(url, **fetch_kwargs)
    archive_out_path.parent.mkdir(parents=True, exist_ok=True)
    archive_out_path.write_bytes(archive_bytes)
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    extract_dir.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(archive_out_path, mode="r") as archive:
        xml_names = [n for n in archive.getnames() if n.lower().endswith(".xml")]
        if not xml_names:
            raise _http.PermanentAcquisitionError(f"dump archive {url} contains no .xml member")
        archive.extractall(path=extract_dir)
    xml_path = extract_dir / xml_names[0]
    if not xml_path.exists():
        raise _http.PermanentAcquisitionError(f"expected extracted file {xml_path} not found after extracting {url}")
    return xml_path, archive_sha256


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem, local_name: str):
    for child in elem:
        if _local(child.tag) == local_name:
            return child
    return None


def _child_text(elem, local_name: str) -> str | None:
    child = _child(elem, local_name)
    return child.text if child is not None else None


def parse_dump_namespaces(xml_path: Path) -> dict[int, str]:
    """Namespace id -> name from the dump's own <siteinfo><namespaces>
    block -- self-contained (no live API dependency for re-normalization),
    and always precedes every <page> in document order, so this stops
    early rather than scanning the whole file."""
    namespaces: dict[int, str] = {}
    for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
        if _local(elem.tag) == "namespaces":
            for ns_elem in elem:
                if _local(ns_elem.tag) != "namespace":
                    continue
                name = ns_elem.text or ""
                namespaces[int(ns_elem.get("key"))] = name if name else "(Main)"
            elem.clear()
            break
    return namespaces


def parse_dump_base_url(xml_path: Path) -> str | None:
    """Wiki site root from <siteinfo><base> (which points at a specific
    article, e.g. .../wiki/Project:Home) -- used to reconstruct a
    canonical_url per page, since the dump has no per-page URL field."""
    for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
        if _local(elem.tag) == "base":
            text = elem.text
            elem.clear()
            return text.split("/wiki/")[0] if text else None
    return None


def build_title_to_pageid_index(xml_path: Path) -> dict[str, int]:
    """First pass over the whole dump: title -> pageid for every page in
    every namespace, so a redirect's target can be resolved to a pageid
    even when the target's own namespace is out of scope for content
    acquisition."""
    index: dict[str, int] = {}
    for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
        if _local(elem.tag) != "page":
            continue
        title = _child_text(elem, "title")
        pageid_text = _child_text(elem, "id")
        if title and pageid_text:
            index[title] = int(pageid_text)
        elem.clear()
    return index


def parse_page_element(elem, *, namespaces: dict[int, str], wiki_base_url: str | None) -> dict:
    """Pure: one <page> Element -> a dict shaped like fetch.py's API
    backend's intermediate page data (categories/content_sha256/
    retrieved_at/source_query are filled in by the caller, not here, since
    those require wikitext analysis or acquisition-time context this
    function doesn't have)."""
    title = _child_text(elem, "title") or ""
    ns_id = int(_child_text(elem, "ns") or "0")
    pageid_text = _child_text(elem, "id")
    pageid = int(pageid_text) if pageid_text else None
    redirect_elem = _child(elem, "redirect")
    is_redirect = redirect_elem is not None
    redirect_target_title = redirect_elem.get("title") if redirect_elem is not None else None

    revision = {"revid": None, "parentid": None, "timestamp": None, "contentmodel": None, "contentformat": None}
    wikitext = ""
    revision_elem = _child(elem, "revision")
    if revision_elem is not None:
        revid_text = _child_text(revision_elem, "id")
        parentid_text = _child_text(revision_elem, "parentid")
        revision = {
            "revid": int(revid_text) if revid_text else None,
            "parentid": int(parentid_text) if parentid_text else None,
            "timestamp": _child_text(revision_elem, "timestamp"),
            "contentmodel": _child_text(revision_elem, "model"),
            "contentformat": _child_text(revision_elem, "format"),
        }
        text_elem = _child(revision_elem, "text")
        wikitext = (text_elem.text or "") if text_elem is not None else ""

    namespace_name = namespaces.get(ns_id, str(ns_id))
    canonical_url = f"{wiki_base_url}/wiki/{title.replace(' ', '_')}" if wiki_base_url else None

    return {
        "pageid": pageid,
        "title": title,
        "namespace_id": ns_id,
        "namespace_name": namespace_name,
        "canonical_url": canonical_url,
        "is_redirect": is_redirect,
        "redirect_target_title": redirect_target_title,
        "wikitext": wikitext,
        "revision": revision,
    }


def iter_dump_pages(xml_path: Path, *, namespaces: dict[int, str], wiki_base_url: str | None) -> Iterator[dict]:
    """Stream the dump's <page> elements one at a time -- never loads the
    whole (potentially very large) file into memory."""
    for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
        if _local(elem.tag) != "page":
            continue
        yield parse_page_element(elem, namespaces=namespaces, wiki_base_url=wiki_base_url)
        elem.clear()
