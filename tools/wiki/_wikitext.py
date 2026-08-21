"""mwparserfromhell-based extraction of sections/templates/links from raw
MediaWiki wikitext, plus a small hand-rolled, best-effort wikitext table
parser (mwparserfromhell has no native table model).

Table parsing is deliberately best-effort inventory, not semantic
normalization: every table's raw source text is preserved regardless of how
well it parsed, and a `parse_quality` of "complete"/"partial"/"failed" is
always reported. `extract_tables` never raises on malformed table wikitext.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

import re

import mwparserfromhell

_REDIRECT_RE = re.compile(r"^\s*#REDIRECT\s*:?\s*\[\[([^\]|#]+)", re.IGNORECASE)
_HEADING_LINE_RE = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$")

_TABLE_FLAG_KEYWORDS = {
    "levels_like": ["level", "lvl"],
    "stats_like": ["stat", "health", "damage", "speed", "reload", "hp", "body damage"],
    "xp_like": ["xp", "experience", "score"],
    "upgrade_relationship_like": ["upgrade", "upgrades to", "branch", "evolves"],
    "game_mode_like": ["mode", "ffa", "team", "domination", "maze", "tag"],
    "balance_like": ["balance", "nerf", "buff", "changed from"],
    "changelog_like": ["date", "version", "update", "patch"],
}


def parse(wikitext: str) -> mwparserfromhell.wikicode.Wikicode:
    return mwparserfromhell.parse(wikitext)


def extract_sections(wikitext: str) -> list[dict]:
    code = parse(wikitext)
    sections = []
    for index, heading in enumerate(code.filter_headings()):
        sections.append({"heading": str(heading.title).strip(), "level": heading.level, "index": index})
    return sections


def extract_templates(wikitext: str) -> list[dict]:
    code = parse(wikitext)
    templates = []
    for template in code.filter_templates(recursive=True):
        fields = {}
        for param in template.params:
            fields[str(param.name).strip()] = str(param.value).strip()
        templates.append({"name": str(template.name).strip(), "fields": fields})
    return templates


def extract_internal_links(wikitext: str) -> list[str]:
    code = parse(wikitext)
    links = []
    for link in code.filter_wikilinks():
        title = str(link.title).strip()
        if title and not title.lower().startswith(("file:", "image:", "category:")):
            links.append(title.split("#")[0].strip())
    return links


def detect_redirect_target(wikitext: str) -> str | None:
    match = _REDIRECT_RE.match(wikitext)
    return match.group(1).strip() if match else None


def _line_heading(line: str) -> str | None:
    match = _HEADING_LINE_RE.match(line.strip())
    return match.group(2).strip() if match else None


def _split_cells(line: str, marker_len: int, multi_sep: str) -> list[str]:
    content = line[marker_len:]
    return [cell.strip() for cell in content.split(multi_sep)]


def _parse_table_block(block_lines: list[str]) -> dict:
    """Best-effort row/column extraction for one `{| ... |}` block's lines
    (attribute line and closing `|}` excluded by the caller). Never raises —
    degrades to parse_quality "failed" on any unexpected shape."""
    quality = "complete"
    columns: list[str] | None = None
    rows: list[list[str]] = []
    current_row: list[str] = []

    try:
        for raw_line in block_lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("|-"):
                if current_row:
                    rows.append(current_row)
                current_row = []
                continue
            if line.startswith("|+"):
                continue  # caption
            if line.startswith("!"):
                cells = _split_cells(line, 1, "!!")
                if columns is None:
                    columns = cells
                else:
                    current_row.extend(cells)
                continue
            if line.startswith("|"):
                cells = _split_cells(line, 1, "||")
                current_row.extend(cells)
                continue
            # continuation of a multi-line cell — best-effort append, flags partial
            if current_row:
                current_row[-1] = (current_row[-1] + " " + line).strip()
            quality = "partial"
        if current_row:
            rows.append(current_row)
    except Exception:
        return {"columns": [], "row_count": 0, "representative_rows": [], "parse_quality": "failed"}

    if columns is None:
        columns = [f"col_{i}" for i in range(max((len(r) for r in rows), default=0))]

    if rows and any(len(r) != len(columns) for r in rows) and quality == "complete":
        quality = "partial"

    return {
        "columns": columns,
        "row_count": len(rows),
        "representative_rows": rows[:5],
        "parse_quality": quality,
    }


def _table_flags(heading: str | None, columns: list[str]) -> tuple[dict, dict]:
    haystack = " ".join([heading or ""] + [str(c) for c in columns]).lower()
    flags: dict[str, bool] = {}
    evidence: dict[str, list[str]] = {}
    for flag, keywords in _TABLE_FLAG_KEYWORDS.items():
        matched = [kw for kw in keywords if kw in haystack]
        flags[flag] = bool(matched)
        if matched:
            evidence[flag] = matched
    return flags, evidence


def extract_tables(wikitext: str) -> list[dict]:
    """Locate every `{| ... |}` block (tracking nesting depth so a nested
    inner table doesn't prematurely close the outer one — nested tables are
    not separately parsed in v0, which is reflected as reduced parse_quality
    for the outer block), assign it the nearest preceding heading as
    context, and best-effort parse rows/columns. Never raises."""
    lines = wikitext.splitlines()
    tables: list[dict] = []
    current_heading: str | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        heading = _line_heading(line)
        if heading is not None:
            current_heading = heading
            i += 1
            continue
        if line.strip().startswith("{|"):
            depth = 1
            block_lines = [line]
            i += 1
            nested = False
            while i < n and depth > 0:
                block_line = lines[i]
                stripped = block_line.strip()
                if stripped.startswith("{|"):
                    depth += 1
                    nested = True
                elif stripped.startswith("|}"):
                    depth -= 1
                block_lines.append(block_line)
                i += 1
            raw_wikitext = "\n".join(block_lines)
            body = block_lines[1:-1] if block_lines and block_lines[-1].strip().startswith("|}") else block_lines[1:]
            parsed = _parse_table_block(body)
            if nested and parsed["parse_quality"] != "failed":
                parsed["parse_quality"] = "partial"
            flags, evidence = _table_flags(current_heading, parsed["columns"])
            tables.append(
                {
                    "heading": current_heading,
                    "raw_wikitext": raw_wikitext,
                    **parsed,
                    "flags": flags,
                    "flag_evidence": evidence,
                }
            )
            continue
        i += 1
    return tables
