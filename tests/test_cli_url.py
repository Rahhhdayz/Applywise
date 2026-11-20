from pathlib import Path

from jobagent import cli
from jobagent.intake import IntakeError
from jobagent.intake.fetch import FetchedPage

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureService:
    def __init__(self, repository, fixture: str = "standard_json_ld.html") -> None:  # type: ignore[no-untyped-def]
        self.repository = repository
        self.fixture = fixture

    def import_url(self, url: str):  # type: ignore[no-untyped-def]
        page = FetchedPage(submitted_url=url, final_url=url, html=(FIXTURES / self.fixture).read_text())
        from jobagent.intake.extraction import extract_job_posting

        return self.repository.add_or_get_job(extract_job_posting(page))


def test_add_url_cli_reports_success_and_duplicate(tmp_path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "UrlIntakeService", FixtureService)
    database = tmp_path / "jobs.sqlite3"

    assert cli.main(["--database", str(database), "add-url", "https://jobs.example.com/openings/123"]) == 0
    assert "Added: job #1 — Example Corp / Security Engineer" in capsys.readouterr().out
    assert cli.main(["--database", str(database), "add-url", "https://jobs.example.com/openings/123"]) == 0
    assert "Already exists: job #1" in capsys.readouterr().out


def test_add_url_cli_reports_expected_extraction_failures(tmp_path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FailingService:
        def __init__(self, repository) -> None:  # type: ignore[no-untyped-def]
            pass

        def import_url(self, url: str):  # type: ignore[no-untyped-def]
            raise IntakeError("Missing required field: company")

    monkeypatch.setattr(cli, "UrlIntakeService", FailingService)

    assert cli.main(["--database", str(tmp_path / "jobs.sqlite3"), "add-url", "https://example.com"]) == 1
    assert capsys.readouterr().out == "Could not extract a valid job posting:\nMissing required field: company\n"
