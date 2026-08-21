# Diep.io Wiki Corpus — Structural Inventory Report

**This is not a corpus inventory. It is an honest report of a failed live-acquisition attempt.**
No page content from `https://diepio.fandom.com` was acquired this session. Nothing below should
be read as describing the real wiki corpus — only what the tooling itself is capable of, as
verified against synthetic test fixtures.

## What happened

`tools/wiki/discover.py` was run for real against `https://diepio.fandom.com` with a generic,
non-personal User-Agent (`deep-eye-oh-wiki-inventory/0.1 (+research tool; see project repo)`),
using the public MediaWiki `action=query` API via Python's `urllib`, per the plan's approved
design (stdlib HTTP client, no `requests`).

Per this project's access constraints, the tool checks `robots.txt` before making any other
request and currently refuses to proceed if that check does not succeed. That check did not
succeed: `https://diepio.fandom.com/robots.txt` returned **HTTP 403 Forbidden** to this tool's
`urllib` client, consistently, across 4 repeated attempts.

**Correction from an earlier version of this report**: that 403 is *not* a robots-protocol
prohibition on crawling, and should not have been described as one. RFC 9309 (the Robots
Exclusion Protocol), §2.3.1.3, classifies a 4xx response when fetching `robots.txt` as
**"unavailable"**, and explicitly permits a crawler to proceed as if there were no restrictions
in that case (this is distinct from a 5xx/"unreachable" response, which the RFC treats more
cautiously). So, per the protocol itself, an HTTP 403 on `robots.txt` alone does not obligate a
crawler to stop. The tool's current refusal to proceed is a deliberate, conservative
implementation choice that is stricter than the protocol requires — not something robots.txt
itself is mandating — and it remains in place for a different, more concrete reason described
below (Fandom's Terms of Use), not because of the 403 itself.

### Diagnosis (evidence, not a workaround)

To understand — not bypass — this failure, a few read-only diagnostic checks were run (outside
the committed tool code):

- `curl` (same machine, same network, same User-Agent string) fetched the same
  `https://diepio.fandom.com/robots.txt` URL successfully (HTTP 200). Its response carried
  `CF-Cache-Status: HIT` and `Age: 25512`, i.e. a Cloudflare-edge-cached response served without
  reaching the origin.
- The same Python `urllib` client, requesting `https://diepio.fandom.com/api.php` (the actual
  resource the tool needs for content), succeeded (HTTP 200) — the block is specific to the
  `/robots.txt` path for this client, not the whole site.
- Swapping the `User-Agent` string sent by `urllib` (including sending the literal string
  `curl/8.0`) did not change the outcome — `urllib` was still blocked on `/robots.txt` while
  `curl` itself, actually run as `curl`, was not.

Together this points to Cloudflare bot-management fingerprinting the HTTP client (TLS/HTTP stack
signature) rather than anything about the User-Agent header content or request headers. This is
exactly the kind of anti-bot protection this project's access constraints forbid working around —
no TLS/client impersonation, proxy, or CAPTCHA-solving was attempted, and none should be.

### Where the acquisition gate currently sits

The tool refuses all further requests (including to `/api.php`, even though that specific
endpoint is independently reachable) whenever the `robots.txt` check does not cleanly succeed.
This refusal is implemented in `tools/wiki/discover.py`'s
`_build_robot_parser_or_record_failure` (mirrored in `fetch.py`), which durably records the
failed attempt to `knowledge/raw/fandom/acquisition_attempt_log.jsonl` before exiting — see that
file for the raw record (timestamp, URL, HTTP status, reason). No page content, page list, or
snapshot was written; there is nothing to preserve raw-vs-derived separation over, because
nothing was acquired.

As corrected above, this gate is not required by robots.txt itself for a 4xx response — it is
being left in place deliberately, unchanged, because of a separate and more directly applicable
restriction, below. **The crawler has not been changed to bypass or loosen this gate.**

## Actual current blocker: Fandom's Terms of Use

Independent of the robots.txt technical question, Fandom's Terms of Use restrict automated
scraping/retrieval of site content without express written permission, and separately restrict
using or copying site content for software/AI development purposes without prior written
consent. This project's stated purpose for this data — structured prior knowledge feeding a
software/AI system (perception, world-modelling, simulator development, eventually policy) — is
exactly the kind of use those terms require prior written consent for.

This is the real, controlling blocker on further acquisition right now, not the `robots.txt`
403. It is a licensing/permissions question, not a technical or engineering one, and resolving
it (requesting/obtaining Fandom's written permission, or choosing a different path) is a
decision for a human, outside the scope of this tooling slice.

### Known sanctioned alternative, deliberately not used yet

Fandom officially publishes database dumps of wiki content, linked from each wiki's
`Special:Statistics` page. This is a legitimate, sanctioned bulk-access mechanism distinct from
live API scraping, and would sidestep the robots.txt/live-crawling question entirely. **This
project has not ingested such a dump in this slice.** Doing so would not, by itself, resolve the
open question above: the same Terms of Use restriction on using/copying content for software/AI
development without prior written consent would still apply to dump-derived content, since the
restriction is about the *use*, not the *acquisition method*. Ingesting a dump before that
consent question is resolved would just move the unresolved policy risk downstream rather than
address it.

## Corpus

- pages discovered: 0
- pages acquired: 0
- pages failed: 0 (no *pages* were attempted — acquisition never got past the robots.txt gate)
- redirects: unknown — not reached
- canonical candidates / fanon / community / other: unknown — not reached

## Information structure / Domains / Temporal data

Not measured against real data this session. The extraction and classification machinery for
all of this (sections, templates/infoboxes, tables with best-effort `parse_quality`, candidate
domains, canonical/fanon/community separation) is implemented in `tools/wiki/_wikitext.py` and
`tools/wiki/_classify.py`, and is verified to work correctly against representative *synthetic*
wikitext/API-response fixtures and a hand-built `tiny_snapshot` (see `tests/test_wiki_wikitext.py`,
`tests/test_wiki_classify.py`, `tests/test_wiki_inventory.py` — 57 tests, all passing, fully
offline). None of that is a substitute for measuring the real corpus; it only establishes that
the tooling is ready to do so once acquisition succeeds.

## Provenance

Fully designed and tested (see `tools/wiki/inventory.py`'s `provenance` block on every output,
and the raw page record schema in `fetch.py`), but never exercised against a real snapshot this
session. What it captures once acquisition succeeds: pageid, title, namespace, canonical URL,
revision id/parentid/timestamp/contentmodel/contentformat, retrieved_at, content_sha256,
categories, redirect status/target — deliberately excluding revision author/edit-comment
(data minimization). Not retained even in the design: sub-page character-offset coordinates for
a given section/template/table (only page-level and nearest-heading context).

## Schema implications

- still-unknown: whether the eventual knowledge schema is well-supported by this wiki's actual
  category/template/table structure cannot be assessed until real acquisition succeeds — this
  session validates the *tooling*, not the *corpus*.
- still-unknown: whether/when Fandom will grant the written permission and consent its Terms of
  Use require for automated retrieval and for software/AI use of the content — that decision,
  and any consequent choice of acquisition path (live API once permitted, the official
  `Special:Statistics` database dump once the same consent question is resolved, or a different
  source entirely), is a call for a human, not something this tool should decide unilaterally.
- still-unknown (narrower, technical): whether the `robots.txt` 403 itself is specific to this
  network/environment or a durable edge/CDN behavior was not further investigated, since it turned
  out not to be the controlling blocker — resolving it would not unblock acquisition on its own.
- supports: the offline half of the pipeline (fixture-driven discovery/section/template/table/
  classification parsing and offline inventory regeneration) works correctly and independently
  of network access, exactly as the plan required ("Analysis/tests must work completely offline
  from committed test fixtures").
- contradicts: nothing about the real corpus's structure, since none was observed.

## Data quality problems

- acquisition failures: 1 recorded (`robots.txt` fetch returned HTTP 403 — see
  `knowledge/raw/fandom/acquisition_attempt_log.jsonl`), 0 page-level failures (no pages were
  attempted).
- this REPORT.md itself is a known limitation: it documents an acquisition failure/policy
  blocker, not a corpus inventory. Do not treat the empty `knowledge/inventory/*.json` state (or
  absence of files) as evidence about the wiki's actual structure — it reflects only that this
  run never reached it.
- an earlier version of this report mischaracterized the HTTP 403 on `robots.txt` as a
  robots-protocol prohibition on crawling; that has been corrected above (RFC 9309 classifies a
  4xx robots.txt response as "unavailable," under which a crawler may proceed) — the real,
  current blocker is Fandom's Terms of Use, not the robots.txt response.
