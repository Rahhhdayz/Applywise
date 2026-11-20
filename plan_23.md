# Plan: Simplify JobAgent into one scheduled pipeline built on python-jobspy

## Context
Discovery currently has two separate systems: ATS board polling (`jobagent/discovery/`: Greenhouse/Lever/Ashby plus a source registry) and staged broad search (`jobagent/broad/`: Adzuna/Jooble → candidates → a 5-step resolver → Codex web-search locator). That's a lot of code for the job it does. Master wants the pipeline shape from the n8n screenshot: **schedule → load resume → scrape jobs → check relevance → tailor resume → save**, with discovery done by `python-jobspy` (LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter).

Decisions (confirmed):
- Keep the Python app. **SQLite stays the system of record; Notion is Master's view** (no Google Sheets/Docs).
- Scrape all five sites, LinkedIn included. Master accepted the block/ToS risk; the plan limits how hard we hit each site (see Step 2).
- Keep the **deterministic eligibility pre-filter** before any AI call.
- Keep **grounded resume writing** (Skill + evidence-ID validation in `jobagent/writing/`). The AI must never invent experience.

Unchanged: `JobPosting` model, normalization, repository dedup, eligibility, Codex scoring, ranking, grounded writing, `apply fill`, URL intake (`add-url`).

Bookkeeping: mirror the plan into `tasks/todo.md` at start and add a review section at the end.

## Target pipeline (`jobagent run cycle`, scheduled via existing launchd `schedule generate`)
```
load candidate truth ("Get Resume")      fail fast if invalid
  → scrape (jobspy, per search-profile query, all sites)
  → normalize + dedup → SQLite (existing add_or_get_job)
  → eligibility (deterministic, all jobs; cheap)
  → score eligible/review jobs with Codex (cached; max_jobs_per_run)   ("Check relevance")
  → build today's queue (min AI score; new jobs only)                  ("Filter")
  → prepare grounded packets for queued jobs (max_packets_per_run)     ("Customise resume")
  → Notion sync of ready packets                                       ("Add to DB")
```
If one step fails, later steps still run on the data already in SQLite. Per-job failures are already isolated. Metrics go into the existing `workflow_runs` table. `--dry-run` scrapes and runs eligibility only; it skips AI scoring, writing and Notion.

---

## Step 1 — Delete the old discovery layer (the simplification)
- Delete `jobagent/discovery/` and all of `jobagent/broad/` except the search-profile pieces, which move (see Step 2).
- Delete the CLI commands `sources`, `discover`, `search`, `candidates`, `import-urls` and their handlers in `jobagent/cli.py`.
- `jobagent/intake/service.py`: remove auto board learning (`derive_source_target` → `register_source`). Keep `add-url` and `intake/urls.py`, which `apply` uses (`detect_source`, `validate_public_url`).
- `jobagent/db/`:
  - Remove `SourceTargetRecord`, `DiscoveryRunRecord`, `DiscoveryCandidateRecord`, `BroadSearchRunRecord` and their repository methods and dataclasses (sources, discovery runs, candidates, broad runs, `mark_missing_observations_inactive`, `adzuna_resolution_metrics`).
  - Turn `JobDiscoveryRecord.source_target_id` / `last_observed_run_id` into plain nullable `Integer` columns, because their FK targets are removed.
  - Existing SQLite files keep the old tables, orphaned and harmless, so no migration is needed.
- Delete the tests and fixtures for removed code: `test_adzuna_resolution_chain`, `test_broad_*`, `test_cli_broad`, `test_codex_web_search`, `test_discovery_*`, `test_cli_sources`, `test_source_registry`, `test_intake_source_learning`, and the adzuna/jooble/greenhouse/lever/ashby JSON fixtures.
- `.env.example`, `config/settings.example.yaml`: remove the Adzuna, Jooble, Codex-web-search and `providers:` keys.

