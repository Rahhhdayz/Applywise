import argparse
from types import SimpleNamespace

import pytest

from jobagent import cli


def _run(monkeypatch: pytest.MonkeyPatch, tracker: object) -> list[list[int]]:
    synced: list[list[int]] = []
    job = SimpleNamespace(id=7)
    packet = SimpleNamespace(packet_directory="out/x", cover_letter_pdf_path="c.pdf")
    repository = SimpleNamespace(get_job=lambda job_id: job, get_eligibility=lambda *a: SimpleNamespace(status="eligible"))
    monkeypatch.setattr(cli, "_load_candidate_or_report", lambda path: object())
    monkeypatch.setattr(cli, "rules_fingerprint", lambda candidate: "fp")
    monkeypatch.setattr(cli, "_cover_letter_writer", lambda: None)
    monkeypatch.setattr(cli, "load_writing_settings", lambda: SimpleNamespace(max_packets_per_run=20))
    monkeypatch.setattr(cli, "PreparationService", lambda *a, **k: SimpleNamespace(prepare=lambda *a, **k: (packet, None)))
    monkeypatch.setattr(cli, "notion_tracker", tracker)
    monkeypatch.setattr(cli, "sync_to_notion", lambda repo, t, ids: synced.append(ids) or {"synced": len(ids), "errors": {}})
    args = argparse.Namespace(job_id=7, date=None, dry_run=False, limit=None, force=False)
    assert cli._run_prepare(repository, args) == 0  # type: ignore[arg-type]
    return synced


def test_prepare_for_a_picked_job_syncs_it_to_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, lambda: object()) == [[7]]  # manual jobs are never queued, so prepare syncs them


def test_prepare_survives_notion_being_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> object:
        raise RuntimeError("NOTION_TOKEN missing")

    assert _run(monkeypatch, unavailable) == []  # packet still counts as ready; exit code stays 0
