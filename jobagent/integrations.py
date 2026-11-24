"""Narrow external workflow adapters; SQLite remains authoritative."""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from jobagent.regions import job_country

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
NOTION_STATUSES = ("Ready", "Prefilled", "Drafted", "Applied", "Recruiter Screen", "Interview", "Offer", "Skipped", "Rejected", "Ghosted")
# Properties JobAgent writes; the database's own title property is reused, never renamed.
NOTION_PROPERTIES: dict[str, dict[str, object]] = {
    "Company": {"rich_text": {}},
    "Location": {"rich_text": {}},
    "Score": {"number": {}},
    "Status": {"select": {"options": [{"name": name} for name in NOTION_STATUSES]}},
    "Apply URL": {"url": {}},
    "Queue Date": {"date": {}},
    "Packet": {"rich_text": {}},
    "JobAgent ID": {"number": {}},
    "Contact": {"rich_text": {}},
    "Contact Email": {"email": {}},
    "Outreach Draft": {"rich_text": {}},
    "No.": {"number": {}},  # dense 1..N row count, rewritten by every sync (see renumber)
}
# Country column on the one dashboard, so US and international rows can be filtered apart.
COUNTRY_PROPERTY: dict[str, dict[str, object]] = {"Country": {"rich_text": {}}}


class Tracker(Protocol):
    def setup(self) -> list[str]: ...
    def upsert(self, packet: Any, job: Any, assessment: Any | None, queue_date: str | None, outreach: Any | None = None) -> str: ...
    def statuses(self) -> dict[int, str]: ...
    def renumber(self, rank: dict[int, int]) -> int: ...
    def set_status(self, page_id: str, status: str) -> None: ...


# Decisions and outcomes Master records in Notion that JobAgent copies into SQLite before each run.
NOTION_PULLED_STATUSES = ("Applied", "Recruiter Screen", "Interview", "Offer", "Skipped", "Rejected", "Ghosted")