## Step 2 — New `jobagent/scrape/` (3 small files)
- **`profiles.py`**: move `SearchProfile`, `SearchQuery`, `load_search_profiles` and `expand_queries` here unchanged from `broad/models.py` / `broad/profiles.py`, and rename the error to `ScrapeError`. The existing `config/search_profiles.example.yaml` keeps working.
- **`jobspy_source.py`**: `scrape_query(query, settings, *, scrape_fn=jobspy.scrape_jobs) -> list[JobPosting]`. The DataFrame is converted with `.to_dict("records")` right at this boundary, so no pandas type leaks past the adapter (AGENTS.md: no source-specific structure outside adapters). Field mapping per row, using jobspy's `desired_order` columns:
  - **Source identity:** `source=site` (`linkedin`/`indeed`/`glassdoor`/`google`/`zip_recruiter`), `source_job_id=id`. Rows missing company or title are skipped and counted.
  - **Canonical and apply URL:** `canonicalize_url(job_url_direct)` when present, else `job_url`. The direct employer/ATS URL is what makes the same job found on different boards dedup automatically, through the repository's existing exact-canonical-URL match. It also lets `apply fill` work on Greenhouse/Lever/Ashby direct links.
  - **Employment type:** `fulltime→full-time`, `parttime→part-time`, `internship`, `contract`. Multiple values → `None`, which eligibility turns into `review` rather than a wrong rejection. This matters: raw `fulltime` would be hard-rejected against `employment_types: [full_time]`.
  - **Location and date:** `location`; `"Remote"` if `is_remote` is set and there is no location. `date_posted` → UTC midnight.
  - **Description:** markdown, truncated to `intake.extraction.MAX_DESCRIPTION_CHARS`. It is stored as inert text only.
  - **Missing values:** pandas `NaN`/`NaT` → `None` via one `_clean()` helper.
  - **`source_metadata`:** board `job_url`, salary min/max/interval/currency, `is_remote`, `job_level`, `search_profile`, `discovered_via`.
  - **Collapsing duplicates within one scrape:** rows are collapsed by canonical URL, or by normalized (company, title, location) when there is no direct URL. All sites are recorded in `discovered_via`. Mark this with a `ponytail:` comment naming its limit: two genuinely different requisitions with identical fields in the same scrape would be merged. Across separate runs the repository keeps its existing no-similarity-merge rule.
- **`service.py`**: `ScrapeService(repository, *, scrape_fn, sleep=time.sleep).run(profiles) -> ScrapeSummary(received, new, existing, updated, skipped, errors)`. It makes one jobspy call per query covering all configured sites. If a query fails (for example a site blocks us), it records the error and continues with the next query, with no retry within the same run. It waits `delay_seconds` between queries, as the jobspy README recommends.
- **Scrape settings:** a `scrape:` section in `config/settings.yaml`, loaded exactly like `jobagent/writing/config.py`:
  - `sites` (default: all five)
  - `results_wanted` (25 per site)
  - `delay_seconds` (5)
  - `country_indeed` (USA)
  - `linkedin_fetch_description` (true: the full description is needed for eligibility/scoring and gives the direct URL)
  - `hours_old` comes from the profile's `freshness_days * 24`; `is_remote` is set only when the profile's `remote: only`
  - `google_search_term` is built as `"{keyword} jobs in {location}"`
  - No proxies and no login.
- **Dependency:** add `python-jobspy` to `pyproject.toml`. It pins `numpy==1.26.3`, which only has wheels up to cp312, so set `requires-python = ">=3.12,<3.13"` (the venv is 3.12.3).

## Step 3 — Queue only new work
In `_run_queue_command`, skip jobs that are already in an earlier day's queue or have any application status (prefilled/applied/skipped/…). Without this, a daily pipeline re-queues the same top jobs every day. Add `repository.tracked_job_ids()` (a union of queue job_ids and status job_ids). Move the queue-building body into `build_queue(repository, candidate, date)` in `jobagent/ranking.py` so the CLI and the pipeline share it.

