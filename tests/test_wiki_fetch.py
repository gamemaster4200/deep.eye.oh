"""Exercises fetch.py's raw record construction, content hashing, redirect
handling, permanent-failure recording, write-once refusal, and
resume/skip-successful behavior -- all offline against fixture responses."""

import json
import sys
import urllib.robotparser
from pathlib import Path

import py7zr
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _dump
import _http
import _mediawiki
import fetch

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wiki" / "api_responses"
SAMPLE_DUMP = Path(__file__).resolve().parent / "fixtures" / "wiki" / "dump_samples" / "sample_dump.xml"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _sample_pages_index():
    return {
        "format_version": 1,
        "wiki_base_url": "https://example.fandom.com",
        "generated_at": "2026-01-01T00:00:00Z",
        "namespaces": {"0": "(Main)"},
        "pages": [
            {"pageid": 101, "title": "Basic", "namespace_id": 0, "namespace_name": "(Main)", "canonical_url": None, "is_redirect": False, "categories": [], "redirect_target_title": None, "redirect_target_pageid": None},
            {"pageid": 102, "title": "Destroyer", "namespace_id": 0, "namespace_name": "(Main)", "canonical_url": None, "is_redirect": False, "categories": [], "redirect_target_title": None, "redirect_target_pageid": None},
        ],
    }


def test_build_raw_page_record_has_expected_fields_and_no_author_or_comment():
    response = _load("revisions_batch.json")
    api_page = _mediawiki.parse_pages(response)[0]
    index_entry = _sample_pages_index()["pages"][0]
    record = fetch.build_raw_page_record(api_page, index_entry, retrieved_at="2026-01-01T00:00:00Z", base_url="https://example.fandom.com", request_params={"pageids": "101"})

    assert record["pageid"] == 101
    assert record["title"] == "Basic"
    assert record["wikitext"].startswith("{{Infobox tank")
    assert record["content_sha256"] == fetch.sha256_of_text(record["wikitext"])
    assert record["revision"]["revid"] == 5001
    assert "user" not in record["revision"]
    assert "comment" not in record["revision"]
    assert record["is_redirect"] is False


def test_build_raw_page_record_redirect_fields():
    response = _load("revisions_redirect.json")
    api_page = _mediawiki.parse_pages(response)[0]
    index_entry = {
        "pageid": 103, "namespace_id": 0, "namespace_name": "(Main)", "canonical_url": None,
        "is_redirect": True, "redirect_target_title": "Overlord", "redirect_target_pageid": 104,
    }
    record = fetch.build_raw_page_record(api_page, index_entry, retrieved_at="2026-01-01T00:00:00Z", base_url="https://example.fandom.com", request_params={})
    assert record["is_redirect"] is True
    assert record["redirect_target_title"] == "Overlord"
    assert record["redirect_target_pageid"] == 104
    assert record["wikitext"] == "#REDIRECT [[Overlord]]"


def test_fetch_snapshot_writes_pages_and_success_records(tmp_path, monkeypatch):
    pages_index = _sample_pages_index()
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    manifest = fetch.init_manifest(snapshot_id="snap", wiki_base_url=pages_index["wiki_base_url"], discover_source="fixture", user_agent="ua/1", http_settings={})
    fetch.write_manifest(snapshot_dir, manifest)

    response = _load("revisions_batch.json")
    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: response)

    manifest = fetch.fetch_snapshot(pages_index, snapshot_dir, manifest, skip_namespaces=set(), retry_permanent_failures=False)

    assert manifest["succeeded_page_count"] == 2
    assert manifest["failed_page_count"] == 0
    assert (snapshot_dir / "pages" / "101.json").exists()
    assert (snapshot_dir / "pages" / "102.json").exists()

    records = fetch.read_acquisition_records(snapshot_dir)
    outcomes = {r["pageid"]: r["outcome"] for r in records}
    assert outcomes == {101: "success", 102: "success"}


