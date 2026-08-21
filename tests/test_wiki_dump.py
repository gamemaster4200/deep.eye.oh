"""Exercises _dump.py's dump-URL derivation, freshness check, HEAD probing,
and MediaWiki XML export parsing -- all offline against a small fixture
dump and fake HTTP transports, no real sockets or network."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import py7zr
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _dump
import _http
import _mediawiki

SAMPLE_DUMP = Path(__file__).resolve().parent / "fixtures" / "wiki" / "dump_samples" / "sample_dump.xml"


def test_derive_dump_url_pattern():
    url = _dump.derive_dump_url("diepio")
    assert url == "https://s3.amazonaws.com/wikia_xml_dumps/d/di/diepio_pages_current.xml.7z"
    assert _dump.derive_dump_url("diepio", variant="full").endswith("diepio_pages_full.xml.7z")


def test_is_fresh_enough_within_window():
    assert _dump.is_fresh_enough("Fri, 12 Jun 2026 14:18:41 GMT", max_age_days=99999)  # far future window, always true if parseable
    assert not _dump.is_fresh_enough("Fri, 12 Jun 2020 14:18:41 GMT", max_age_days=1)  # old date, tiny window


def test_is_fresh_enough_missing_or_unparseable_is_not_fresh():
    assert not _dump.is_fresh_enough(None, max_age_days=99999)
    assert not _dump.is_fresh_enough("not a date", max_age_days=99999)


def test_probe_dump_returns_metadata_on_200():
    def fake_head_opener(request, timeout):
        return _http.HeadResult(200, {"Last-Modified": "Fri, 12 Jun 2026 14:18:41 GMT", "Content-Length": "123", "ETag": '"abc"'})

    probe = _dump.probe_dump("https://s3.amazonaws.com/x.7z", user_agent="ua/1", timeout=5.0, opener=fake_head_opener)
    assert probe["http_status"] == 200
    assert probe["last_modified"] == "Fri, 12 Jun 2026 14:18:41 GMT"
    assert probe["content_length"] == "123"


def test_probe_dump_returns_none_on_404():
    def fake_head_opener(request, timeout):
        import urllib.error

        raise urllib.error.HTTPError("https://s3.amazonaws.com/x.7z", 404, "Not Found", {}, None)

    assert _dump.probe_dump("https://s3.amazonaws.com/x.7z", user_agent="ua/1", timeout=5.0, opener=fake_head_opener) is None


def test_dump_dbname_key_reads_wikiid_from_siteinfo(monkeypatch):
    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: {"query": {"general": {"wikiid": "diepio"}}})
    assert _dump.dump_dbname_key("https://diepio.fandom.com") == "diepio"


def test_dump_dbname_key_raises_when_missing(monkeypatch):
    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: {"query": {"general": {}}})
    with pytest.raises(_http.PermanentAcquisitionError):
        _dump.dump_dbname_key("https://diepio.fandom.com")


def test_parse_dump_namespaces():
    namespaces = _dump.parse_dump_namespaces(SAMPLE_DUMP)
    assert namespaces[0] == "(Main)"
    assert namespaces[14] == "Category"
    assert namespaces[1201] == "Thread"
    assert namespaces[-1] == "Special"


def test_parse_dump_base_url():
    assert _dump.parse_dump_base_url(SAMPLE_DUMP) == "https://example.fandom.com"


def test_build_title_to_pageid_index_covers_every_namespace():
    index = _dump.build_title_to_pageid_index(SAMPLE_DUMP)
    assert index == {"Basic": 201, "Overlord Redirect": 203, "Overlord": 204, "Thread:12345": 900}


def test_iter_dump_pages_shapes_match_api_backend_fields():
    namespaces = _dump.parse_dump_namespaces(SAMPLE_DUMP)
    base_url = _dump.parse_dump_base_url(SAMPLE_DUMP)
    pages = list(_dump.iter_dump_pages(SAMPLE_DUMP, namespaces=namespaces, wiki_base_url=base_url))
    by_title = {p["title"]: p for p in pages}

    basic = by_title["Basic"]
    assert basic["pageid"] == 201
    assert basic["namespace_id"] == 0
    assert basic["namespace_name"] == "(Main)"
    assert basic["is_redirect"] is False
    assert basic["redirect_target_title"] is None
    assert basic["canonical_url"] == "https://example.fandom.com/wiki/Basic"
    assert "{{Infobox tank" in basic["wikitext"]
    assert basic["revision"]["revid"] == 5001
    assert basic["revision"]["contentmodel"] == "wikitext"
    assert basic["revision"]["contentformat"] == "text/x-wiki"
    # data minimization: no contributor/comment fields anywhere in the record
    assert "contributor" not in basic and "comment" not in basic

    redirect_page = by_title["Overlord Redirect"]
    assert redirect_page["is_redirect"] is True
    assert redirect_page["redirect_target_title"] == "Overlord"
    assert redirect_page["wikitext"] == "#REDIRECT [[Overlord]]"

    thread_page = by_title["Thread:12345"]
    assert thread_page["namespace_name"] == "Thread"


def test_download_and_extract_uses_fetch_bytes_and_returns_xml_and_hash(tmp_path):
    # build a real, tiny .7z archive from the sample dump so extraction is
    # exercised for real (py7zr), while the network layer is still faked.
    archive_src = tmp_path / "src" / "diepio_pages_current.xml.7z"
    archive_src.parent.mkdir()
    with py7zr.SevenZipFile(archive_src, mode="w") as archive:
        archive.write(SAMPLE_DUMP, arcname="diepio_pages_current.xml")
    archive_bytes = archive_src.read_bytes()

    def fake_opener(request, timeout):
        return archive_bytes

    archive_out = tmp_path / "snapshot" / "dump_archive" / "diepio_pages_current.xml.7z"
    extract_dir = tmp_path / "extract"

    xml_path, archive_sha256 = _dump.download_and_extract(
        "https://s3.amazonaws.com/wikia_xml_dumps/d/di/diepio_pages_current.xml.7z",
        archive_out_path=archive_out,
        extract_dir=extract_dir,
        user_agent="ua/1",
        timeout=5.0,
        opener=fake_opener,
    )

    assert archive_out.exists()
    assert xml_path.exists()
    assert xml_path.read_text(encoding="utf-8").startswith("<mediawiki")
    import hashlib

    assert archive_sha256 == hashlib.sha256(archive_bytes).hexdigest()