## Step 4 — `jobagent/pipeline.py` + `run cycle`
`run_cycle(repository, candidate, *, scrape, scorer, writer, tracker, today, dry_run) -> dict[str, object]` runs the steps above with every dependency injected, so tests can pass fakes. It reuses `EligibilityEngine`, `AssessmentService`, `build_queue`, `PreparationService` and `load_writing_settings`. `cli._run_cycle` wires in the real implementations inside the existing `WorkflowLock` and records the metrics, replacing today's stub. The digest (Telegram) stays optional and separate.

## Step 5 — Notion as Master's view (currently a stub)
`jobagent/integrations.py` `NotionTracker`, updated for Notion-Version 2025-09-03:
- **Resolve the data source:** `GET /v1/databases/{NOTION_DATABASE_ID}` → `data_sources[0].id`.
- **`setup()`:** read the data-source schema and `PATCH /v1/data_sources/{id}` to add any missing properties: Company, Location (rich_text), Score (number), Status (select: Ready/Prefilled/Applied/Skipped/Rejected), Apply URL (url), Queue Date (date), Packet (rich_text), JobAgent ID (number). It reuses whatever the database's existing title property is called (often "Name") instead of renaming it.
- **`upsert(packet, job, assessment, queue_date)`:**
  - New page: `POST /v1/pages` with `parent: {data_source_id}`, then save the page id with the existing `repository.set_packet_notion_page`.
  - Existing page: `PATCH` it, but never overwrite **Status**, so Master's manual edits in Notion survive.
- **Testability:** an injectable `httpx.Client` factory, so tests use `httpx.MockTransport`.
- **CLI:** `jobagent notion setup` and `jobagent notion sync [--job-id]` replace the current "requires configuration" stub.
- Pulling Status back from Notion into SQLite is deferred. SQLite never depends on it.

## Step 6 — CLI & docs
- **New commands:** `jobagent scrape profiles` and `jobagent scrape run (--profile X | --all) [--results N]`, which print a `ScrapeSummary`.
- **README:** replace the "Source registry" and "Broad-market discovery" sections with a short "Scraping (jobspy)" and "Daily pipeline" section. State the limits we put on scraping, and that LinkedIn may block.
- **AGENTS.md:** update the "External sources" list to: jobspy boards plus manual URLs.

## Tests (all offline; jobspy is never called for real)
- `tests/test_scrape_jobspy.py`: a fake `scrape_fn` returns a small pandas DataFrame. Covers:
  - field mapping, including `fulltime` → accepted by eligibility, and multiple types → `review`
  - NaN → None
  - direct-URL canonicalization, and cross-site duplicates collapsing with `discovered_via`
  - rows missing company skipped
  - prompt-injection description stored inert
- `tests/test_scrape_service.py`: one failing query doesn't stop the others; `sleep` is called between queries; a second run reports `existing`.
- `tests/test_pipeline.py`: with fakes, the full cycle calls every step in order, and a scrape failure still lets scoring/queue/prepare/Notion run on existing jobs. `dry_run` skips scorer, writer and tracker.
- Queue test: jobs queued on an earlier day or already `prefilled`/`applied` are excluded.
- `tests/test_notion.py` (MockTransport): setup adds only the missing properties and keeps the existing title property; the first sync creates a page with a `data_source_id` parent and stores its id; the second sync patches it without touching Status.
- Keep all remaining tests green (eligibility, scoring, writing, apply, intake, repository).

## Verification
1. `uv sync`, then `uv run ruff check .` and `uv run pytest -q`: all green.
2. `uv run jobagent doctor --offline` and `uv run jobagent run cycle --dry-run` (no AI).
3. Live, run by Master (this sandbox can't reach job boards or api.notion.com):
   - `uv run jobagent scrape run --profile security-engineering --results 5`, then `list-jobs`, to confirm rows from several sites and at least one cross-site duplicate collapsed
   - `uv run jobagent notion setup`, then `uv run jobagent run cycle`, to confirm the Notion database gets rows for ready packets