def test_fetch_snapshot_records_permanent_failure_for_missing_page(tmp_path, monkeypatch):
    pages_index = {
        "wiki_base_url": "https://example.fandom.com",
        "pages": [{"pageid": 999, "title": "Deleted Page", "namespace_id": 0, "namespace_name": "(Main)", "canonical_url": None, "is_redirect": False, "categories": [], "redirect_target_title": None, "redirect_target_pageid": None}],
    }
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    manifest = fetch.init_manifest(snapshot_id="snap", wiki_base_url=pages_index["wiki_base_url"], discover_source="fixture", user_agent="ua/1", http_settings={})
    fetch.write_manifest(snapshot_dir, manifest)

    response = _load("revisions_missing_page.json")
    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: response)

    manifest = fetch.fetch_snapshot(pages_index, snapshot_dir, manifest, skip_namespaces=set(), retry_permanent_failures=False)

    assert manifest["succeeded_page_count"] == 0
    assert manifest["failed_page_count"] == 1
    records = fetch.read_acquisition_records(snapshot_dir)
    assert records[0]["outcome"] == "permanent_failure"
    assert records[0]["reason"] == "missingtitle"
    assert not (snapshot_dir / "pages").exists() or not any((snapshot_dir / "pages").iterdir())


def test_fetch_snapshot_skips_namespace_in_skip_list(tmp_path, monkeypatch):
    pages_index = _sample_pages_index()
    pages_index["pages"][1]["namespace_name"] = "User talk"
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    manifest = fetch.init_manifest(snapshot_id="snap", wiki_base_url=pages_index["wiki_base_url"], discover_source="fixture", user_agent="ua/1", http_settings={})
    fetch.write_manifest(snapshot_dir, manifest)

    response = {"query": {"pages": [_mediawiki.parse_pages(_load("revisions_batch.json"))[0]]}}
    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: response)

    manifest = fetch.fetch_snapshot(pages_index, snapshot_dir, manifest, skip_namespaces={"User talk"}, retry_permanent_failures=False)

    assert manifest["succeeded_page_count"] == 1
    assert (snapshot_dir / "pages" / "101.json").exists()
    assert not (snapshot_dir / "pages" / "102.json").exists()


def test_write_page_record_refuses_to_overwrite(tmp_path):
    snapshot_dir = tmp_path / "snap"
    record = {"pageid": 101, "title": "Basic"}
    fetch.write_page_record(snapshot_dir, record)
    with pytest.raises(SystemExit):
        fetch.write_page_record(snapshot_dir, record)


def test_resume_skips_already_succeeded_pages(tmp_path, monkeypatch):
    pages_index = _sample_pages_index()
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    manifest = fetch.init_manifest(snapshot_id="snap", wiki_base_url=pages_index["wiki_base_url"], discover_source="fixture", user_agent="ua/1", http_settings={})
    fetch.write_manifest(snapshot_dir, manifest)

    # pre-seed page 101 as already successfully fetched
    fetch.write_page_record(snapshot_dir, {"pageid": 101, "title": "Basic", "wikitext": "already here"})

    call_count = {"n": 0}

    def fake_fetch_json(url, **kwargs):
        call_count["n"] += 1
        api_page = _mediawiki.parse_pages(_load("revisions_batch.json"))[1]  # only Destroyer (102)
        return {"query": {"pages": [api_page]}}

    monkeypatch.setattr(_mediawiki._http, "fetch_json", fake_fetch_json)

    manifest = fetch.fetch_snapshot(pages_index, snapshot_dir, manifest, skip_namespaces=set(), retry_permanent_failures=False)

    assert manifest["succeeded_page_count"] == 2  # 101 (pre-seeded) + 102 (fetched now)
    assert call_count["n"] == 1  # only pageid 102 was actually requested
    # 101's pre-seeded content must be untouched
    assert json.loads((snapshot_dir / "pages" / "101.json").read_text())["wikitext"] == "already here"


def test_new_snapshot_isolated_from_existing(tmp_path):
    out_root = tmp_path / "fandom"
    id1 = fetch.new_snapshot_id()
    dir1 = fetch.snapshot_dir_for(out_root, id1)
    dir1.mkdir(parents=True)
    (dir1 / "marker.txt").write_text("first snapshot")

    # a distinct id (simulate a later run) must not collide with dir1
    id2 = "different-id"
    dir2 = fetch.snapshot_dir_for(out_root, id2)
    assert dir2 != dir1
    assert not dir2.exists()


def _make_sample_archive(tmp_path) -> bytes:
    archive_path = tmp_path / "diepio_pages_current.xml.7z"
    with py7zr.SevenZipFile(archive_path, mode="w") as archive:
        archive.write(SAMPLE_DUMP, arcname="diepio_pages_current.xml")
    return archive_path.read_bytes()


