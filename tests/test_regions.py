from types import SimpleNamespace as Job

from jobagent.regions import US, is_international, job_country


def test_scrape_country_wins_over_location() -> None:
    assert job_country(Job(source_metadata={"country": "japan"}, location="Remote")) == "Japan"
    assert job_country(Job(source_metadata={"country": "USA"}, location="Paris, TX")) == US


def test_location_fallback_and_us_default() -> None:
    assert job_country(Job(source_metadata={}, location="Sydney, New South Wales, Australia")) == "Australia"
    assert job_country(Job(source_metadata=None, location="Paris, Île-de-France")) == "France"
    assert job_country(Job(source_metadata={}, location="Tokyo")) == "Japan"
    assert job_country(Job(source_metadata={}, location="Paris, A8, FR")) == "France"
    assert job_country(Job(source_metadata={}, location="South Sydney, NSW, AU")) == "Australia"
    assert job_country(Job(source_metadata={}, location="Boston, MA")) == US
    assert job_country(Job(source_metadata={}, location="Melbourne, FL, US")) == US
    assert job_country(Job(source_metadata={}, location="Paris, TX")) == US
    assert not is_international(Job(source_metadata={}, location=None))
