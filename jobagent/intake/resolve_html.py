"""Static extraction only: scripts are text/JSON, never executed."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobagent.normalization import normalize_title

COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "plc", "gmbh"}


def company_key(name: str) -> str:
    return "".join(w for w in re.findall(r"[a-z0-9]+", name.casefold()) if w not in COMPANY_SUFFIXES)


def identity_matches(company: str, title: str, actual_company: str, actual_title: str) -> bool:
    """Require company equality and close titles, preserving seniority and level tokens."""
    wanted, actual = normalize_title(title), normalize_title(actual_title)
    if not wanted or not actual or not company_key(company):
        return False
    levels = {"senior", "junior", "lead", "principal", "staff", "intern", "ii", "iii", "iv", "1", "2", "3", "4"}
    return (
        company_key(company) == company_key(actual_company)
        and set(wanted.split()) & levels == set(actual.split()) & levels
        and SequenceMatcher(None, wanted, actual).ratio() >= 0.9
    )


@dataclass
class PageData:
    identities: list[tuple[str, str]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    easy_apply: bool = False

    def matches(self, company: str, title: str) -> bool:
        return any(identity_matches(company, title, c, t) for c, t in self.identities)


def parse_page(html: str, url: str) -> PageData:
    soup = BeautifulSoup(html, "html.parser")
    result = PageData()
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.get_text())
        except (ValueError, RecursionError):
            continue
        pending = [data]
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                if item.get("@type") == "JobPosting":
                    organization = item.get("hiringOrganization", {})
                    company = organization.get("name") if isinstance(organization, dict) else None
                    title = item.get("title")
                    if isinstance(company, str) and isinstance(title, str):
                        result.identities.append((company, title))
                pending.extend(v for v in item.values() if isinstance(v, (dict, list)))
    title = soup.select_one("h1")
    company = soup.select_one(
        '[itemprop="hiringOrganization"], .topcard__org-name-link, '
        '.topcard__flavor--black-link, [data-company-name]'
    )
    if title and company:
        result.identities.append((str(company.get("data-company-name") or company.get_text(" ", strip=True)),
                                  title.get_text(" ", strip=True)))
    for tag in soup.find_all(["a", "iframe"]):
        value = tag.get("src" if tag.name == "iframe" else "href")
        if isinstance(value, str):
            result.links.append((urljoin(url, unescape(value)), "iframe" if tag.name == "iframe" else "link"))
    for tag in soup.find_all("input", attrs={"name": re.compile(r"^(?:url|applyUrl|apply_url)$")}):
        value = tag.get("value")
        if isinstance(value, str):
            result.links.append((urljoin(url, unescape(value)), "offsite_input"))
    # Quoted URL values in script config (including escaped JSON URLs); never eval JavaScript.
    for script in soup.find_all("script"):
        raw = script.get_text().replace(r"\/", "/").replace(r"\u002F", "/").replace(r"\u0026", "&")
        for value in re.findall(r'''["'](https?://[^\s"'<>]+)["']''', raw):
            result.links.append((unescape(value), "script_config"))
        if re.search(r'"(?:isEasyApply|easyApply)"\s*:\s*true\b', raw):
            result.easy_apply = True
    for tag in soup.find_all(["a", "button"]):
        label = str(tag.get("aria-label") or tag.get_text(" ", strip=True))
        if re.fullmatch(r"easy apply(?: to .+)?", label, flags=re.IGNORECASE):
            result.easy_apply = True
    return result