def test_run_dump_backend_writes_records_matching_api_shape(tmp_path, monkeypatch):
    archive_bytes = _make_sample_archive(tmp_path)

    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: {"query": {"general": {"wikiid": "diepio"}}})
    allow_all = urllib.robotparser.RobotFileParser()
    allow_all.parse(["User-agent: *", "Allow: /"])
    monkeypatch.setattr(
        fetch._http,
        "check_robots",
        lambda base_url, **kwargs: {"url": base_url + "/robots.txt", "outcome": "obeyed", "http_status": 200, "reason": None, "checked_at": "t", "parser": allow_all},
    )
    monkeypatch.setattr(
        _dump._http,
        "fetch_head",
        lambda url, **kwargs: _http.HeadResult(200, {"Last-Modified": "Fri, 12 Jun 2026 14:18:41 GMT"}),
    )
    monkeypatch.setattr(_dump._http, "fetch_bytes", lambda url, **kwargs: archive_bytes)

    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    manifest = fetch.init_manifest(snapshot_id="snap", wiki_base_url="https://diepio.fandom.com", discover_source=None, user_agent="ua/1", http_settings={})

    used = fetch.run_dump_backend(
        "https://diepio.fandom.com",
        snapshot_dir,
        manifest,
        skip_namespaces={"Thread"},
        dump_variant="current",
        dump_max_age_days=180,
        retry_permanent_failures=False,
        user_agent="ua/1",
        timeout=5.0,
    )

    assert used is True
    assert manifest["source"]["backend"] == "dump"
    assert manifest["source"]["usable"] is True
    assert manifest["source"]["wikiid"] == "diepio"
    assert manifest["source"]["archive_sha256"]

    # Basic/Overlord Redirect/Overlord kept; Thread(namespace-skipped) excluded
    succeeded = fetch.already_succeeded_pageids(snapshot_dir)
    assert succeeded == {201, 203, 204}

    basic = json.loads((snapshot_dir / "pages" / "201.json").read_text(encoding="utf-8"))
    assert basic["categories"] == ["Category:Tanks"]
    assert basic["content_sha256"] == fetch.sha256_of_text(basic["wikitext"])
    assert "contributor" not in basic and "contributor" not in basic["revision"]
    assert set(basic["revision"].keys()) == {"revid", "parentid", "timestamp", "contentmodel", "contentformat"}

    redirect_page = json.loads((snapshot_dir / "pages" / "203.json").read_text(encoding="utf-8"))
    assert redirect_page["is_redirect"] is True
    assert redirect_page["redirect_target_title"] == "Overlord"
    assert redirect_page["redirect_target_pageid"] == 204  # resolved via the dump's own title index

    assert (snapshot_dir / "dump_archive" / "diepio_pages_current.xml.7z").exists()


def test_run_dump_backend_returns_false_and_records_reason_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(_mediawiki._http, "fetch_json", lambda url, **kwargs: {"query": {"general": {"wikiid": "diepio"}}})
    allow_all = urllib.robotparser.RobotFileParser()
    allow_all.parse(["User-agent: *", "Allow: /"])
    monkeypatch.setattr(
        fetch._http,
        "check_robots",
        lambda base_url, **kwargs: {"url": base_url + "/robots.txt", "outcome": "obeyed", "http_status": 200, "reason": None, "checked_at": "t", "parser": allow_all},
    )
    monkeypatch.setattr(
        _dump._http,
        "fetch_head",
        lambda url, **kwargs: _http.HeadResult(200, {"Last-Modified": "Fri, 12 Jun 2020 14:18:41 GMT"}),  # very old
    )

    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()
    manifest = fetch.init_manifest(snapshot_id="snap", wiki_base_url="https://diepio.fandom.com", discover_source=None, user_agent="ua/1", http_settings={})

    used = fetch.run_dump_backend(
        "https://diepio.fandom.com",
        snapshot_dir,
        manifest,
        skip_namespaces=set(),
        dump_variant="current",
        dump_max_age_days=180,
        retry_permanent_failures=False,
        user_agent="ua/1",
        timeout=5.0,
    )

    assert used is False
    assert manifest["source"]["usable"] is False
    assert manifest["source"]["stage"] == "freshness"
    assert fetch.already_succeeded_pageids(snapshot_dir) == set()  # nothing fabricated
