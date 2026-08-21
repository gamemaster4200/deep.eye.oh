"""Enumerate every namespace and page on a MediaWiki/Fandom wiki (metadata
only — titles, ids, namespaces, categories, redirect flags/targets — no
page content) via the public action=query API, and write the result to a
`pages_index.json` used by fetch.py to decide what to actually acquire.

Cheap and safe to re-run often: this never writes page content, and always
overwrites its `--out` path fresh (it is not itself a raw snapshot). robots.
txt is checked against every `/api.php` URL this tool actually requests
before it's sent.

Development tooling only — not part of the production pipeline.

    python tools/wiki/discover.py --user-agent "deep-eye-oh-wiki-inventory/0.1 (+research tool; see project repo)" --out knowledge/raw/fandom/pages_index.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _http
import _mediawiki


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_namespace_pages(base_url: str, namespace_id: int, namespace_name: str, **fetch_kwargs) -> list[dict]:
    pages: list[dict] = []
    for response in _mediawiki.query_continued(base_url, _mediawiki.allpages_params(namespace_id), **fetch_kwargs):
        for raw_page in _mediawiki.parse_pages(response):
            if "missing" in raw_page:
                continue
            categories = [c["title"] for c in raw_page.get("categories", [])]
            pages.append(
                {
                    "pageid": raw_page["pageid"],
                    "title": raw_page["title"],
                    "namespace_id": namespace_id,
                    "namespace_name": namespace_name,
                    "canonical_url": raw_page.get("fullurl"),
                    "is_redirect": bool(raw_page.get("redirect", False)),
                    "categories": categories,
                }
            )
    return pages


def discover_all_pages(base_url: str, namespaces: dict[int, str], **fetch_kwargs) -> list[dict]:
    """Enumerate every non-virtual namespace (ns id >= 0 — Special/Media
    namespaces are virtual and not enumerable via allpages)."""
    pages: list[dict] = []
    for namespace_id, namespace_name in sorted(namespaces.items()):
        if namespace_id < 0:
            continue
        pages.extend(discover_namespace_pages(base_url, namespace_id, namespace_name, **fetch_kwargs))
    return pages


def resolve_redirects(base_url: str, pages: list[dict], **fetch_kwargs) -> None:
    """Fills redirect_target_title/redirect_target_pageid (both None if not
    a redirect, or if the target couldn't be resolved) on every page in
    `pages`, in place."""
    redirect_pages = [p for p in pages if p["is_redirect"]]
    by_title = {p["title"]: p for p in redirect_pages}
    for batch in _mediawiki.chunked(list(by_title.keys())):
        response = _mediawiki.query(base_url, _mediawiki.redirect_resolution_params(batch), **fetch_kwargs)
        resolution = _mediawiki.parse_redirect_resolution(response)
        for from_title, target in resolution.items():
            page = by_title.get(from_title)
            if page is not None:
                page["redirect_target_title"] = target["to_title"]
                page["redirect_target_pageid"] = target["to_pageid"]
    for page in pages:
        page.setdefault("redirect_target_title", None)
        page.setdefault("redirect_target_pageid", None)


def _build_robot_parser_or_record_failure(base_url: str, *, user_agent: str, timeout: float, out_path: Path):
    """Wraps _http.build_robot_parser: if robots.txt itself cannot be
    fetched (this tool's own client blocked, timeout, etc.), that is a
    legitimate acquisition failure -- refuse to proceed without confirmed
    robots.txt compliance, and record the failed attempt durably instead of
    a raw traceback. Never falls back to skipping the robots.txt check."""
    try:
        return _http.build_robot_parser(base_url, user_agent=user_agent, timeout=timeout)
    except (_http.PermanentAcquisitionError, _http.TransientAcquisitionError) as exc:
        record = {
            "attempted_at": _now_iso(),
            "base_url": base_url,
            "target_resource": "robots.txt",
            "user_agent": user_agent,
            "outcome": "unreachable",
            "http_status": getattr(exc, "http_status", None),
            "reason": str(exc),
            "note": (
                "robots.txt could not be fetched by this tool's standard HTTP client; refusing to proceed "
                "without confirmed robots.txt compliance. No bypass (TLS/client impersonation, proxy, CAPTCHA "
                "solving) was attempted."
            ),
        }
        log_path = out_path.parent / "acquisition_attempt_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        raise SystemExit(
            f"could not fetch robots.txt from {base_url} ({exc}) -- refusing to proceed without confirmed "
            f"robots.txt compliance; recorded this failed attempt to {log_path}"
        ) from exc


def build_pages_index(base_url: str, namespaces: dict[int, str], pages: list[dict]) -> dict:
    return {
        "format_version": 1,
        "wiki_base_url": base_url,
        "generated_at": _now_iso(),
        "namespaces": {str(ns_id): name for ns_id, name in namespaces.items()},
        "pages": pages,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://diepio.fandom.com", help="wiki base URL")
    _http.add_http_args(parser)
    parser.add_argument("--out", default="knowledge/raw/fandom/pages_index.json", help="output path for pages_index.json")
    args = parser.parse_args(argv)

    robot_parser = _build_robot_parser_or_record_failure(args.base_url, user_agent=args.user_agent, timeout=args.timeout, out_path=Path(args.out))
    rate_limiter = _http.RateLimiter(args.delay)
    fetch_kwargs = dict(
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        rate_limiter=rate_limiter,
        robot_parser=robot_parser,
    )

    siteinfo = _mediawiki.query(args.base_url, _mediawiki.siteinfo_params(), **fetch_kwargs)
    namespaces = _mediawiki.parse_namespaces(siteinfo)

    pages = discover_all_pages(args.base_url, namespaces, **fetch_kwargs)
    resolve_redirects(args.base_url, pages, **fetch_kwargs)

    index = build_pages_index(args.base_url, namespaces, pages)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    by_namespace: dict[str, int] = {}
    for page in pages:
        by_namespace[page["namespace_name"]] = by_namespace.get(page["namespace_name"], 0) + 1
    redirect_count = sum(1 for p in pages if p["is_redirect"])
    print(f"discovered {len(pages)} page(s) across {len(namespaces)} namespace(s), {redirect_count} redirect(s) -> {out_path}")
    for ns_name, count in sorted(by_namespace.items(), key=lambda kv: -kv[1]):
        print(f"  {ns_name}: {count}")


if __name__ == "__main__":
    main()
