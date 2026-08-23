"""Acquire raw page content into a snapshot directory under
knowledge/raw/fandom/<snapshot-id>/. Two raw-source backends, both
producing the identical per-page record shape so inventory.py never needs
to know which one was used:

- dump (preferred, --backend auto/dump): the official Fandom database dump
  (see _dump.py) -- one bulk artifact instead of thousands of live calls.
- api (fallback, --backend auto/api): the page-by-page MediaWiki API,
  driven by a pages_index.json from discover.py, with polite rate limiting.

--backend auto (default) prefers the dump, falling back to the API only if
no sufficiently fresh dump is available (see --dump-max-age-days) -- this
requires --pages-index to already exist for the fallback to work.

Every attempted page gets an outcome recorded in acquisition.jsonl, success
or failure; raw page records under pages/ are write-once and never mutated
by a later run.

Resumability: --snapshot-id resumes into an existing snapshot, skipping
pageids that already have a successful raw record and never silently
retrying previously-recorded permanent failures (pass
--retry-permanent-failures to opt into that explicitly). --new-snapshot
always starts a fresh, separate snapshot directory. An old snapshot's
successful pages are never mutated in place.

Development tooling only — not part of the production pipeline.

    python tools/wiki/fetch.py --new-snapshot --user-agent "deep-eye-oh-wiki-inventory/0.1 (+research tool; see project repo)"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _dump
import _http
import _mediawiki
import _wikitext

DEFAULT_SKIP_NAMESPACES = "User talk,Message Wall,Message Wall Greeting,Board,Board Thread,Thread,File,File talk"
DEFAULT_DUMP_MAX_AGE_DAYS = 180


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_snapshot_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot_dir_for(out_root: Path, snapshot_id: str) -> Path:
    return out_root / snapshot_id


def load_pages_index(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def init_manifest(*, snapshot_id: str, wiki_base_url: str, discover_source: str, user_agent: str, http_settings: dict) -> dict:
    return {
        "format_version": 1,
        "snapshot_id": snapshot_id,
        "wiki_base_url": wiki_base_url,
        "started_at": _now_iso(),
        "completed_at": None,
        "discover_source": discover_source,
        "user_agent": user_agent,
        "http_settings": http_settings,
        "attempted_page_count": 0,
        "succeeded_page_count": 0,
        "failed_page_count": 0,
    }


def load_manifest(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))


def write_manifest(snapshot_dir: Path, manifest: dict) -> None:
    with (snapshot_dir / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def already_succeeded_pageids(snapshot_dir: Path) -> set[int]:
    pages_dir = snapshot_dir / "pages"
    if not pages_dir.exists():
        return set()
    return {int(p.stem) for p in pages_dir.glob("*.json")}


def read_acquisition_records(snapshot_dir: Path) -> list[dict]:
    path = snapshot_dir / "acquisition.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def append_acquisition_record(snapshot_dir: Path, record: dict) -> None:
    with (snapshot_dir / "acquisition.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def permanently_failed_pageids(snapshot_dir: Path) -> set[int]:
    succeeded = already_succeeded_pageids(snapshot_dir)
    failed = {r["pageid"] for r in read_acquisition_records(snapshot_dir) if r.get("outcome") == "permanent_failure" and r.get("pageid") is not None}
    return failed - succeeded


def failed_pageids(snapshot_dir: Path) -> set[int]:
    succeeded = already_succeeded_pageids(snapshot_dir)
    failed = {
        r["pageid"]
        for r in read_acquisition_records(snapshot_dir)
        if r.get("outcome") in ("permanent_failure", "transient_failure") and r.get("pageid") is not None
    }
    return failed - succeeded


def select_pages_to_fetch(
    pages_index: dict, *, skip_namespaces: set[str], already_done: set[int], previously_failed: set[int], retry_permanent_failures: bool
) -> list[dict]:
    selected = []
    for page in pages_index["pages"]:
        if page["namespace_name"] in skip_namespaces:
            continue
        pageid = page["pageid"]
        if pageid in already_done:
            continue
        if pageid in previously_failed and not retry_permanent_failures:
            continue
        selected.append(page)
    return selected


def build_raw_page_record(api_page: dict, index_entry: dict, *, retrieved_at: str, base_url: str, request_params: dict) -> dict:
    revisions = api_page.get("revisions", [])
    revision = revisions[0] if revisions else {}
    slot = revision.get("slots", {}).get("main", {})
    wikitext = slot.get("content", "")
    categories = [c["title"] for c in api_page.get("categories", [])]
    is_redirect = bool(api_page.get("redirect", index_entry.get("is_redirect", False)))
    params_digest = hashlib.sha256(json.dumps(request_params, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "format_version": 1,
        "pageid": api_page["pageid"],
        "title": api_page["title"],
        "namespace_id": index_entry["namespace_id"],
        "namespace_name": index_entry["namespace_name"],
        "canonical_url": api_page.get("fullurl", index_entry.get("canonical_url")),
        "is_redirect": is_redirect,
        "redirect_target_title": index_entry.get("redirect_target_title"),
        "redirect_target_pageid": index_entry.get("redirect_target_pageid"),
        "categories": categories,
        "revision": {
            "revid": revision.get("revid"),
            "parentid": revision.get("parentid"),
            "timestamp": revision.get("timestamp"),
            "contentmodel": slot.get("contentmodel"),
            "contentformat": slot.get("contentformat"),
        },
        "wikitext": wikitext,
        "content_sha256": sha256_of_text(wikitext),
        "retrieved_at": retrieved_at,
        "source_query": {"endpoint": f"{base_url.rstrip('/')}/api.php", "params_digest": params_digest},
    }


def write_page_record(snapshot_dir: Path, record: dict) -> None:
    pages_dir = snapshot_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    path = pages_dir / f"{record['pageid']}.json"
    if path.exists():
        raise SystemExit(
            f"refusing to overwrite existing raw page record {path} -- raw snapshots are write-once; "
            "use --new-snapshot for fresh data"
        )
    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.write("\n")


def _check_robots_or_fail_closed(base_url: str, *, user_agent: str, timeout: float, snapshot_dir: Path) -> dict:
    """See discover.py's twin of this function (RFC 9309 semantics): a 4xx
    robots.txt response is "unavailable" and acquisition proceeds; only a
    genuinely unreachable (5xx/network) robots.txt fails closed, recorded
    durably into this snapshot's acquisition.jsonl rather than bypassed."""
    try:
        return _http.check_robots(base_url, user_agent=user_agent, timeout=timeout)
    except _http.TransientAcquisitionError as exc:
        append_acquisition_record(
            snapshot_dir,
            {
                "pageid": None,
                "title": None,
                "url": base_url.rstrip("/") + "/robots.txt",
                "attempt": 1,
                "outcome": "permanent_failure",
                "http_status": getattr(exc, "http_status", None),
                "reason": f"robots.txt unreachable (failing closed per RFC 9309): {exc}",
                "timestamp": _now_iso(),
            },
        )
        raise SystemExit(
            f"could not fetch robots.txt from {base_url} ({exc}) -- failing closed per RFC 9309 (5xx/unreachable "
            f"is not \"unavailable\"); recorded this failed attempt in {snapshot_dir / 'acquisition.jsonl'}"
        ) from exc


