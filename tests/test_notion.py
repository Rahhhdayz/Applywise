"""NotionTracker against httpx.MockTransport (Notion-Version 2025-09-03 data sources)."""

from __future__ import annotations

import json

import httpx

from jobagent.db import JobRepository
from jobagent.integrations import (
    NOTION_PROPERTIES,
    NotionTracker,
    notion_tracker,
)
from jobagent.pipeline import sync_to_notion
from jobagent.scoring import AssessmentService
from tests.test_milestone5 import FakeScorer, candidate, posting


class FakeNotion:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.properties = {"Name": {"type": "title"}, "Company": {"type": "rich_text"}}
        self.rows: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if request.method == "GET" and path == "/v1/databases/db-1":
            return httpx.Response(200, json={"object": "database", "data_sources": [{"id": "ds-1"}]})
        if request.method == "GET" and path == "/v1/data_sources/ds-1":
            return httpx.Response(200, json={"properties": self.properties})
        if request.method == "PATCH" and path == "/v1/data_sources/ds-1":
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/v1/pages":
            return httpx.Response(200, json={"id": "page-1"})
        if request.method == "POST" and path == "/v1/data_sources/ds-1/query":
            return httpx.Response(200, json={"results": self.rows, "has_more": False})
        if request.method == "PATCH" and path.startswith("/v1/pages/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        return httpx.Response(404, json={"message": "unexpected"})

    def tracker(self) -> NotionTracker:
        return NotionTracker(
            "secret", "db-1",
            client_factory=lambda: httpx.Client(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(self.handler)),
        )


def test_setup_adds_only_missing_properties_and_keeps_title() -> None:
    notion = FakeNotion()
    added = notion.tracker().setup()
    assert added == sorted(set(NOTION_PROPERTIES) - {"Company"})
    method, path, body = notion.requests[-1]
    assert (method, path) == ("PATCH", "/v1/data_sources/ds-1")
    assert "Company" not in body["properties"] and "Name" not in body["properties"]


def test_first_sync_creates_page_second_patches_without_status() -> None:
    repository = JobRepository(":memory:")
    repository.initialize()
    job = repository.add_or_get_job(posting("n-1")).job
    AssessmentService(repository, FakeScorer(), candidate()).evaluate(job)
    repository.add_queue_entries("2026-09-22", [{"job_id": job.id, "rank": 1, "ranking_score": 1.0, "exceptional": False, "reason": "t"}])
    repository.save_packet(job_id=job.id, fingerprint="f", status="ready", packet_directory="out/2026-09-22/x")
    notion = FakeNotion()
    tracker = notion.tracker()

    assert sync_to_notion(repository, tracker, [job.id])["synced"] == 1
    method, path, body = [r for r in notion.requests if not r[1].endswith('/query')][-1]  # last is the renumber query
    assert (method, path) == ("POST", "/v1/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    assert body["properties"]["Name"]["title"][0]["text"]["content"] == "Security Engineer"
    assert body["properties"]["Status"] == {"select": {"name": "Ready"}}
    assert body["properties"]["Score"] == {"number": 90}
    assert body["properties"]["Queue Date"] == {"date": {"start": "2026-09-22"}}
    assert repository.get_latest_ready_packet(job.id).notion_page_id == "page-1"  # type: ignore[union-attr]

    assert sync_to_notion(repository, tracker, [job.id])["synced"] == 1
    method, path, body = [r for r in notion.requests if not r[1].endswith('/query')][-1]  # last is the renumber query
    assert (method, path) == ("PATCH", "/v1/pages/page-1")
    assert "Status" not in body["properties"]


def test_http_failure_is_reported_per_row() -> None:
    repository = JobRepository(":memory:")
    repository.initialize()
    job = repository.add_or_get_job(posting("n-2")).job
    repository.save_packet(job_id=job.id, fingerprint="f", status="ready")

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    tracker = NotionTracker("bad", "db-1", client_factory=lambda: httpx.Client(base_url="https://api.notion.com/v1", transport=httpx.MockTransport(refuse)))
    result = sync_to_notion(repository, tracker, [job.id])
    assert result["synced"] == 0 and "401" in result["errors"][job.id]  # type: ignore[index]


def test_statuses_reads_status_select_and_applied_checkbox() -> None:
    notion = FakeNotion()
    notion.properties = {**notion.properties, "Applied": {"type": "checkbox"}, "Status": {"type": "select"}}

    def row(job_id: int, status: str, ticked: bool) -> dict:
        return {"properties": {
            "JobAgent ID": {"number": job_id},
            "Status": {"select": {"name": status}},
            "Applied": {"checkbox": ticked},
        }}

    notion.rows = [row(1, "Ready", True), row(2, "Skipped", False), row(3, "Rejected", True)]
    assert notion.tracker().statuses() == {1: "Applied", 2: "Skipped", 3: "Rejected"}
    query = next(body for method, path, body in notion.requests if path.endswith("/query"))
    assert {"property": "Applied", "checkbox": {"equals": True}} in query["filter"]["or"]


def test_row_deleted_in_notion_marks_the_job_skipped_instead_of_failing() -> None:
    repository = JobRepository(":memory:")
    repository.initialize()
    job = repository.add_or_get_job(posting("gone")).job
    packet = repository.save_packet(job_id=job.id, fingerprint="f", status="ready", packet_directory="out/x")
    repository.set_packet_notion_page(packet.id, "page-gone")

    class Trashed:
        def upsert(self, *args):  # type: ignore[no-untyped-def]
            raise RuntimeError("Notion PATCH /pages/page-gone failed: HTTP 400 Can't edit block that is archived.")

        def renumber(self, rank: dict[int, int]) -> int:
            return 0

    result = sync_to_notion(repository, Trashed(), [job.id])  # type: ignore[arg-type]
    assert result["failed"] == 0 and result["removed_in_notion"] == 1
    assert repository.latest_statuses()[job.id] == "skipped"
    assert sync_to_notion(repository, Trashed(), [job.id])["removed_in_notion"] == 0  # not retried


def test_one_dashboard_carries_the_country_column(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    monkeypatch.setenv("NOTION_DATABASE_ID", "main-db")
    tracker = notion_tracker()
    assert isinstance(tracker, NotionTracker) and tracker.database_id == "main-db" and tracker.with_country


def test_renumber_is_dense_in_rank_order_and_patches_only_changes() -> None:
    notion = FakeNotion()
    row = lambda page, job, no: {"id": page, "created_time": page, "properties": {"JobAgent ID": {"number": job}, "No.": {"number": no}}}
    notion.rows = [row("p-a", 30, 7), row("p-b", 10, 1), row("p-c", None, None), row("p-d", 20, 2)]
    assert notion.tracker().renumber({10: 0, 20: 1, 30: 2}) == 2  # job 10 already 1, job 20 already 2
    patches = {path.rsplit("/", 1)[-1]: body["properties"]["No."]["number"] for method, path, body in notion.requests if method == "PATCH"}
    assert patches == {"p-a": 3, "p-c": 4}  # gap at 7 closed; row without JobAgent ID goes last