class NotionTracker:
    """Notion is only Master's view: JobAgent pushes rows, SQLite stays the source of truth."""

    def __init__(
        self,
        token: str | None = None,
        database_id: str | None = None,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        with_country: bool = False,
    ) -> None:
        self.token = token or os.getenv("NOTION_API_KEY")
        self.database_id = database_id or os.getenv("NOTION_DATABASE_ID")
        self.with_country = with_country
        if not self.token or not self.database_id:
            raise RuntimeError("NOTION_API_KEY and NOTION_DATABASE_ID must be configured")
        self._client_factory = client_factory or self._default_client
        self._data_source_id: str | None = None
        self._title_property: str | None = None

    def _default_client(self) -> httpx.Client:
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}
        return httpx.Client(base_url=NOTION_API, headers=headers, timeout=20)

    @staticmethod
    def _call(client: httpx.Client, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, Any]:
        try:
            response = client.request(method, path, json=body)
        except httpx.HTTPError as error:
            raise RuntimeError(f"Notion {method} {path} failed: {error}") from error
        if response.is_error:
            raise RuntimeError(f"Notion {method} {path} failed: HTTP {response.status_code} {response.text[:300]}")
        return response.json()

    def _schema(self, client: httpx.Client) -> dict[str, Any]:
        """Resolve the database's first data source and return its property schema."""
        if self._data_source_id is None:
            sources = self._call(client, "GET", f"/databases/{self.database_id}").get("data_sources") or []
            if not sources:
                raise RuntimeError("Notion database has no data source")
            self._data_source_id = str(sources[0]["id"])
        properties = self._call(client, "GET", f"/data_sources/{self._data_source_id}").get("properties", {})
        self._title_property = next(
            (name for name, spec in properties.items() if spec.get("type") == "title"), "Name"
        )
        return properties

    def setup(self) -> list[str]:
        """Add the missing JobAgent properties and Status options; return what was added.

        Existing Status options (and any Master added himself) are kept: Notion replaces a select's
        option list on update, so the full list is sent.
        """
        with self._client_factory() as client:
            existing = self._schema(client)
            wanted = {**NOTION_PROPERTIES, **(COUNTRY_PROPERTY if self.with_country else {})}
            missing: dict[str, object] = {name: spec for name, spec in wanted.items() if name not in existing}
            added = sorted(missing)
            status = existing.get("Status") or {}
            if status.get("type") == "select":
                current = (status.get("select") or {}).get("options") or []
                names = {option.get("name") for option in current}
                new = [name for name in NOTION_STATUSES if name not in names]
                if new:
                    missing["Status"] = {"select": {"options": [*current, *({"name": name} for name in new)]}}
                    added += [f"Status: {name}" for name in new]
            if missing:
                self._call(client, "PATCH", f"/data_sources/{self._data_source_id}", {"properties": missing})
            return added

    def upsert(self, packet: Any, job: Any, assessment: Any | None, queue_date: str | None, outreach: Any | None = None) -> str:
        """Create or update one row. Updates never touch Status, so edits made in Notion survive.

        Contact columns are written only when there is an outreach draft (run `notion setup` once to add them).
        """
        with self._client_factory() as client:
            if self._title_property is None:
                self._schema(client)
            properties: dict[str, object] = {
                str(self._title_property): {"title": _text(job.title)},
                "Company": {"rich_text": _text(job.company)},
                "Location": {"rich_text": _text(job.location)},
                "Score": {"number": assessment.overall_score if assessment else None},
                "Apply URL": {"url": job.apply_url or job.canonical_url},
                "Queue Date": {"date": {"start": queue_date} if queue_date else None},
                "Packet": {"rich_text": _text(packet.packet_directory)},
                "JobAgent ID": {"number": job.id},
            }
            if self.with_country:
                properties["Country"] = {"rich_text": _text(job_country(job))}
            if outreach is not None:
                guess = (" · no email found" if not outreach.email
                         else " · UNVERIFIED email guess" if outreach.email_source == "pattern" else "")
                properties["Contact"] = {"rich_text": _text(f"{outreach.name} · {outreach.title}{guess}")}
                properties["Contact Email"] = {"email": outreach.email or None}
                properties["Outreach Draft"] = {"rich_text": _text(f"Subject: {outreach.subject}\n\n{outreach.body}")}
            if packet.notion_page_id:
                self._call(client, "PATCH", f"/pages/{packet.notion_page_id}", {"properties": properties})
                return str(packet.notion_page_id)
            properties["Status"] = {"select": {"name": "Ready"}}
            parent = {"type": "data_source_id", "data_source_id": self._data_source_id}
            return str(self._call(client, "POST", "/pages", {"parent": parent, "properties": properties})["id"])

    def set_status(self, page_id: str, status: str) -> None:
        """Write one row's Status ("recruiter_screen" -> "Recruiter Screen"), for changes SQLite learned first (email)."""
        with self._client_factory() as client:
            label = status.replace("_", " ").title()
            self._call(client, "PATCH", f"/pages/{page_id}", {"properties": {"Status": {"select": {"name": label}}}})

    def renumber(self, rank: dict[int, int]) -> int:
        """Set "No." to a dense 1..N over the live rows, ordered by ``rank`` (JobAgent ID -> position).

        Rows without a known JobAgent ID go last in creation order. Only changed rows are patched;
        returns how many. ponytail: Notion's unique_id can't be renumbered, so a Number column is used.
        """
        with self._client_factory() as client:
            if self._data_source_id is None:
                self._schema(client)
            pages: list[dict[str, Any]] = []
            body: dict[str, object] = {"page_size": 100}
            while True:
                result = self._call(client, "POST", f"/data_sources/{self._data_source_id}/query", body)
                pages += result.get("results", [])
                if not result.get("has_more"):
                    break
                body["start_cursor"] = result["next_cursor"]

            def key(page: dict[str, Any]) -> tuple[int, str]:
                job_id = (page.get("properties", {}).get("JobAgent ID") or {}).get("number")
                return rank.get(int(job_id), len(rank)) if isinstance(job_id, (int, float)) else len(rank), str(page.get("created_time", ""))

            changed = 0
            for number, page in enumerate(sorted(pages, key=key), start=1):
                if (page.get("properties", {}).get("No.") or {}).get("number") != number:
                    self._call(client, "PATCH", f"/pages/{page['id']}", {"properties": {"No.": {"number": number}}})
                    changed += 1
            return changed

    def statuses(self) -> dict[int, str]:
        """Read-only: JobAgent ID -> Status label for rows with a pulled Status (or the Applied box ticked)."""
        found: dict[int, str] = {}
        conditions: list[dict[str, object]] = [
            {"property": "Status", "select": {"equals": name}} for name in NOTION_PULLED_STATUSES
        ]
        with self._client_factory() as client:
            schema = self._schema(client)
            checkbox = (schema.get("Applied") or {}).get("type") == "checkbox"  # Master's own "Applied" tick box
            if checkbox:
                conditions.append({"property": "Applied", "checkbox": {"equals": True}})
            body: dict[str, object] = {"filter": {"or": conditions}, "page_size": 100}
            while True:
                result = self._call(client, "POST", f"/data_sources/{self._data_source_id}/query", body)
                for page in result.get("results", []):
                    properties = page.get("properties", {})
                    job_id = (properties.get("JobAgent ID") or {}).get("number")
                    status = ((properties.get("Status") or {}).get("select") or {}).get("name")
                    if status not in NOTION_PULLED_STATUSES:
                        status = "Applied" if checkbox and (properties.get("Applied") or {}).get("checkbox") else None
                    if isinstance(job_id, (int, float)) and status:
                        found[int(job_id)] = str(status)
                if not result.get("has_more"):
                    return found
                body["start_cursor"] = result["next_cursor"]


def notion_tracker() -> Tracker:
    """One dashboard for every job; the Country column tells US and international apart."""
    return NotionTracker(with_country=True)


def _text(value: str | None) -> list[dict[str, object]]:
    return [{"text": {"content": value[:2000]}}] if value else []


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None: self.token, self.chat_id = token or os.getenv("TELEGRAM_BOT_TOKEN"), chat_id or os.getenv("TELEGRAM_CHAT_ID")
    def send(self, text: str) -> None:
        if not self.token or not self.chat_id: raise RuntimeError("Telegram is not configured")
        response = httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": text[:4096]}, timeout=20); response.raise_for_status()


def digest_chunks(text: str, limit: int = 4096) -> list[str]:
    return [text[index:index + limit] for index in range(0, len(text), limit)] or [""]