def run_dump_backend(
    base_url: str,
    snapshot_dir: Path,
    manifest: dict,
    *,
    skip_namespaces: set[str],
    dump_variant: str,
    dump_max_age_days: int,
    retry_permanent_failures: bool,
    now_fn=_now_iso,
    **fetch_kwargs,
) -> bool:
    """Attempt the dump backend. Always sets `manifest["source"]` to a
    record of what was tried and why it did/didn't work (provenance,
    regardless of outcome). Returns True if a usable dump was found and
    page records were written from it (caller should not also run the API
    backend); False if the dump wasn't usable (caller should fall back to
    the API backend, if one is available) -- never raises for an unusable
    dump, since that's an expected, recorded outcome, not an error."""
    attempt: dict = {"backend": "dump", "usable": False}
    manifest["source"] = attempt

    try:
        wikiid = _dump.dump_dbname_key(base_url, **fetch_kwargs)
    except (_http.PermanentAcquisitionError, _http.TransientAcquisitionError) as exc:
        attempt["stage"] = "wikiid_lookup"
        attempt["reason"] = str(exc)
        return False
    attempt["wikiid"] = wikiid

    dump_url = _dump.derive_dump_url(wikiid, variant=dump_variant)
    attempt["dump_url"] = dump_url

    s3_base_url = "https://s3.amazonaws.com"
    try:
        s3_robots = _http.check_robots(s3_base_url, user_agent=fetch_kwargs["user_agent"], timeout=fetch_kwargs["timeout"])
    except _http.TransientAcquisitionError as exc:
        attempt["stage"] = "s3_robots_check"
        attempt["reason"] = str(exc)
        return False
    attempt["s3_robots_txt_status"] = {k: v for k, v in s3_robots.items() if k != "parser"}

    probe = _dump.probe_dump(dump_url, user_agent=fetch_kwargs["user_agent"], timeout=fetch_kwargs["timeout"])
    if probe is None:
        attempt["stage"] = "probe"
        attempt["reason"] = "HEAD request for the dump archive failed or returned a non-200 status"
        return False
    attempt["probe"] = probe

    if not _dump.is_fresh_enough(probe["last_modified"], max_age_days=dump_max_age_days):
        attempt["stage"] = "freshness"
        attempt["reason"] = (
            f"dump Last-Modified={probe['last_modified']!r} is missing/unparseable or older than "
            f"{dump_max_age_days} day(s) -- treated as stale enough to make the inventory materially misleading"
        )
        attempt["max_age_days"] = dump_max_age_days
        return False

    archive_path = snapshot_dir / "dump_archive" / Path(dump_url).name
    with tempfile.TemporaryDirectory(prefix="wiki_dump_") as extract_dir:
        dump_fetch_kwargs = dict(fetch_kwargs)
        dump_fetch_kwargs["robot_parser"] = s3_robots["parser"]
        try:
            xml_path, archive_sha256 = _dump.download_and_extract(
                dump_url, archive_out_path=archive_path, extract_dir=Path(extract_dir), **dump_fetch_kwargs
            )
        except (_http.PermanentAcquisitionError, _http.TransientAcquisitionError) as exc:
            attempt["stage"] = "download"
            attempt["reason"] = str(exc)
            return False

        namespaces = _dump.parse_dump_namespaces(xml_path)
        wiki_site_root = _dump.parse_dump_base_url(xml_path) or base_url
        title_index = _dump.build_title_to_pageid_index(xml_path)

        already_done = already_succeeded_pageids(snapshot_dir)
        previously_failed = permanently_failed_pageids(snapshot_dir)

        for page in _dump.iter_dump_pages(xml_path, namespaces=namespaces, wiki_base_url=wiki_site_root):
            pageid = page["pageid"]
            if pageid is None or page["namespace_name"] in skip_namespaces:
                continue
            if pageid in already_done or (pageid in previously_failed and not retry_permanent_failures):
                continue

            wikitext = page["wikitext"]
            redirect_target_title = page["redirect_target_title"]
            record = {
                "format_version": 1,
                "pageid": pageid,
                "title": page["title"],
                "namespace_id": page["namespace_id"],
                "namespace_name": page["namespace_name"],
                "canonical_url": page["canonical_url"],
                "is_redirect": page["is_redirect"],
                "redirect_target_title": redirect_target_title,
                "redirect_target_pageid": title_index.get(redirect_target_title) if redirect_target_title else None,
                "categories": _wikitext.extract_category_links(wikitext),
                "revision": page["revision"],
                "wikitext": wikitext,
                "content_sha256": sha256_of_text(wikitext),
                "retrieved_at": now_fn(),
                "source_query": {"endpoint": dump_url, "params_digest": archive_sha256},
            }
            write_page_record(snapshot_dir, record)
            append_acquisition_record(
                snapshot_dir,
                {"pageid": pageid, "title": page["title"], "url": dump_url, "attempt": 1, "outcome": "success", "http_status": 200, "timestamp": now_fn()},
            )

    attempt["usable"] = True
    attempt["dump_last_modified"] = probe["last_modified"]
    attempt["archive_sha256"] = archive_sha256
    attempt["dump_type"] = f"mediawiki-xml-export-pages-{dump_variant}"
    attempt["retrieved_at"] = now_fn()
    return True


