from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


@dataclass(frozen=True, slots=True)
class FilterValue:
    display_value: str
    request_value: str


@dataclass(frozen=True, slots=True)
class FilterControl:
    name: str
    control_type: str
    values: tuple[FilterValue, ...]
    form_action: str | None
    form_method: str | None
    selector_hint: str
    defaults: tuple[tuple[str, str], ...] = ()


def non_content_form(form) -> bool:
    """Contact/account fields are not website inventory filters."""
    if not form:
        return False
    if form.select_one('input[type=password], input[type=file], input[type=email], input[type=tel], textarea'):
        return True
    return bool(re.search(r'login|checkout|cart|register|delete|subscribe|contact|feedback', form.get('action', ''), re.I)
                or any(re.search(r'^(email|phone|contact|organization|remarks|captcha.*)$', field.get('name', ''), re.I) for field in form.select('input[name]')))


def detect_filters(html: str, page_url: str) -> list[FilterControl]:
    """Learn filter values from supplied page controls; never invent site values."""
    soup = BeautifulSoup(html, "lxml")
    controls: list[FilterControl] = []
    for element in soup.select("select[name], input[type=checkbox][name], input[type=radio][name]"):
        name = element.get("name", "").strip()
        if not name:
            continue
        form = element.find_parent("form")
        if non_content_form(form):
            continue
        form_action = urljoin(page_url, form.get("action") or page_url) if form else page_url
        form_method = (form.get("method", "get") if form else "javascript").lower()
        defaults = []
        if form:
            for hidden in form.select("input[type=hidden][name]"):
                if not re.search(r"token|csrf|password|secret|session", hidden["name"], re.I):
                    defaults.append((hidden["name"], str(hidden.get("value", ""))))
        element_type = element.name if element.name == "select" else (element.get("type") or "input").lower()
        values: list[FilterValue] = []
        if element.name == "select":
            for option in element.select("option[value]"):
                value = str(option.get("value", "")).strip()
                if value and not option.has_attr("disabled"):
                    values.append(FilterValue(option.get_text(" ", strip=True), value))
        else:
            value = str(element.get("value", "on")).strip()
            if value:
                label = element.find_parent("label")
                values.append(FilterValue(label.get_text(" ", strip=True) if label else value, value))
        if values:
            identifier = element.get("id") or name
            controls.append(FilterControl(name, element_type, tuple(values), form_action, form_method, f"#{identifier}" if element.get("id") else f"[name='{name}']", tuple(defaults)))
    return controls


def get_filter_urls(control: FilterControl, page_url: str) -> list[str]:
    """Generate independent single-filter GET states while retaining action query parameters."""
    if control.form_method != "get" or not control.form_action:
        return []
    parts = urlsplit(control.form_action)
    # Preserve dependencies already supplied by the current page and hidden defaults.
    current = dict(parse_qsl(urlsplit(page_url).query, keep_blank_values=True))
    current.update(dict(control.defaults))
    current.update(dict(parse_qsl(parts.query, keep_blank_values=True)))
    base_pairs = [(key, value) for key, value in current.items() if key != control.name and key.lower() not in {"page", "paged", "offset", "start"}]
    return [urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode([*base_pairs, (control.name, value.request_value)]), ""))
            for value in control.values]
