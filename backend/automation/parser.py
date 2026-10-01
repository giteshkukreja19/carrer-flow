"""Read-only parsers for the captured Haveloc pages.

The selectors below are taken from the recovered HTML fixtures. This module
only reads page content; it does not interact with Haveloc or perform actions.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_DASHES = {"-", "–", "—", "―"}


class _Element:
    """Minimal DOM node that retains text and element order."""

    __slots__ = ("tag", "attrs", "parts", "parent")

    def __init__(self, tag: str, attrs: list[tuple[str, str | None]], parent: _Element | None):
        self.tag = tag
        self.attrs = {key: value or "" for key, value in attrs}
        self.parts: list[str | _Element] = []
        self.parent = parent

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())


class _DOMParser(HTMLParser):
    """Small standard-library HTML tree builder for the static captured pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("document", [], None)
        self.stack = [self.root]

    def _close_open(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # The captured pages contain ordinary table/list markup. These implied
        # closures keep the tree sound if a browser-tolerated close tag is absent.
        if tag in {"td", "th", "tr"}:
            self._close_open("td")
            self._close_open("th")
            if tag == "tr":
                self._close_open("tr")
        node = _Element(tag, attrs, self.stack[-1])
        self.stack[-1].parts.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self._close_open(tag)

    def handle_data(self, data: str) -> None:
        if data and not any(node.tag in {"script", "style"} for node in self.stack):
            self.stack[-1].parts.append(data)


def _parse(html: str | bytes) -> _Element:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    parser = _DOMParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _text(node: _Element | None) -> str:
    if node is None:
        return ""
    raw: list[str] = []

    def collect(part: str | _Element) -> None:
        if isinstance(part, str):
            raw.append(part)
        elif part.tag == "br":
            raw.append(" ")
        else:
            for child in part.parts:
                collect(child)

    for part in node.parts:
        collect(part)
    return re.sub(r"\s+", " ", "".join(raw)).strip()


def _find(node: _Element, class_name: str) -> list[_Element]:
    matches: list[_Element] = []
    if class_name in node.classes:
        matches.append(node)
    for part in node.parts:
        if isinstance(part, _Element):
            matches.extend(_find(part, class_name))
    return matches


def _first(node: _Element, class_name: str) -> _Element | None:
    matches = _find(node, class_name)
    return matches[0] if matches else None


def _children(node: _Element, tag: str | None = None) -> list[_Element]:
    return [
        part for part in node.parts
        if isinstance(part, _Element) and (tag is None or part.tag == tag)
    ]


def _descendants(node: _Element, tag: str) -> list[_Element]:
    found: list[_Element] = []
    for child in _children(node):
        if child.tag == tag:
            found.append(child)
        found.extend(_descendants(child, tag))
    return found


def _present(value: str | None) -> str | None:
    """Return captured text, treating empty and standalone dash placeholders as absent."""
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return None if not value or value in _DASHES else value


def _class_text(node: _Element, class_name: str) -> str | None:
    match = _first(node, class_name)
    return _present(_text(match))


def _header_map(table: _Element) -> dict[str, int]:
    thead = next((child for child in _children(table) if child.tag == "thead"), None)
    if thead is None:
        return {}
    rows = _descendants(thead, "tr")
    if not rows:
        return {}
    cells = [cell for cell in _children(rows[0]) if cell.tag in {"th", "td"}]
    return {re.sub(r"\s+", " ", _text(cell)).strip().casefold(): i for i, cell in enumerate(cells)}


def _cell_by_header(cells: list[_Element], headers: dict[str, int], label: str) -> _Element | None:
    index = headers.get(label.casefold())
    return cells[index] if index is not None and index < len(cells) else None


def parse_jobs(html: str | bytes) -> list[dict[str, str | None]]:
    """Parse job/application rows from the captured desktop jobs table."""
    root = _parse(html)
    table = next(
        (node for node in _find(root, "jobs-table") if "jobs-table--desktop" in node.classes),
        None,
    )
    if table is None:
        return []
    headers = _header_map(table)
    tbody = next((child for child in _children(table) if child.tag == "tbody"), None)
    if tbody is None:
        return []

    jobs: list[dict[str, str | None]] = []
    for row in _children(tbody, "tr"):
        cells = [cell for cell in _children(row) if cell.tag in {"td", "th"}]
        if not cells:
            continue
        title_cell = _cell_by_header(cells, headers, "title") or cells[0]
        salary_cell = _cell_by_header(cells, headers, "salary")
        stipend_cell = _cell_by_header(cells, headers, "stipend")
        apply_cell = _cell_by_header(cells, headers, "apply before")
        visit_cell = _cell_by_header(cells, headers, "date of visit")
        status_cell = _cell_by_header(cells, headers, "status")
        batch_cell = _cell_by_header(cells, headers, "batch")
        posted_cell = _cell_by_header(cells, headers, "job posted date")

        jobs.append({
            "company": _class_text(title_cell, "company-text"),
            "role": _class_text(title_cell, "title-text"),
            "salary_ctc": _class_text(salary_cell, "salary-text") if salary_cell else None,
            "salary_type": _class_text(salary_cell, "type-text") if salary_cell else None,
            "stipend": _class_text(stipend_cell, "salary-text") if stipend_cell else None,
            "stipend_period": _class_text(stipend_cell, "type-text") if stipend_cell else None,
            "apply_by": _class_text(apply_cell, "applyby-date") if apply_cell else None,
            "apply_by_time": _class_text(apply_cell, "applyby-time") if apply_cell else None,
            "visit_date": _class_text(visit_cell, "salary-text") if visit_cell else None,
            "visit_date_note": _class_text(visit_cell, "type-text") if visit_cell else None,
            "status": _class_text(status_cell, "status-badge") if status_cell else None,
            "batch": _present(_text(batch_cell)) if batch_cell else None,
            "posted_date": _present(_text(posted_cell)) if posted_cell else None,
        })
    return jobs


def _description_eligibility(description: _Element | None) -> list[str]:
    if description is None:
        return []
    values = []
    for item in _descendants(description, "li"):
        content = _text(item)
        match = re.search(r"\bEligibility\s*:\s*(.*)", content, flags=re.IGNORECASE)
        if match:
            value = _present(match.group(1))
            if value:
                values.append(value)
    return values


def parse_job_details(html: str | bytes) -> dict[str, object]:
    """Parse company details, metrics, process, facts, skills and attachment labels."""
    root = _parse(html)
    description = _first(root, "sjd-job-desc__html")

    metrics: list[dict[str, str | None]] = []
    for metric in _find(root, "sjd-metric"):
        metrics.append({
            "label": _present(_class_text(metric, "sjd-metric__label")),
            "value": _class_text(metric, "sjd-metric__value"),
            "meta": _class_text(metric, "sjd-metric__meta"),
        })

    rounds: list[dict[str, str | None]] = []
    for item in _find(root, "sjd-round-item"):
        rounds.append({
            "name": _class_text(item, "sjd-round-item__title"),
            "meta": _class_text(item, "sjd-round-item__meta"),
            "status": _class_text(item, "sjd-round-item__status"),
        })

    facts: list[dict[str, object]] = []
    for section in _find(root, "sjd-facts-split__col"):
        heading = _class_text(section, "sjd-facts-split__heading")
        section_facts = []
        for fact in _find(section, "sjd-fact"):
            section_facts.append({
                "label": _class_text(fact, "sjd-fact__label"),
                "value": _class_text(fact, "sjd-fact__value"),
            })
        facts.append({"section": heading, "items": section_facts})

    attachments: list[dict[str, str | None]] = []
    for row in _find(root, "sjd-attachment-row"):
        name = _class_text(row, "sjd-attachment-row__name")
        if not name:
            continue
        link = next((node for node in _descendants(row, "a")), None)
        attachments.append({"name": name, "href": _present(link.attrs.get("href")) if link else None})

    return {
        "company": _class_text(root, "sjd-hero__company-name"),
        "role": _class_text(root, "sjd-hero__title"),
        "role_type": _class_text(root, "sjd-job-role-line"),
        "status": _class_text(root, "sjd-hero__status"),
        "job_tags": [_text(tag) for tag in _find(root, "sjd-hero__tag") if _present(_text(tag))],
        "metrics": metrics,
        "hiring_process": rounds,
        "job_description": _present(_text(description)),
        "eligibility": _description_eligibility(description),
        "facts": facts,
        "skills": [_text(skill) for skill in _find(root, "sjd-skill-tag") if _present(_text(skill))],
        "attachments": attachments,
    }


def _meta_values(node: _Element) -> list[str]:
    meta = _first(node, "stu-job-att__meta") or _first(node, "stu-round-acc__meta")
    if meta is None:
        return []
    items = _descendants(meta, "li")
    values = [_text(item) for item in items]
    return [value for value in values if _present(value)]


def _participation_item(node: _Element, prefix: str) -> dict[str, object]:
    return {
        "company": _class_text(node, f"{prefix}__company"),
        "role": _class_text(node, f"{prefix}__job"),
        "round": _class_text(node, f"{prefix}__round"),
        "status": _class_text(node, f"{prefix}__badge"),
        "meta": _meta_values(node),
        "note": _class_text(node, f"{prefix}__card-note"),
    }


def parse_participation(html: str | bytes) -> dict[str, list[dict[str, object]]]:
    """Parse displayed attendance and round-confirmation records without acting on them."""
    root = _parse(html)
    return {
        "attendance": [_participation_item(card, "stu-job-att") for card in _find(root, "stu-job-att__card")],
        "rounds": [_participation_item(card, "stu-round-acc") for card in _find(root, "stu-round-acc__card")],
    }


__all__ = ["parse_jobs", "parse_job_details", "parse_participation"]