def fetch_snapshot(
    pages_index: dict,
    snapshot_dir: Path,
    manifest: dict,
    *,
    skip_namespaces: set[str],
    retry_permanent_failures: bool,
    now_fn=_now_iso,
    **fetch_kwargs,
) -> dict:
    """Run one fetch pass into `snapshot_dir`, writing raw page records and
    acquisition.jsonl entries, and return `manifest` updated with final
    cumulative counts (across this and any prior run into the same
    snapshot). `fetch_kwargs` are forwarded to _mediawiki.query."""
    already_done = already_succeeded_pageids(snapshot_dir)
    previously_failed = permanently_failed_pageids(snapshot_dir)
    to_fetch = select_pages_to_fetch(
        pages_index,
        skip_namespaces=skip_namespaces,
        already_done=already_done,
        previously_failed=previously_failed,
        retry_permanent_failures=retry_permanent_failures,
    )
    index_by_pageid = {p["pageid"]: p for p in pages_index["pages"]}
    base_url = pages_index["wiki_base_url"]

    for batch in _mediawiki.chunked([p["pageid"] for p in to_fetch]):
        request_params = _mediawiki.revisions_params(batch)
        url = _mediawiki.build_api_url(base_url, {"action": "query", **request_params})
        try:
            response = _mediawiki.query(base_url, request_params, **fetch_kwargs)
        except _http.PermanentAcquisitionError as exc:
            for pageid in batch:
                append_acquisition_record(
                    snapshot_dir,
                    {
                        "pageid": pageid,
                        "title": index_by_pageid[pageid]["title"],
                        "url": url,
                        "attempt": 1,
                        "outcome": "permanent_failure",
                        "http_status": exc.http_status,
                        "reason": str(exc),
                        "timestamp": now_fn(),
                    },
                )
            continue
        except _http.TransientAcquisitionError as exc:
            for pageid in batch:
                append_acquisition_record(
                    snapshot_dir,
                    {
                        "pageid": pageid,
                        "title": index_by_pageid[pageid]["title"],
                        "url": url,
                        "attempt": 1,
                        "outcome": "transient_failure",
                        "http_status": exc.http_status,
                        "reason": str(exc),
                        "timestamp": now_fn(),
                    },
                )
            continue

        seen_pageids: set[int] = set()
        for api_page in _mediawiki.parse_pages(response):
            pageid = api_page.get("pageid")
            index_entry = index_by_pageid.get(pageid)
            if index_entry is None:
                continue
            seen_pageids.add(pageid)
            if api_page.get("missing"):
                append_acquisition_record(
                    snapshot_dir,
                    {
                        "pageid": pageid,
                        "title": index_entry["title"],
                        "url": url,
                        "attempt": 1,
                        "outcome": "permanent_failure",
                        "http_status": None,
                        "reason": "missingtitle",
                        "timestamp": now_fn(),
                    },
                )
                continue
            record = build_raw_page_record(api_page, index_entry, retrieved_at=now_fn(), base_url=base_url, request_params=request_params)
            write_page_record(snapshot_dir, record)
            append_acquisition_record(
                snapshot_dir,
                {"pageid": pageid, "title": index_entry["title"], "url": url, "attempt": 1, "outcome": "success", "http_status": 200, "timestamp": now_fn()},
            )

        for pageid in batch:
            if pageid not in seen_pageids:
                append_acquisition_record(
                    snapshot_dir,
                    {
                        "pageid": pageid,
                        "title": index_by_pageid[pageid]["title"],
                        "url": url,
                        "attempt": 1,
                        "outcome": "permanent_failure",
                        "http_status": None,
                        "reason": "not_returned_by_api",
                        "timestamp": now_fn(),
                    },
                )

    manifest["succeeded_page_count"] = len(already_succeeded_pageids(snapshot_dir))
    manifest["failed_page_count"] = len(failed_pageids(snapshot_dir))
    manifest["attempted_page_count"] = manifest["succeeded_page_count"] + manifest["failed_page_count"]
    manifest["completed_at"] = now_fn()
    write_manifest(snapshot_dir, manifest)
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://diepio.fandom.com", help="wiki base URL (used to derive the dump URL and, for the API backend, must match --pages-index)")
    parser.add_argument("--pages-index", default=None, help="path to pages_index.json produced by discover.py (required only if the API backend ends up being used)")
    parser.add_argument("--backend", choices=("auto", "dump", "api"), default="auto", help="auto (default): prefer the official database dump, fall back to the API; dump/api: force one backend")
    parser.add_argument("--dump-variant", default=_dump.DEFAULT_DUMP_VARIANT, choices=("current", "full"), help="dump variant (current = latest revision only, no history)")
    parser.add_argument(
        "--dump-max-age-days",
        type=int,
        default=DEFAULT_DUMP_MAX_AGE_DAYS,
        help=f"treat the dump as too stale to use (fall back to API) if its Last-Modified is older than this (default: {DEFAULT_DUMP_MAX_AGE_DAYS})",
    )
    parser.add_argument("--out-root", default="knowledge/raw/fandom", help="root directory for snapshot directories")
    snapshot_group = parser.add_mutually_exclusive_group(required=True)
    snapshot_group.add_argument("--new-snapshot", action="store_true", help="start a fresh snapshot with a new UTC-timestamp id")
    snapshot_group.add_argument("--snapshot-id", help="resume into an existing snapshot directory")
    parser.add_argument(
        "--skip-namespaces",
        default=DEFAULT_SKIP_NAMESPACES,
        help=f"comma-separated namespace names to discover/count but not fetch content for (default: {DEFAULT_SKIP_NAMESPACES})",
    )
    parser.add_argument(
        "--retry-permanent-failures",
        action="store_true",
        help="also retry pageids previously recorded as permanent failures in this snapshot (off by default)",
    )
    _http.add_http_args(parser)
    args = parser.parse_args(argv)

    pages_index = load_pages_index(Path(args.pages_index)) if args.pages_index else None
    wiki_base_url = pages_index["wiki_base_url"] if pages_index else args.base_url
    out_root = Path(args.out_root)

    if args.new_snapshot:
        snapshot_id = new_snapshot_id()
        snapshot_dir = snapshot_dir_for(out_root, snapshot_id)
        if snapshot_dir.exists():
            raise SystemExit(f"snapshot directory {snapshot_dir} already exists -- refusing to reuse a snapshot id")
        snapshot_dir.mkdir(parents=True)
        manifest = init_manifest(
            snapshot_id=snapshot_id,
            wiki_base_url=wiki_base_url,
            discover_source=str(args.pages_index) if args.pages_index else None,
            user_agent=args.user_agent,
            http_settings={"delay": args.delay, "timeout": args.timeout, "retries": args.retries},
        )
        write_manifest(snapshot_dir, manifest)
    else:
        snapshot_dir = snapshot_dir_for(out_root, args.snapshot_id)
        if not snapshot_dir.exists():
            raise SystemExit(f"snapshot directory {snapshot_dir} does not exist -- cannot resume; use --new-snapshot to start one")
        manifest = load_manifest(snapshot_dir)
        if manifest.get("wiki_base_url") != wiki_base_url:
            raise SystemExit("--base-url / pages_index wiki_base_url does not match this snapshot's recorded wiki_base_url -- refusing to mix sources")

    robots_status = _check_robots_or_fail_closed(wiki_base_url, user_agent=args.user_agent, timeout=args.timeout, snapshot_dir=snapshot_dir)
    manifest["robots_txt_status"] = {k: v for k, v in robots_status.items() if k != "parser"}
    rate_limiter = _http.RateLimiter(args.delay)
    fetch_kwargs = dict(
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        rate_limiter=rate_limiter,
        robot_parser=robots_status["parser"],
    )
    skip_namespaces = {ns.strip() for ns in args.skip_namespaces.split(",") if ns.strip()}

    backend_used = None
    if args.backend in ("auto", "dump"):
        used = run_dump_backend(
            wiki_base_url,
            snapshot_dir,
            manifest,
            skip_namespaces=skip_namespaces,
            dump_variant=args.dump_variant,
            dump_max_age_days=args.dump_max_age_days,
            retry_permanent_failures=args.retry_permanent_failures,
            **fetch_kwargs,
        )
        if used:
            backend_used = "dump"
        elif args.backend == "dump":
            write_manifest(snapshot_dir, manifest)
            raise SystemExit(f"--backend dump requested but no usable dump was found -- see {snapshot_dir / 'manifest.json'}'s \"source\" field for why")

    if backend_used is None:
        dump_attempt = manifest.get("source")  # preserved for provenance: why the dump wasn't used, if it was tried
        if pages_index is None:
            manifest["source"] = {"backend": "api", "dump_attempt": dump_attempt, "error": "no usable dump and no --pages-index given for API fallback"}
            write_manifest(snapshot_dir, manifest)
            raise SystemExit("no usable dump was found and no --pages-index was given for API fallback -- run discover.py first, or pass --pages-index")
        if pages_index["wiki_base_url"] != wiki_base_url:
            raise SystemExit("pages_index wiki_base_url does not match --base-url -- refusing to mix sources")
        manifest["source"] = {"backend": "api", "dump_attempt": dump_attempt}
        manifest = fetch_snapshot(
            pages_index,
            snapshot_dir,
            manifest,
            skip_namespaces=skip_namespaces,
            retry_permanent_failures=args.retry_permanent_failures,
            **fetch_kwargs,
        )
        backend_used = "api"
    else:
        manifest["succeeded_page_count"] = len(already_succeeded_pageids(snapshot_dir))
        manifest["failed_page_count"] = len(failed_pageids(snapshot_dir))
        manifest["attempted_page_count"] = manifest["succeeded_page_count"] + manifest["failed_page_count"]
        manifest["completed_at"] = _now_iso()
        write_manifest(snapshot_dir, manifest)

    print(f"backend: {backend_used}")
    print(f"snapshot {manifest['snapshot_id']}: {manifest['succeeded_page_count']} succeeded, {manifest['failed_page_count']} failed -> {snapshot_dir}")


if __name__ == "__main__":
    main()
