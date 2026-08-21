"""Given a pages_index.json (from discover.py), acquire raw page content
(wikitext + revision/category/redirect metadata) into a snapshot directory
under knowledge/raw/fandom/<snapshot-id>/. Every attempted page gets an
outcome recorded in acquisition.jsonl, success or failure; raw page records
under pages/ are write-once and never mutated by a later run.

Resumability: --snapshot-id resumes into an existing snapshot, skipping
pageids that already have a successful raw record and never silently
retrying previously-recorded permanent failures (pass
--retry-permanent-failures to opt into that explicitly). --new-snapshot
always starts a fresh, separate snapshot directory. An old snapshot's
successful pages are never mutated in place.

Development tooling only — not part of the production pipeline.

    python tools/wiki/fetch.py --pages-index knowledge/raw/fandom/pages_index.json --new-snapshot --user-agent "deep-eye-oh-wiki-inventory/0.1 (+research tool; see project repo)"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _http
import _mediawiki

DEFAULT_SKIP_NAMESPACES = "User talk,Message Wall,Board,Thread"


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


def _build_robot_parser_or_record_failure(base_url: str, *, user_agent: str, timeout: float, snapshot_dir: Path):
    """See discover.py's twin of this function: robots.txt being
    unreachable is a legitimate acquisition failure, recorded durably
    (into this snapshot's acquisition.jsonl) rather than bypassed."""
    try:
        return _http.build_robot_parser(base_url, user_agent=user_agent, timeout=timeout)
    except (_http.PermanentAcquisitionError, _http.TransientAcquisitionError) as exc:
        append_acquisition_record(
            snapshot_dir,
            {
                "pageid": None,
                "title": None,
                "url": base_url.rstrip("/") + "/robots.txt",
                "attempt": 1,
                "outcome": "permanent_failure",
                "http_status": getattr(exc, "http_status", None),
                "reason": f"robots.txt unreachable: {exc}",
                "timestamp": _now_iso(),
            },
        )
        raise SystemExit(
            f"could not fetch robots.txt from {base_url} ({exc}) -- refusing to proceed without confirmed "
            f"robots.txt compliance; recorded this failed attempt in {snapshot_dir / 'acquisition.jsonl'}"
        ) from exc


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
    parser.add_argument("--pages-index", required=True, help="path to pages_index.json produced by discover.py")
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

    pages_index = load_pages_index(Path(args.pages_index))
    out_root = Path(args.out_root)

    if args.new_snapshot:
        snapshot_id = new_snapshot_id()
        snapshot_dir = snapshot_dir_for(out_root, snapshot_id)
        if snapshot_dir.exists():
            raise SystemExit(f"snapshot directory {snapshot_dir} already exists -- refusing to reuse a snapshot id")
        snapshot_dir.mkdir(parents=True)
        manifest = init_manifest(
            snapshot_id=snapshot_id,
            wiki_base_url=pages_index["wiki_base_url"],
            discover_source=str(args.pages_index),
            user_agent=args.user_agent,
            http_settings={"delay": args.delay, "timeout": args.timeout, "retries": args.retries},
        )
        write_manifest(snapshot_dir, manifest)
    else:
        snapshot_dir = snapshot_dir_for(out_root, args.snapshot_id)
        if not snapshot_dir.exists():
            raise SystemExit(f"snapshot directory {snapshot_dir} does not exist -- cannot resume; use --new-snapshot to start one")
        manifest = load_manifest(snapshot_dir)
        if manifest.get("wiki_base_url") != pages_index["wiki_base_url"]:
            raise SystemExit("pages_index wiki_base_url does not match this snapshot's recorded wiki_base_url -- refusing to mix sources")

    robot_parser = _build_robot_parser_or_record_failure(
        pages_index["wiki_base_url"], user_agent=args.user_agent, timeout=args.timeout, snapshot_dir=snapshot_dir
    )
    rate_limiter = _http.RateLimiter(args.delay)
    fetch_kwargs = dict(
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        rate_limiter=rate_limiter,
        robot_parser=robot_parser,
    )
    skip_namespaces = {ns.strip() for ns in args.skip_namespaces.split(",") if ns.strip()}

    manifest = fetch_snapshot(
        pages_index,
        snapshot_dir,
        manifest,
        skip_namespaces=skip_namespaces,
        retry_permanent_failures=args.retry_permanent_failures,
        **fetch_kwargs,
    )

    print(f"snapshot {manifest['snapshot_id']}: {manifest['succeeded_page_count']} succeeded, {manifest['failed_page_count']} failed -> {snapshot_dir}")


if __name__ == "__main__":
    main()
