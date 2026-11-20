import httpx

from jobagent.intake.oracle import enrich_oracle_posting
from jobagent.models import JobPosting

URL = "https://fa-x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/42"


def test_oracle_description_comes_from_requisition_api(monkeypatch) -> None:
    monkeypatch.setattr("jobagent.intake.oracle.validate_public_url", lambda url: url)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        item = {"ExternalDescriptionStr": "<p>Protect <b>clients</b></p>", "PrimaryLocation": "Philadelphia, PA"}
        return httpx.Response(200, json={"items": [item]})

    posting = JobPosting(source="manual_url", company="Vandelay", title="Associate", canonical_url=URL, apply_url=URL)
    enriched = enrich_oracle_posting(posting, URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "Protect" in (enriched.description_text or "") and "clients" in (enriched.description_text or "")
    assert enriched.location == "Philadelphia, PA"
    assert 'Id=%2242%22' in seen[0] or 'Id="42"' in seen[0]


def test_non_oracle_urls_are_untouched() -> None:
    posting = JobPosting(source="manual_url", company="X", title="Y", canonical_url="https://x.test/1", apply_url="https://x.test/1")
    assert enrich_oracle_posting(posting, "https://x.test/1") is posting
