"""Training-loop bookkeeping: findings become regression fixtures; the streak says when training is done."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobagent.apply.ats import is_workday_url
from jobagent.apply.review import WRONG
from jobagent.apply.snapshot import SNAPSHOT_ROOT

FINDING_FIXTURES = Path("tests/fixtures/fill_findings")
_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def tenant(form_url: str) -> str:
    """"acme.wd5.myworkdayjobs.com/..." -> "acme"; other hosts as they are."""
    host = (urlsplit(form_url).hostname or "").casefold()
    return host.split(".")[0] if is_workday_url(form_url) else host


def reviews(job_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(job_dir.glob("[0-9][0-9].review.*.json"))]


def synthetic(value: str) -> str:
    """Same shape, no personal value: dates stay dates, numbers stay numbers, Yes/No stay."""
    first = value.split("|")[0].strip()
    if first.casefold() in {"yes", "no"}:
        return first
    if _DATE.fullmatch(first):
        return "01/01/2030"
    if first.replace(",", "").isdigit():
        return "100000"
    return "Example answer"


def fixtures_for_job(job_id: int, profile_answers: list[Any], root: Path = SNAPSHOT_ROOT) -> list[dict[str, Any]]:
    """One fixture per wrong finding on a JobAgent-filled field: label, kind, options and rule keys only."""
    directory = root / str(job_id)
    snapshot = next((json.loads(path.read_text()) for path in sorted(directory.glob("[0-9][0-9].json"))), {})
    fixtures = []
    for review in reviews(directory):
        for finding in review.get("findings", []):
            if finding.get("problem") not in WRONG or not finding.get("filled_by_jobagent"):
                continue
            wrong = finding.get("source", "")
            fact = finding.get("fact", "")
            expected = "skip" if finding["problem"] == "should_be_empty" else (
                fact if fact and not fact.startswith("answer:") and fact != "yes_no" else "")
            item: dict[str, Any] = {
                "tenant": tenant(snapshot.get("form_url", "")), "label": finding["label"],
                "kind": finding.get("kind") or "text", "options": finding.get("options") or [],
                "wrong_source": wrong, "expected": expected, "problem": finding["problem"],
            }
            if any(other["label"] == item["label"] and other["wrong_source"] == wrong for other in fixtures):
                continue  # the same mistake found by both reviewers (Claude and Cowork)
            canned = next((a for a in profile_answers if wrong == "answer:" + "+".join(a.match)), None)
            if canned is not None:  # reproduce the rule that fired, with a shape-preserving fake value
                item["answers"] = [{"match": list(canned.match), "value": synthetic(canned.value)}]
            fixtures.append(item)
    return fixtures


def write_fixtures(job_id: int, fixtures: list[dict[str, Any]], directory: Path = FINDING_FIXTURES) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for number, item in enumerate(fixtures, 1):
        name = re.sub(r"[^a-z0-9]+", "-", f"{item['tenant']}-{job_id}-{number}".casefold()).strip("-")
        path = directory / f"{name}.json"
        path.write_text(json.dumps(item, indent=2) + "\n")
        paths.append(path)
    return paths


@dataclass(frozen=True)
class Streak:
    tenants: list[str]
    cowork: int
    broken_by: str = ""

    def met(self, n: int, cowork_needed: int = 2) -> bool:
        return len(self.tenants) >= n and self.cowork >= cowork_needed


def streak(root: Path = SNAPSHOT_ROOT, n: int = 5) -> Streak:
    """The newest reviewed Workday jobs, newest first, while every review has zero wrong findings.

    Counts distinct tenants up to ``n``; ``cowork`` is how many of them had a Cowork review.
    """
    jobs = []
    for job_dir in (path for path in root.glob("*") if path.is_dir()):
        found = reviews(job_dir)
        first = next(iter(sorted(job_dir.glob("[0-9][0-9].json"))), None)
        if not found or first is None:
            continue
        form_url = json.loads(first.read_text()).get("form_url", "")
        if is_workday_url(form_url):
            jobs.append((max(item.get("reviewed_at", "") for item in found), job_dir.name, tenant(form_url), found))
    tenants: list[str] = []
    cowork = 0
    for _, job_name, name, found in sorted(jobs, reverse=True):
        if any(f.get("problem") in WRONG for item in found for f in item.get("findings", [])):
            return Streak(tenants, cowork, broken_by=f"job {job_name} ({name})")
        if name in tenants:
            continue
        tenants.append(name)
        cowork += any(item.get("reviewer") == "cowork" for item in found)
        if len(tenants) >= n:
            break
    return Streak(tenants, cowork)
