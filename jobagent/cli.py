"""JobAgent local CLI."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from jobagent.apply.answers import ClaudeAnswerDrafter
from jobagent.apply.ats import ApplicationForm, application_form
from jobagent.apply.filler import (
    DEFAULT_PROFILE_DIRECTORY,
    FillReport,
    TabJob,
    fill_application_form,
    fill_in_tabs,
)
from jobagent.apply.oversight import Oversight, unattended_summary
from jobagent.apply.profile import (
    ApplicationConfigurationError,
    ApplicationProfile,
    load_application_profile,
)
from jobagent.apply.review import (
    RUNTIME_MODEL,
    RUNTIME_TIMEOUT_SECONDS,
    TRAINING_MODEL,
    ClaudeReviewer,
    format_findings,
    parse_pasted_review,
    review_prompt,
    save_review,
    validate_review,
)
from jobagent.apply.snapshot import SNAPSHOT_ROOT, load_snapshot
from jobagent.apply.training import fixtures_for_job, streak, write_fixtures
from jobagent.candidate import CandidateConfigurationError, load_candidate_truth
from jobagent.candidate.models import CandidateTruth
from jobagent.db import JobRepository, StoredJob
from jobagent.eligibility import refresh_eligibility, rules_fingerprint
from jobagent.environment import load_local_env
from jobagent.followup import ClaudeFollowupWriter, FollowupService
from jobagent.inbox import (
    ClaudeEmailClassifier,
    InboxError,
    InboxResult,
    adopt_email,
    applied_jobs,
    gmail_filter_xml,
    reader_from_env,
    run_inbox,
)
from jobagent.intake import IntakeError, UrlIntakeService
from jobagent.intake.resolve import company_hint, resolve_application_url, resolve_destination
from jobagent.intake.urls import detect_source
from jobagent.integrations import (
    TelegramNotifier,
    digest_chunks,
    notion_tracker,
)
from jobagent.liveness import job_closed
from jobagent.models import JobPosting
from jobagent.outcomes import (
    FOLLOWUP_AFTER_DAYS,
    FUNNEL,
    GHOSTED_AFTER_DAYS,
    due_for_followup,
    funnel,
    quiet_applications,
)
from jobagent.outreach import ClaudeOutreachWriter, HunterClient, OutreachService, signature
from jobagent.pipeline import pull_notion_statuses, run_cycle, run_outreach, sync_to_notion
from jobagent.ranking import build_queue
from jobagent.runtime import WorkflowLock, bot_launchd_plist, doctor, launchd_plist
from jobagent.scoring import AssessmentService, CodexCliJobScorer, score_batch
from jobagent.scrape.jobspy_source import load_scrape_settings
from jobagent.scrape.profiles import ScrapeError, load_search_profiles
from jobagent.scrape.service import ScrapeService
from jobagent.telegram_bot import run_bot
from jobagent.writing.config import load_writing_settings
from jobagent.writing.cover_letter import ClaudeCoverLetterWriter
from jobagent.writing.service import PreparationService

DEFAULT_DATABASE = "data/jobagent.sqlite3"
FILL_REPORTS = Path("logs/fill_reports")


def _default_profiles() -> str:
    local = Path("config/search_profiles.yaml")
    return str(local if local.is_file() else Path("config/search_profiles.example.yaml"))


def demo_job() -> JobPosting:
    """Return a fixed job used to demonstrate persistence and deduplication."""
    return JobPosting(
        source="demo",
        source_job_id="demo-software-engineer-001",
        company="Example Labs, Inc.",
        title="Software Engineer",
        location="New York, NY",
        employment_type="full-time",
        description_text="A deterministic local demonstration job.",
        apply_url="https://careers.example.test/jobs/demo-software-engineer-001",
        canonical_url="https://careers.example.test/jobs/demo-software-engineer-001",
        posted_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_metadata={"kind": "demo"},
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser."""
    parser = argparse.ArgumentParser(prog="jobagent", description="JobAgent local CLI")
    parser.add_argument(
        "--database",
        default=os.environ.get("JOBAGENT_DATABASE", DEFAULT_DATABASE),
        help="SQLite database path (default: data/jobagent.sqlite3)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="Create the SQLite schema")
    subcommands.add_parser("add-demo-job", help="Insert the fixed demo job")
    add_url = subcommands.add_parser("add-url", help="Fetch and import a public job URL")
    add_url.add_argument("url", help="Public http(s) job URL")
    resolve = subcommands.add_parser("resolve", help="Resolve pending jobs to verified ATS destinations")
    resolve.add_argument("--pending", action="store_true", required=True)
    resolve.add_argument("--limit", type=int)
    resolve.add_argument("--dry-run", action="store_true", help="List pending counts without fetching or saving")
    scrape = subcommands.add_parser("scrape", help="Scrape job boards with python-jobspy")
    scrape_commands = scrape.add_subparsers(dest="scrape_command", required=True)
    scrape_profiles = scrape_commands.add_parser("profiles", help="List configured search profiles")
    scrape_profiles.add_argument("--config", default=_default_profiles())
    scrape_run = scrape_commands.add_parser("run", help="Scrape jobs for one or all profiles")
    scrape_run.add_argument("--config", default=_default_profiles())
    scrape_group = scrape_run.add_mutually_exclusive_group(required=True)
    scrape_group.add_argument("--profile")
    scrape_group.add_argument("--all", action="store_true", dest="all_profiles")
    scrape_run.add_argument("--results", type=int, help="Results wanted per site, per query")
    candidate = subcommands.add_parser("candidate", help="Validate candidate truth configuration")
    candidate_commands = candidate.add_subparsers(dest="candidate_command", required=True)
    candidate_validate = candidate_commands.add_parser("validate", help="Validate candidate configuration")
    candidate_validate.add_argument("--directory", default="candidate")
    eligibility = subcommands.add_parser("eligibility", help="Run deterministic eligibility rules")
    eligibility_commands = eligibility.add_subparsers(dest="eligibility_command", required=True)
    eligibility_run = eligibility_commands.add_parser("run", help="Evaluate canonical jobs")
    eligibility_run.add_argument("--job-id", type=int)
    eligibility_run.add_argument("--candidate-directory", default="candidate")
    evaluate = subcommands.add_parser("evaluate", help="Run semantic scoring for eligible or reviewable jobs")
    evaluate_group = evaluate.add_mutually_exclusive_group(required=True)
    evaluate_group.add_argument("--job-id", type=int)
    evaluate_group.add_argument("--eligible", action="store_true")
    evaluate.add_argument("--limit", type=int)
    evaluate.add_argument("--force", action="store_true")
    evaluate.add_argument("--dry-run", action="store_true")
    evaluate.add_argument("--candidate-directory", default="candidate")
    queue = subcommands.add_parser("queue", help="Build or inspect the deterministic daily queue")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_build = queue_commands.add_parser("build", help="Build today's queue")
    queue_build.add_argument("--date")
    queue_build.add_argument("--candidate-directory", default="candidate")
    queue_show = queue_commands.add_parser("show", help="Show a daily queue")
    queue_show.add_argument("--date")
    jobs = subcommands.add_parser("jobs", help="Inspect canonical jobs")
    jobs_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_explain = jobs_commands.add_parser("explain", help="Explain eligibility, scoring, and queue state")
    jobs_explain.add_argument("job_id", type=int)
    subcommands.add_parser("setup", help="Initialize safe local V1 state")
    doctor_parser = subcommands.add_parser("doctor", help="Inspect local runtime prerequisites")
    doctor_parser.add_argument("--offline", action="store_true")
    prepare = subcommands.add_parser("prepare", help="Prepare validated packets; never submit applications")
    prepare_group = prepare.add_mutually_exclusive_group(required=True); prepare_group.add_argument("--job-id", type=int); prepare_group.add_argument("--queue", action="store_true")
    prepare.add_argument("--date"); prepare.add_argument("--limit", type=int); prepare.add_argument("--force", action="store_true"); prepare.add_argument("--dry-run", action="store_true")
    apply_cmd = subcommands.add_parser("apply", help="Pre-fill ATS application forms for human review; never submits")
    apply_commands = apply_cmd.add_subparsers(dest="apply_command", required=True)
    apply_fill = apply_commands.add_parser("fill", help="Open a job's application form and pre-fill known fields")
    apply_fill_group = apply_fill.add_mutually_exclusive_group(required=True)
    apply_fill_group.add_argument("--job-id", type=int, nargs="+")
    apply_fill_group.add_argument("--queue", action="store_true")
    apply_fill_group.add_argument("--ready", action="store_true", help="Every ready packet not yet applied/prefilled/skipped")
    apply_fill.add_argument("--advance", action="store_true", help=argparse.SUPPRESS)  # deprecated no-op: Master clicks Continue
    apply_fill.add_argument("--tabs", action="store_true", help="One browser, one tab per single-page form; no sign-in forms")
    apply_fill.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIRECTORY, help="Browser profile directory")
    apply_fill.add_argument("--date")
    apply_fill.add_argument("--limit", type=int)
    apply_fill.add_argument("--slow-mo", type=float, default=0, help="Milliseconds between browser actions, to watch the fill")
    _fill_switches(apply_fill)
    apply_train = apply_commands.add_parser("train", help="Training round: practice fill (auto Next/Continue, never Submit) "
                                            "with a Sonnet review of every step; prints the streak")
    apply_train.add_argument("--job-id", type=int, nargs="+", required=True)
    apply_train.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIRECTORY, help="Browser profile directory")
    apply_train.add_argument("--slow-mo", type=float, default=0, help="Milliseconds between browser actions, to watch the fill")
    _fill_switches(apply_train, training=True)
    apply_url = apply_commands.add_parser("url", help="Import a job link, find its real application form, prepare, sync Notion and pre-fill")
    apply_url.add_argument("url")
    apply_url.add_argument("--force", action="store_true", help="Continue even if this opening was already applied/prefilled")
    apply_url.add_argument("--advance", action="store_true", help=argparse.SUPPRESS)  # deprecated no-op: Master clicks Continue
    apply_url.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIRECTORY, help="Browser profile directory")
    apply_url.add_argument("--slow-mo", type=float, default=0, help="Milliseconds between browser actions, to watch the fill")
    _fill_switches(apply_url)
    apply_review = apply_commands.add_parser("review", help="AI-review a fill's saved step snapshots (restricted Claude, no tools)")
    apply_review.add_argument("--job-id", type=int, required=True)
    apply_review.add_argument("--step", type=int, help="One step (default: every step)")
    apply_review.add_argument("--model", default=TRAINING_MODEL, help="Claude model (training default: sonnet)")
    apply_prompt = apply_commands.add_parser("review-prompt", help="Print a step's review prompt for a Cowork agent (ChatGPT/Claude in Chrome)")
    apply_prompt.add_argument("--job-id", type=int, required=True)
    apply_prompt.add_argument("--step", type=int, help="Default: the newest step")
    apply_findings = apply_commands.add_parser("findings", help="Save findings JSON pasted back from a Cowork agent (validated)")
    apply_findings.add_argument("--job-id", type=int, required=True)
    apply_findings.add_argument("--step", type=int, required=True)
    apply_findings.add_argument("--file", type=Path, help="JSON file (default: read stdin)")
    apply_fixture = apply_commands.add_parser("fixture", help="Turn a job's wrong findings into tests/fixtures/fill_findings/*.json")
    apply_fixture.add_argument("--job-id", type=int, required=True)
    apply_streak = apply_commands.add_parser("streak", help="Training progress: Workday tenants in a row with zero wrong findings")
    apply_streak.add_argument("--n", type=int, default=5)
    apply_track = apply_commands.add_parser("track", help="Import job links into Notion only (no resume, cover letter or form fill)")
    apply_track.add_argument("urls", nargs="+")
    outreach = subcommands.add_parser("outreach", help="Find a contact and draft an email for prepared jobs; never sends")
    outreach_group = outreach.add_mutually_exclusive_group(required=True)
    outreach_group.add_argument("--job-id", type=int, nargs="+")
    outreach_group.add_argument("--queue", action="store_true")
    outreach.add_argument("--date"); outreach.add_argument("--force", action="store_true", help="Look up and draft again")
    notion = subcommands.add_parser("notion", help="Sync prepared packet tracking")
    notion_commands = notion.add_subparsers(dest="notion_command", required=True); notion_commands.add_parser("setup"); notion_commands.add_parser("pull", help="Copy Status changes (Applied, Interview, Offer, Rejected...) from Notion"); notion_sync = notion_commands.add_parser("sync"); notion_sync.add_argument("--job-id", type=int)
    notify = subcommands.add_parser("notify", help="Send bounded Telegram notifications"); notify_commands = notify.add_subparsers(dest="notify_command", required=True); notify_commands.add_parser("test")
    digest = subcommands.add_parser("digest", help="Generate/send deterministic daily digest"); digest_commands = digest.add_subparsers(dest="digest_command", required=True); digest_commands.add_parser("send")
    status = subcommands.add_parser("status", help="Set application lifecycle status"); status_commands = status.add_subparsers(dest="status_command", required=True); status_set = status_commands.add_parser("set"); status_set.add_argument("job_id", type=int); status_set.add_argument("state")
    stats = subcommands.add_parser("stats", help="Local factual lifecycle analytics"); stats_commands = stats.add_subparsers(dest="stats_command", required=True); stats_commands.add_parser("summary")
    followup = subcommands.add_parser("followup", help="Draft follow-ups on quiet applications; never sends")
    followup_commands = followup.add_subparsers(dest="followup_command", required=True)
    followup_list = followup_commands.add_parser("list", help="Applications due for a follow-up")
    followup_list.add_argument("--days", type=int, default=FOLLOWUP_AFTER_DAYS)
    followup_draft = followup_commands.add_parser("draft", help="Draft the next follow-up (all due jobs, or --job-id)")
    followup_draft.add_argument("--job-id", type=int, nargs="+"); followup_draft.add_argument("--days", type=int, default=FOLLOWUP_AFTER_DAYS)
    followup_sent = followup_commands.add_parser("sent", help="Record that you sent the latest follow-up")
    followup_sent.add_argument("job_id", type=int)
    outcome = subcommands.add_parser("outcome", help="Close out applications that went quiet")
    outcome_commands = outcome.add_subparsers(dest="outcome_command", required=True)
    sweep = outcome_commands.add_parser("sweep", help="Propose 'ghosted' for long-quiet applications")
    sweep.add_argument("--days", type=int, default=GHOSTED_AFTER_DAYS); sweep.add_argument("--yes", action="store_true", help="Apply the proposals")
    run = subcommands.add_parser("run", help="Run safe workflow orchestration"); run_commands = run.add_subparsers(dest="run_command", required=True); cycle = run_commands.add_parser("cycle"); cycle.add_argument("--dry-run", action="store_true"); cycle.add_argument("--config", default=_default_profiles())
    schedule = subcommands.add_parser("schedule", help="Render/install macOS launchd schedules"); schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True); schedule_commands.add_parser("generate"); schedule_commands.add_parser("status")
    schedule_commands.add_parser("bot", help="Render the always-on Telegram bot LaunchAgent").add_argument("--install", action="store_true", help="Write it to ~/Library/LaunchAgents")
    inbox = subcommands.add_parser("inbox", help="Read employer replies from Gmail (read-only) and update statuses")
    inbox_commands = inbox.add_subparsers(dest="inbox_command", required=True)
    inbox_check = inbox_commands.add_parser("check", help="Match new replies to applied jobs and move statuses forward")
    inbox_check.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD (default: earliest application)")
    inbox_check.add_argument("--dry-run", action="store_true", help="Print proposed changes; write nothing")
    inbox_check.add_argument("--no-ai", action="store_true", help="Rules only; inconclusive emails go to review")
    inbox_commands.add_parser("review", help="List emails that need Master's review")
    inbox_commands.add_parser("filter", help="Write config/gmail_jobagent_filter.xml for Gmail's filter import")
    inbox_adopt = inbox_commands.add_parser("adopt", help="Make an applied job from an unmatched email (id from inbox review)")
    inbox_adopt.add_argument("email_id", type=int)
    inbox_adopt.add_argument("--company", help="Default: the sender's domain")
    inbox_adopt.add_argument("--title", help="Default: from the subject, else 'Unknown role'")
    subcommands.add_parser("list-jobs", help="List canonical stored jobs")
    subcommands.add_parser("bot", help="Run the Telegram bot (long polling; Ctrl-C stops it)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a shell-compatible status code."""
    load_local_env()
    args = build_parser().parse_args(argv)
    repository = JobRepository(Path(args.database))

    if args.command == "init-db":
        repository.initialize()
        print(f"Initialized database: {args.database}")
        return 0

    repository.initialize()
    if args.command == "add-demo-job":
        result = repository.add_or_get_job(demo_job())
        status = "Added" if result.created else "Already exists"
        print(f"{status}: job #{result.job.id} — {result.job.company} / {result.job.title}")
        return 0

    if args.command == "add-url":
        try:
            result = UrlIntakeService(repository).import_url(args.url)
        except IntakeError as error:
            print("Could not extract a valid job posting:")
            print(error)
            return 1
        status = "Added" if result.created else "Already exists"
        job = result.job
        print(f"{status}: job #{job.id} — {job.company} / {job.title}")
        print(f"Source: {job.canonical_source}")
        print(f"Location: {job.location or 'Unspecified'}")
        print(f"URL: {job.canonical_url or args.url}")
        return 0

    if args.command == "resolve":
        return _run_resolve(repository, args)

    if args.command == "scrape":
        return _run_scrape_command(repository, args)

    if args.command == "candidate":
        return _run_candidate_command(args)

    if args.command == "eligibility":
        return _run_eligibility_command(repository, args)

    if args.command == "evaluate":
        return _run_evaluate_command(repository, args)

    if args.command == "queue":
        return _run_queue_command(repository, args)

    if args.command == "jobs":
        return _run_jobs_command(repository, args)
    if args.command == "setup":
        Path("out").mkdir(exist_ok=True); Path("logs").mkdir(exist_ok=True); print("JobAgent local setup complete. Run: jobagent doctor --offline"); return 0
    if args.command == "doctor":
        for check in doctor(Path(args.database), offline=args.offline): print(f"{check.state}\t{check.name}\t{check.detail}")
        return 0
    if args.command == "prepare": return _run_prepare(repository, args)
    if args.command == "apply": return _run_apply(repository, args)
    if args.command == "notion": return _run_notion(repository, args)
    if args.command == "outreach": return _run_outreach(repository, args)
    if args.command == "notify":
        try: TelegramNotifier().send("JobAgent notification test.")
        except RuntimeError as error: print(f"Notification unavailable: {error}"); return 1
        print("Test notification sent."); return 0
    if args.command == "digest": return _run_digest(repository)
    if args.command == "status":
        try: repository.set_application_status(args.job_id, args.state)
        except ValueError as error: print(error); return 1
        print(f"Job #{args.job_id}: {args.state}"); return 0
    if args.command == "stats": return _run_stats(repository)
    if args.command == "followup": return _run_followup(repository, args)
    if args.command == "outcome": return _run_outcome(repository, args)
    if args.command == "inbox": return _run_inbox_command(repository, args)
    if args.command == "run": return _run_cycle(repository, args)
    if args.command == "schedule": return _run_schedule(args)
    if args.command == "bot": return run_bot(args.database)

    jobs = repository.list_jobs()
    if not jobs:
        print("No jobs stored.")
        return 0
    for job in jobs:
        location = job.location or "Unspecified"
        print(f"{job.id}\t{job.company}\t{job.title}\t{location}\t{job.canonical_source}")
    return 0


def _run_resolve(repository: JobRepository, args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        print("--limit must be positive")
        return 1
    jobs = repository.list_pending_resolutions(args.limit)
    if args.dry_run:
        print(f"Would resolve: {len(jobs)} (no HTTP requests or resolution writes)")
        return 0
    counts: Counter[str] = Counter()
    for index, job in enumerate(jobs):
        if index:
            time.sleep(1)
        result = resolve_destination(job.apply_url or job.canonical_url or "", job.company, job.title, job.location)
        if result is None:
            counts["unresolved"] += 1
            continue
        repository.set_job_resolution(job.id, destination_url=result.destination_url, ats=result.ats,
                                      evidence=result.evidence, confidence=result.confidence)
        counts[result.ats] += 1
    print(f"Processed: {len(jobs)}")
    for ats, count in sorted(counts.items()):
        print(f"{ats}: {count}")
    return 0


def _run_scrape_command(repository: JobRepository, args: argparse.Namespace) -> int:
    try:
        profiles = load_search_profiles(args.config)
        settings = load_scrape_settings()
    except ScrapeError as error:
        print(f"Could not load scrape configuration: {error}")
        return 1
    if args.scrape_command == "profiles":
        for profile in profiles:
            print(f"{profile.name}\t{'enabled' if profile.enabled else 'disabled'}\t{len(profile.keywords)} keywords")
        return 0
    selected = profiles if args.all_profiles else [profile for profile in profiles if profile.name == args.profile]
    if not selected:
        print(f"Search profile not found: {args.profile}")
        return 1
    if args.results:
        settings = replace(settings, results_wanted=max(1, args.results))
    summary = ScrapeService(repository, settings=settings).run(selected)
    print(f"Received: {summary.received}\nNew: {summary.new}\nExisting: {summary.existing}")
    print(f"Updated: {summary.updated}\nSkipped: {summary.skipped}")
    for error in summary.errors:
        print(f"Error: {error}")
    return 1 if summary.errors and not summary.received else 0


def _load_candidate_or_report(directory: str):
    try:
        return load_candidate_truth(directory)
    except CandidateConfigurationError as error:
        print(f"Could not load candidate configuration: {error}")
        return None


def _run_candidate_command(args: argparse.Namespace) -> int:
    candidate = _load_candidate_or_report(args.directory)
    if candidate is None:
        return 1
    facts = len(candidate.evidence_ids)
    print("Candidate configuration valid.")
    print(f"Experiences: {len(candidate.experiences)}")
    print(f"Evidence facts: {facts}")
    print(f"Skills: {candidate.skill_count}")
    return 0


def _run_eligibility_command(repository: JobRepository, args: argparse.Namespace) -> int:
    candidate = _load_candidate_or_report(args.candidate_directory)
    if candidate is None:
        return 1
    jobs = [repository.get_job(args.job_id)] if args.job_id else repository.list_jobs()
    jobs = [job for job in jobs if job is not None]
    if args.job_id and not jobs:
        print(f"Job not found: {args.job_id}")
        return 1
    counts = {"eligible": 0, "review": 0, "ineligible": 0}
    for job, result in zip(jobs, refresh_eligibility(repository, candidate, jobs), strict=True):
        counts[result.status] += 1
        if args.job_id:
            print(f"Job #{job.id}: {result.status}")
            for reason in result.reasons:
                print(f"Reason: {reason}")
            for flag in result.flags:
                print(f"Flag: {flag}")
    print(f"Evaluated: {len(jobs)}")
    print(f"Eligible: {counts['eligible']}")
    print(f"Review: {counts['review']}")
    print(f"Ineligible: {counts['ineligible']}")
    return 0


def _run_evaluate_command(repository: JobRepository, args: argparse.Namespace) -> int:
    candidate = _load_candidate_or_report(args.candidate_directory)
    if candidate is None:
        return 1
    jobs = [repository.get_job(args.job_id)] if args.job_id else repository.list_jobs()
    jobs = [job for job in jobs if job is not None]
    current_rules = rules_fingerprint(candidate)
    eligible = [
        job
        for job in jobs
        if (result := repository.get_eligibility(job.id, current_rules)) and result.status in {"eligible", "review"}
    ]
    limit = min(args.limit or candidate.preferences.scoring.max_jobs_per_run, candidate.preferences.scoring.max_jobs_per_run)
    if args.dry_run:
        print(f"Would score: {min(len(eligible), limit)}")
        return 0
    result = score_batch(AssessmentService(repository, CodexCliJobScorer(), candidate), eligible, limit=limit, force=args.force)
    for job_id, error in result["errors"].items():  # type: ignore[union-attr]
        print(f"Job #{job_id}: failed — {error}")
    print(f"Scored: {result['scored']}")
    print(f"Cached: {result['cached']}")
    print(f"Failed: {result['failed']}")
    return 0


def _queue_date(value: str | None) -> str:
    if value is None:
        return datetime.now().astimezone().date().isoformat()  # Master's calendar day, not UTC's
    return value


def _run_queue_command(repository: JobRepository, args: argparse.Namespace) -> int:
    queue_date = _queue_date(args.date)
    if args.queue_command == "show":
        entries = repository.list_queue(queue_date)
        if not entries:
            print("No queued jobs.")
            return 0
        for entry in entries:
            job = repository.get_job(entry.job_id)
            if job:
                print(f"{entry.rank:2}. {entry.ranking_score:4.1f}  {job.company}\t{job.title}")
        return 0
    candidate = _load_candidate_or_report(args.candidate_directory)
    if candidate is None:
        return 1
    built = build_queue(repository, candidate, queue_date, job_closed)
    entries = built.entries
    print(f"New eligible evaluated: {built.considered}")
    print(f"Above threshold: {built.above_threshold}")
    print(f"Queued: {len(entries)}")
    if len(entries) < candidate.preferences.daily_queue.minimum_target:
        print("Target not filled because no additional jobs met quality requirements.")
    return 0


def _run_jobs_command(repository: JobRepository, args: argparse.Namespace) -> int:
    job = repository.get_job(args.job_id)
    if job is None:
        print(f"Job not found: {args.job_id}")
        return 1
    print(f"Job #{job.id}: {job.company} / {job.title}")
    print(f"Location: {job.location or 'Unspecified'}")
    print(f"Source: {job.canonical_source}")
    eligibility = repository.get_eligibility(job.id)
    if eligibility:
        print(f"Eligibility: {eligibility.status}")
        print(f"Reasons: {'; '.join(eligibility.reasons)}")
        if eligibility.flags:
            print(f"Flags: {', '.join(eligibility.flags)}")
    assessment = repository.get_latest_assessment(job.id)
    if assessment:
        print(f"Assessment: {assessment.overall_score} ({assessment.recommendation})")
        print(f"Assessment reason: {assessment.summary_reason}")
    queued = [entry for entry in repository.list_queue(_queue_date(None)) if entry.job_id == job.id]
    if queued:
        print(f"Queue: rank {queued[0].rank}, score {queued[0].ranking_score:.1f}")
    return 0


def _run_prepare(repository: JobRepository, args: argparse.Namespace) -> int:
    candidate = _load_candidate_or_report("candidate")
    if candidate is None: return 1
    if args.dry_run:
        jobs = [repository.get_job(args.job_id)] if args.job_id else [repository.get_job(entry.job_id) for entry in repository.list_queue(_queue_date(args.date))]
        print(f"Would prepare: {len([job for job in jobs if job])}"); return 0
    jobs = [repository.get_job(args.job_id)] if args.job_id else [repository.get_job(entry.job_id) for entry in repository.list_queue(_queue_date(args.date))]
    limit = args.limit or load_writing_settings().max_packets_per_run
    service = PreparationService(repository, candidate, cover_letter_writer=_cover_letter_writer()); ready = failed = 0
    for job in [item for item in jobs if item][:limit]:
        if args.job_id:  # Master picked this job: filters warn, never block
            eligibility = repository.get_eligibility(job.id, rules_fingerprint(candidate))
            if eligibility is None or eligibility.status == "ineligible":
                print(f"Job #{job.id}: warning — {', '.join(eligibility.reasons) if eligibility else 'eligibility not run'}; preparing anyway")
        packet, error = service.prepare(job, queue_date=_queue_date(args.date), force=args.force, master_picked=bool(args.job_id))
        ready += int(packet is not None and error is None); failed += int(error is not None)
        if error: print(f"Job #{job.id}: failed — {error}")
        elif packet: print(f"Job #{job.id}: {packet.packet_directory}" + ("" if packet.cover_letter_pdf_path else "  (no cover letter)"))
    if args.job_id and ready:  # Master's own pick is never in a daily queue, so the cycle's Notion sync skips it
        _sync_one_to_notion(repository, args.job_id)
    print(f"Ready: {ready}\nFailed: {failed}"); return 0 if not failed else 1


def _sync_one_to_notion(repository: JobRepository, job_id: int) -> None:
    """Best effort: a Notion outage must not fail a packet that is already prepared."""
    try:
        result = sync_to_notion(repository, notion_tracker(), [job_id])
    except RuntimeError as error:
        print(f"Job #{job_id}: not synced to Notion — {error}"); return
    for failed_id, error in result["errors"].items():  # type: ignore[union-attr]
        print(f"Job #{failed_id}: Notion sync failed — {error}")
    if result["synced"]:
        print(f"Job #{job_id}: synced to Notion")


def _cover_letter_writer() -> ClaudeCoverLetterWriter | None:
    writer = ClaudeCoverLetterWriter()
    return writer if shutil.which(writer.executable) else None


def _outreach_service(repository: JobRepository, candidate: CandidateTruth) -> OutreachService | None:
    """None (step reported "not configured") when the Claude CLI is missing."""
    writer = ClaudeOutreachWriter()
    if not shutil.which(writer.executable):
        return None
    profile = load_application_profile("candidate")
    contact, links = profile.contact, profile.links
    sign = signature(f"{contact.first_name} {contact.last_name}", contact.phone, contact.email, links.linkedin)
    return OutreachService(repository, candidate, writer, sign, hunter=HunterClient.from_env())


def _run_stats(repository: JobRepository) -> int:
    for state, count in sorted(repository.status_counts().items()): print(f"{state}: {count}")
    result = funnel(repository)
    applied = result.reached["applied"]
    print(f"\nFunnel (jobs that ever reached each stage, of {applied} applied):")
    for stage in FUNNEL:
        print(f"  {stage:<17}{result.reached[stage]:>4}  {result.rate(stage):6.1%}")
    print(f"  closed: rejected {result.closed['rejected']}, ghosted {result.closed['ghosted']}")
    if result.by_week:
        print("\nBy apply week: " + "  ".join(stage for stage in FUNNEL))
        for week, counts in result.by_week.items():
            print(f"  {week}  " + "  ".join(f"{counts[stage]:>{len(stage)}}" for stage in FUNNEL))
    return 0


def _describe(repository: JobRepository, job_id: int) -> str:
    job = repository.get_job(job_id)
    return f"{job.company} / {job.title}" if job else "(job missing)"


def _run_followup(repository: JobRepository, args: argparse.Namespace) -> int:
    """Follow-up drafts only: Master reviews and sends them himself, then records it with `followup sent`."""
    if args.followup_command == "sent":
        followup = repository.mark_followup_sent(args.job_id)
        print(f"Job #{args.job_id}: follow-up {followup.number} marked sent" if followup else f"Job #{args.job_id}: no unsent follow-up draft")
        return 0 if followup else 1
    due = due_for_followup(repository, days=args.days)
    if args.followup_command == "list":
        if not due:
            print(f"No applications quiet for {args.days}+ days that need a follow-up.")
        for item in due:
            print(f"Job #{item.job_id}: {_describe(repository, item.job_id)} — {item.status}, quiet {item.quiet_days}d, {item.followups_sent} sent")
        return 0
    candidate = _load_candidate_or_report("candidate")
    if candidate is None:
        return 1
    writer = ClaudeFollowupWriter()
    if not shutil.which(writer.executable):
        print("Claude CLI not found; follow-up drafts need it."); return 1
    profile = load_application_profile("candidate")
    contact, links = profile.contact, profile.links
    service = FollowupService(repository, candidate, writer, signature(f"{contact.first_name} {contact.last_name}", contact.phone, contact.email, links.linkedin))
    job_ids = args.job_id or [item.job_id for item in due]
    if not job_ids:
        print(f"No applications quiet for {args.days}+ days that need a follow-up."); return 0
    failed = 0
    for job_id in job_ids:
        job = repository.get_job(job_id)
        if job is None:
            print(f"Job #{job_id}: not found"); failed += 1; continue
        try:
            followup = service.draft(job)
        except (OSError, RuntimeError, ValueError) as error:  # one bad draft spares the rest
            print(f"Job #{job_id}: failed — {error}"); failed += 1; continue
        print(f"Job #{job_id}: follow-up {followup.number} drafted\n  {followup.draft_path}")
    return 0 if not failed else 1


def _run_outcome(repository: JobRepository, args: argparse.Namespace) -> int:
    quiet = quiet_applications(repository, days=args.days)
    if not quiet:
        print(f"No open applications quiet for {args.days}+ days."); return 0
    for item in quiet:
        print(f"Job #{item.job_id}: {_describe(repository, item.job_id)} — {item.status}, quiet {item.quiet_days}d, {item.followups_sent} follow-ups sent")
        if args.yes:
            repository.set_application_status(item.job_id, "ghosted")
    print(f"Marked {len(quiet)} ghosted." if args.yes else f"{len(quiet)} would be marked ghosted; re-run with --yes to apply.")
    return 0


def _run_outreach(repository: JobRepository, args: argparse.Namespace) -> int:
    """Draft outreach emails for prepared jobs. Drafts only: Master reviews and sends them himself."""
    candidate = _load_candidate_or_report("candidate")
    if candidate is None:
        return 1
    service = _outreach_service(repository, candidate)
    if service is None:
        print("Claude CLI not found; outreach drafts need it."); return 1
    if service.hunter is None:
        print("HUNTER_API_KEY not set: only jobs whose LinkedIn page names a poster can get a contact.")
    job_ids = args.job_id or [entry.job_id for entry in repository.list_queue(_queue_date(args.date))]
    result = run_outreach(repository, service, job_ids, force=args.force)
    for job_id in job_ids:
        found = repository.get_outreach(job_id)
        if found:
            guess = "" if found.verified else "  (UNVERIFIED guess)" if found.email_source == "pattern" else ""
            print(f"Job #{job_id}: {found.name}, {found.title} <{found.email or 'no email'}>{guess}\n  {found.draft_path}")
        elif job_id in result["errors"]:
            print(f"Job #{job_id}: failed — {result['errors'][job_id]}")
        else:
            print(f"Job #{job_id}: no contact found")
    print({key: value for key, value in result.items() if key != "errors"})
    return 0 if not result["failed"] else 1


def _run_apply(repository: JobRepository, args: argparse.Namespace) -> int:
    """Pre-fill a job's application form for human review. Never submits it."""
    if args.apply_command in {"review", "review-prompt", "findings", "fixture", "streak"}:
        return _run_apply_training(args)
    if args.apply_command == "train":
        args.practice, args.no_review, args.tabs = True, False, False
    if getattr(args, "unattended", False):
        if getattr(args, "tabs", False):
            print("--unattended fills one job at a time; drop --tabs."); return 1
        args.practice = True  # the same Next / Continue allowlist and submit guard as practice mode
    if getattr(args, "practice", False) and (args.no_review or os.getenv("JOBAGENT_FILL_REVIEW", "").casefold() == "off"):
        print("--practice / --unattended need the step review (they only advance reviewed steps); "
              "drop --no-review / JOBAGENT_FILL_REVIEW=off.")
        return 1
    if getattr(args, "unattended", False):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # Telegram /cancel: close the browser on the way out
    if getattr(args, "advance", False):
        print("--advance is deprecated and ignored: JobAgent never clicks Continue/Next/Submit; you click Continue.")
    profile = _load_profile_or_report()
    if profile is None:
        return 1
    _pull_notion(repository)
    if args.apply_command == "url":
        return _run_apply_url(repository, profile, args)
    if args.apply_command == "track":
        return _run_apply_track(repository, args)
    if args.job_id:
        jobs = [repository.get_job(job_id) for job_id in args.job_id]
        missing = [job_id for job_id, job in zip(args.job_id, jobs, strict=True) if job is None]
        if missing:
            print(f"Job not found: {', '.join(map(str, missing))}")
            return 1
    elif args.ready:
        done = {job_id for job_id, status in repository.latest_statuses().items() if status in {"applied", "prefilled", "drafted", "skipped", "rejected", "expired"}}
        ids = dict.fromkeys(packet.job_id for packet in repository.list_packets(status="ready") if packet.job_id not in done)
        jobs = [job for job in map(repository.get_job, ids) if job][:args.limit]
    else:
        jobs = [repository.get_job(entry.job_id) for entry in repository.list_queue(_queue_date(args.date))]
        jobs = [job for job in jobs if job][:args.limit]
    if not jobs:
        print("No jobs to fill.")
        return 0
    if getattr(args, "tabs", False):
        return _run_apply_tabs(repository, jobs, profile, args)
    filled = sum(_fill_one(repository, job, profile, args) for job in jobs)
    print(f"Filled: {filled}\nFailed: {len(jobs) - filled}")
    if args.apply_command == "train":
        _run_apply_training(argparse.Namespace(apply_command="streak", n=5))
        ids = " ".join(str(job.id) for job in jobs)
        print(f"Next: fix each finding, then `uv run jobagent apply fixture --job-id <id>` ({ids}) and `uv run pytest`. "
              "For a Cowork review: `uv run jobagent apply review-prompt --job-id <id> --step <n>`, then paste the "
              "answer into `uv run jobagent apply findings --job-id <id> --step <n>`.")
    return 0 if filled == len(jobs) else 1


def _load_profile_or_report() -> ApplicationProfile | None:
    try:
        return load_application_profile("candidate")
    except ApplicationConfigurationError as error:
        print(f"Could not load application configuration: {error}")
        return None


def _pull_notion(repository: JobRepository) -> None:
    try:  # Master marks applications in Notion; never refill one already applied to
        print(f"Statuses updated from Notion: {pull_notion_statuses(repository, notion_tracker())['updated']}")
    except RuntimeError as error:
        print(f"Notion pull skipped: {error}")


def _form_for(repository: JobRepository, job: StoredJob) -> tuple[StoredJob, ApplicationForm | None]:
    """The job's application form. A LinkedIn or careers-page link is first resolved to its ATS form and saved."""
    form = application_form(job)
    if form is not None:
        return job, form
    if job.resolved_ats == "linkedin_easy_apply" or job.resolved_destination_url:
        return job, None
    result = resolve_destination(job.apply_url or job.canonical_url or "", job.company, job.title, job.location)
    if result is None:
        return job, None
    repository.set_job_resolution(job.id, destination_url=result.destination_url, ats=result.ats,
                                  evidence=result.evidence, confidence=result.confidence)
    print(f"Job #{job.id}: {result.ats}")
    job = repository.get_job(job.id) or job
    return job, application_form(job)


def _fill_one(repository: JobRepository, job: StoredJob, profile: ApplicationProfile, args: argparse.Namespace) -> bool:
    """Open and pre-fill one job's form for Master's review. True when it was filled. Never submits."""
    packet = repository.get_latest_ready_packet(job.id)
    if packet is None:
        print(f"Job #{job.id}: no ready packet — run `jobagent prepare` first"); return False
    job, form = _form_for(repository, job)
    if form is None:
        print(f"Job #{job.id}: unsupported ATS — open and fill in manually"); return False
    resume_path = Path(packet.resume_pdf_path) if packet.resume_pdf_path else None
    cover_letter_path = Path(packet.cover_letter_pdf_path) if packet.cover_letter_pdf_path else None
    print(f"Job #{job.id}: opening {form.form_url}")
    oversight = _oversight_for(job, args)
    unattended = getattr(args, "unattended", False)

    def record(report: FillReport) -> None:
        report.write(FILL_REPORTS / f"job_{job.id}.json")
        _print_ai_drafts(report)
        if report.required_empty:
            print(f"Job #{job.id}: {len(report.required_empty)} required field(s) left empty — review before submitting")
        repository.set_application_status(job.id, "drafted" if report.draft == "confirmed" else "prefilled")
        if unattended:  # before the browser is held open, so Master hears about it now
            text = unattended_summary(report, oversight, form.provider)
            print(text, flush=True)
            for chunk in digest_chunks(text):
                if oversight.notify is not None:
                    try:
                        oversight.notify(chunk)
                    except Exception as error:  # noqa: BLE001 - the fill itself is done
                        print(f"Telegram notification failed: {type(error).__name__}", flush=True)

    try:
        report = fill_application_form(
            form, profile.for_company(job.company), job_id=job.id, resume_path=resume_path, cover_letter_path=cover_letter_path,
            slow_mo=args.slow_mo, profile_directory=args.profile_dir, oversight=oversight,
            **({"deadline_seconds": args.deadline_min * 60, "on_done": record} if unattended else {}),
        )
    except RuntimeError as error:
        print(f"Job #{job.id}: could not fill form — {error}")
        if unattended and oversight.notify is not None:
            oversight.notify(f"JobAgent fill #{job.id} failed: {str(error)[:200]}")
        return False
    if not unattended:
        record(report)
    return True


def _run_apply_training(args: argparse.Namespace) -> int:
    """The training loop's offline commands: they read logs/fill_snapshots/ and never open a browser."""
    if args.apply_command == "streak":
        result = streak(n=args.n)
        print(f"Streak: {len(result.tenants)}/{args.n} Workday tenants with zero wrong findings "
              f"({', '.join(result.tenants) or 'none'}); Cowork-reviewed: {result.cowork}/2")
        if result.broken_by:
            print(f"Broken by {result.broken_by}")
        return 0 if result.met(args.n) else 1
    profile = _load_profile_or_report()
    if profile is None:
        return 1
    directory = SNAPSHOT_ROOT / str(args.job_id)
    if args.apply_command == "fixture":
        paths = write_fixtures(args.job_id, fixtures_for_job(args.job_id, profile.answers))
        print("\n".join(str(path) for path in paths) or "No wrong findings on JobAgent-filled fields.")
        return 0
    try:
        if args.apply_command == "review-prompt":
            print(review_prompt(load_snapshot(args.job_id, args.step), profile, for_cowork=True))
            return 0
        if args.apply_command == "findings":
            text = args.file.read_text() if args.file else sys.stdin.read()
            snapshot = load_snapshot(args.job_id, args.step)
            findings = validate_review(parse_pasted_review(text), snapshot, profile)
            print(f"Saved {save_review(directory, args.step, findings, reviewer='cowork')}\n{format_findings(findings)}")
            return 0
        steps = [args.step] if args.step else sorted(int(path.stem) for path in directory.glob("[0-9][0-9].json"))
        if not steps:
            print(f"No snapshots for job {args.job_id} in {directory}")
            return 1
        reviewer = ClaudeReviewer(model=args.model, timeout=300)
        for step in steps:
            findings = reviewer(load_snapshot(args.job_id, step), profile)
            save_review(directory, step, findings, reviewer="claude", model=args.model)
            print(f"Step {step}:\n{format_findings(findings)}")
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as error:  # pydantic's ValidationError is a ValueError
        print(f"Review failed: {str(error)[:300]}")
        return 1


def _fill_switches(parser: argparse.ArgumentParser, *, training: bool = False) -> None:
    parser.add_argument("--no-ai-answers", action="store_true",
                        help="Leave free-text questions blank (default: Claude drafts them from the truth files; env JOBAGENT_AI_ANSWERS=off)")
    if not training:
        parser.add_argument("--practice", action="store_true",
                            help="Training rounds only: after each step is filled, snapshotted and reviewed, click Next / "
                                 "Continue / Save and Continue; stops for good at the Review page. Never clicks Submit")
        parser.add_argument("--no-review", action="store_true",
                            help="Skip the per-step AI review (env JOBAGENT_FILL_REVIEW=off)")
        parser.add_argument("--unattended", action="store_true",
                            help="Telegram /fill: practice-fill up to the final step (Next / Continue / Save and Continue, "
                                 "then an exact 'Save for Later' if shown), confirm the Workday draft, send the link and a "
                                 "summary to Telegram. Never clicks Submit")
        parser.add_argument("--deadline-min", type=float, default=40,
                            help="--unattended: give up (and ping Telegram) after this many minutes (default 40)")
    parser.add_argument("--review-model", default=TRAINING_MODEL if training else None,
                        help=f"Claude model for step reviews (default: {TRAINING_MODEL if training else RUNTIME_MODEL})")
    parser.add_argument("--notify", action="store_true", help="Also send review findings to Telegram")
    parser.add_argument("--oversight-fix", action="store_true",
                        help="Correct JobAgent-filled choice fields to validated values the review names (off by default)")


def _telegram_notify() -> Callable[[str], None] | None:
    notifier = TelegramNotifier()
    return notifier.send if notifier.token and notifier.chat_id else None


def _oversight_for(job: StoredJob, args: argparse.Namespace) -> Oversight:
    """Snapshots every settled step to logs/fill_snapshots/<job>/; signs in with help from read-only Gmail;
    drafts free-text answers (ai_draft); "needs you" goes to Telegram when configured."""
    drafter = None
    if not getattr(args, "no_ai_answers", False) and os.getenv("JOBAGENT_AI_ANSWERS", "").casefold() != "off":
        try:
            drafter = ClaudeAnswerDrafter(job, load_candidate_truth("candidate"))
        except (CandidateConfigurationError, ValueError, OSError) as error:
            print(f"AI answers off: {error}")
    reviewer = None
    if not getattr(args, "no_review", False) and os.getenv("JOBAGENT_FILL_REVIEW", "").casefold() != "off":
        model = getattr(args, "review_model", None)
        reviewer = ClaudeReviewer(model=model, timeout=300 if args.apply_command == "train" else RUNTIME_TIMEOUT_SECONDS)
    return Oversight(job_id=job.id, job_label=f"#{job.id} {job.company} {job.title}", company=job.company, job_title=job.title,
                     unattended=getattr(args, "unattended", False),
                     notify=_telegram_notify(), mail=reader_from_env(), drafter=drafter,
                     profile=load_application_profile("candidate").for_company(job.company), reviewer=reviewer,
                     practice=getattr(args, "practice", False), notify_findings=getattr(args, "notify", False),
                     fix=getattr(args, "oversight_fix", False))


def _print_ai_drafts(report: FillReport) -> None:
    for item in report.fields:
        if item.source == "certification.fallback":  # Master sees this every time (his call, 2026-11-19)
            print(f"Job #{report.job_id}: {item.label}: {item.reason or 'certification fallback: Security+ used'}")
    drafts = [item for item in report.fields if item.source == "ai_draft"]
    if drafts:
        print(f"Job #{report.job_id}: {len(drafts)} AI-drafted answer(s) to review before you submit:")
        for item in drafts:
            print(f"  - {item.label}\n    {item.value}")


def _run_apply_url(repository: JobRepository, profile: ApplicationProfile, args: argparse.Namespace) -> int:
    """One pasted link to a pre-filled form: import, dedupe the opening, resolve the ATS, prepare, sync, fill."""
    keep = _import_opening(repository, args.url, force=args.force)
    if keep is None:
        return 1
    if repository.get_latest_ready_packet(keep.id) is None:
        prepare_args = argparse.Namespace(job_id=keep.id, date=None, dry_run=False, limit=None, force=False)
        if _run_prepare(repository, prepare_args) != 0:
            return 1
    else:
        print(f"Job #{keep.id}: packet already ready")
        _sync_one_to_notion(repository, keep.id)
    return 0 if _fill_one(repository, keep, profile, args) else 1


def _run_apply_track(repository: JobRepository, args: argparse.Namespace) -> int:
    """Links Master applies to by hand: a Notion row to mark Applied, and nothing else."""
    failed = 0
    for url in args.urls:
        keep = _import_opening(repository, url, force=False)
        if keep is None:
            failed += 1; continue
        if repository.get_latest_ready_packet(keep.id) is None:
            # ponytail: a document-less "ready" packet is the Notion sync's unit of work; no files exist.
            repository.save_packet(job_id=keep.id, fingerprint="track-only", status="ready",
                                   validation_state="track_only", writing_model="none")
        _sync_one_to_notion(repository, keep.id)
    return 1 if failed else 0


def _import_opening(repository: JobRepository, url: str, *, force: bool) -> StoredJob | None:
    """Import one pasted link as one opening: resolve its ATS form, dedupe by company + title."""
    # Resolve before importing: a careers listing page imports as a junk "job" (STARK, Oct 8), while the
    # ATS form behind it carries the real company and title.
    resolution = resolve_destination(url, company_hint(url), "")
    resolved = resolution.destination_url if resolution else None
    structured = resolved is not None and detect_source(resolved) in {"greenhouse", "ashby", "lever"}
    sources = [resolved, url] if structured else [url, resolved]
    imported, error = None, None
    for source in dict.fromkeys(item for item in sources if item):  # best source first, the other as fallback
        try:
            imported = UrlIntakeService(repository).import_url(source).job; break
        except IntakeError as failure:
            error = failure
    if imported is None:
        print(f"Could not extract a valid job posting: {error}"); return None
    copies = repository.find_opening(imported.company, imported.title) or [imported]
    statuses = repository.latest_statuses()
    acted = [copy.id for copy in copies if statuses.get(copy.id) in {"applied", "prefilled", "drafted"}]
    if acted and not force:
        print(f"{imported.company} / {imported.title} was already {statuses[acted[0]]} as job #{acted[0]} "
              "(one opening = company + title). Rerun with --force to apply again."); return None
    keep = next((copy for copy in copies if repository.get_latest_ready_packet(copy.id)), min(copies, key=lambda c: c.id))
    for copy in copies:
        if copy.id != keep.id and copy.id not in statuses:  # never touch a copy Master already acted on
            repository.set_application_status(copy.id, "skipped", source="dedupe")
            print(f"Job #{copy.id}: duplicate of #{keep.id}, skipped")
    print(f"Job #{keep.id}: {keep.company} / {keep.title} ({keep.location or 'location unspecified'})")
    resolution = resolution or resolve_destination(url, keep.company, keep.title, keep.location)
    if resolution:
        repository.set_job_resolution(keep.id, destination_url=resolution.destination_url, ats=resolution.ats,
                                      evidence=resolution.evidence, confidence=resolution.confidence)
        keep = repository.get_job(keep.id) or keep
    return keep


def _run_apply_tabs(repository: JobRepository, jobs: list[StoredJob], profile: ApplicationProfile, args: argparse.Namespace) -> int:
    """Fill every single-page (no sign-in) form in its own tab of one browser. Never submits."""
    tabs = []
    for job in jobs:
        packet = repository.get_latest_ready_packet(job.id)
        job, form = _form_for(repository, job)
        if packet is None or form is None or form.multi_page:
            print(f"Job #{job.id}: skipped — {'no ready packet' if packet is None else 'needs sign-in or unsupported ATS'}")
            continue
        tabs.append(TabJob(
            job.id, form,
            Path(packet.resume_pdf_path) if packet.resume_pdf_path else None,
            Path(packet.cover_letter_pdf_path) if packet.cover_letter_pdf_path else None,
        ))
    if not tabs:
        print("No single-page forms to fill.")
        return 0

    def record(report: FillReport) -> None:
        report.write(FILL_REPORTS / f"job_{report.job_id}.json")
        repository.set_application_status(report.job_id, "prefilled")
        empty = len(report.required_empty)
        print(f"Job #{report.job_id}: filled{f', {empty} required field(s) left empty' if empty else ''}", flush=True)

    print(f"Opening {len(tabs)} tab(s): " + ", ".join(f"#{tab.job_id}" for tab in tabs), flush=True)
    fill_in_tabs(tabs, profile, profile_directory=args.profile_dir, slow_mo=args.slow_mo, on_report=record)
    return 0


def _run_digest(repository: JobRepository) -> int:
    counts = repository.status_counts(); packets = repository.list_packets()
    text = f"JobAgent Daily Digest\nCanonical jobs: {repository.count_jobs()}\nPackets ready: {sum(packet.status == 'ready' for packet in packets)}\nPackets failed: {sum(packet.status == 'failed' for packet in packets)}\nApplied: {counts.get('applied', 0)}"
    try:
        for chunk in digest_chunks(text): TelegramNotifier().send(chunk)
    except RuntimeError as error: print(f"Digest generated but not sent: {error}\n{text}"); return 1
    print("Daily digest sent."); return 0


def _run_notion(repository: JobRepository, args: argparse.Namespace) -> int:
    try:
        tracker = notion_tracker()
        if args.notion_command == "setup":
            added = tracker.setup()
            print(f"Notion properties added: {', '.join(added) if added else 'none (already set up)'}")
            return 0
        if args.notion_command == "pull":
            print(f"Statuses updated from Notion: {pull_notion_statuses(repository, tracker)['updated']}")
            return 0
    except RuntimeError as error:
        print(f"Notion unavailable: {error}")
        return 1
    job_ids = [args.job_id] if args.job_id else sorted({packet.job_id for packet in repository.list_packets(status="ready")})
    result = sync_to_notion(repository, tracker, job_ids)
    for job_id, error in result["errors"].items():  # type: ignore[union-attr]
        print(f"Job #{job_id}: failed — {error}")
    print(f"Synced: {result['synced']}\nDeleted in Notion (now skipped): {result['removed_in_notion']}\nFailed: {result['failed']}")
    return 0 if not result["failed"] else 1


def _run_cycle(repository: JobRepository, args: argparse.Namespace) -> int:
    """The scheduled pipeline. Dry run scrapes and runs eligibility only (no AI, no Notion)."""
    candidate = _load_candidate_or_report("candidate")
    if candidate is None:
        return 1
    try:
        profiles, settings = load_search_profiles(args.config), load_scrape_settings()
    except ScrapeError as error:
        print(f"Could not load scrape configuration: {error}")
        return 1
    try:
        tracker = notion_tracker()
    except RuntimeError:
        tracker = None
    scorer = CodexCliJobScorer()  # skipped below if not installed
    try:
        with WorkflowLock(), _cycle_log():
            run_id = repository.start_workflow_run()
            metrics = run_cycle(
                repository,
                candidate,
                scrape=lambda: ScrapeService(repository, settings=settings).run(profiles),
                scorer=scorer if shutil.which(scorer.executable) else None,
                tracker=tracker,
                preparer=PreparationService(repository, candidate, cover_letter_writer=_cover_letter_writer()),
                outreach=_outreach_service(repository, candidate),
                resolve=lambda job: resolve_application_url(job.apply_url or job.canonical_url or "", job.company, job.title, job.location),
                today=_queue_date(None),
                max_packets=load_writing_settings().max_packets_per_run,
                dry_run=args.dry_run,
                notify=_cycle_notifier(),
                inbox=_inbox_stage(repository, tracker),
            )
            errors = metrics["errors"]
            repository.complete_workflow_run(run_id, status="partial" if errors else "success", metrics=metrics)
    except RuntimeError as error:
        print(error)
        return 1
    for key, value in metrics.items():
        print(f"{key}: {value}")
    return 1 if errors else 0


@contextmanager
def _cycle_log() -> Iterator[Path]:
    """Append this cycle's log lines (UTC timestamps) to logs/cycle_<local date>.log and echo them to stderr."""
    path = Path("logs") / f"cycle_{_queue_date(None)}.log"
    path.parent.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handlers = [logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()]
    root, previous = logging.getLogger(), logging.getLogger().level
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per HTTP request is noise
    try:
        yield path
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(previous)


def _cycle_notifier() -> Callable[[str], None] | None:
    """Telegram start/end/failure messages for the cycle; None (silent) when Telegram is not configured."""
    notifier = TelegramNotifier()
    return notifier.send if notifier.token and notifier.chat_id else None


GMAIL_FILTER = Path("config/gmail_jobagent_filter.xml")


_gmail_reader = reader_from_env
INBOX_AUTO_APPLY = "JOBAGENT_INBOX_AUTO_APPLY"


def _inbox_stage(repository: JobRepository, tracker: object) -> Callable[[], dict[str, object]] | None:
    """The cycle's inbox step; None (skipped) without Gmail credentials.

    By default it only proposes: status changes are notified and wait for Master's ✅ in the bot
    (/inbox). JOBAGENT_INBOX_AUTO_APPLY=1 applies them directly."""
    reader = _gmail_reader()
    if reader is None:
        return None
    auto = os.getenv(INBOX_AUTO_APPLY, "").strip().lower() in {"1", "true", "yes"}

    def stage() -> dict[str, object]:
        result = run_inbox(repository, reader, propose_only=not auto, classifier=ClaudeEmailClassifier(),
                           tracker=tracker, notify=_cycle_notifier())  # type: ignore[arg-type]
        return {**result.summary(), "mode": "applied" if auto else "proposed"}

    return stage


def _run_inbox_command(repository: JobRepository, args: argparse.Namespace) -> int:
    if args.inbox_command == "filter":
        GMAIL_FILTER.parent.mkdir(exist_ok=True)
        GMAIL_FILTER.write_text(gmail_filter_xml(applied_jobs(repository)), encoding="utf-8")
        print(f"Wrote {GMAIL_FILTER}. Import it in Gmail: Settings → See all settings → Filters and Blocked Addresses → Import filters.")
        return 0
    if args.inbox_command == "review":
        items = repository.list_email_responses(needs_review=True)
        for item in items:
            job = repository.get_job(item.job_id) if item.job_id else None
            when = item.received_at.astimezone().strftime("%Y-%m-%d") if item.received_at else "?"
            where = f"#{job.id} {job.company} / {job.title}" if job else "no job matched"
            print(f"email {item.id}  {when}  {item.from_addr}  {item.subject[:80]!r}  → {item.classification} ({item.confidence:.2f}); {where}")
        print(f"{len(items)} email(s) need review. Set a status with: jobagent status set <job_id> <state>; "
              "make a job from an unmatched one with: jobagent inbox adopt <email_id>")
        return 0
    if args.inbox_command == "adopt":
        try:
            job, created = adopt_email(repository, args.email_id, company=args.company, title=args.title)
        except ValueError as error:
            print(error); return 1
        status = repository.latest_statuses().get(job.id)
        print(f"{'Created' if created else 'Linked to existing'} job #{job.id} {job.company} / {job.title} "
              f"({status}); email {args.email_id} linked.")
        return 0
    reader = _gmail_reader()
    if reader is None:
        print("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first."); return 1
    try:
        tracker = None if args.dry_run else notion_tracker()
    except RuntimeError:
        tracker = None
    try:
        result = run_inbox(repository, reader, since=args.since, dry_run=args.dry_run,
                           classifier=None if args.no_ai else ClaudeEmailClassifier(), tracker=tracker,
                           notify=None if args.dry_run else _cycle_notifier())
    except InboxError as error:
        print(f"Gmail check failed: {error}"); return 1
    _print_inbox(result, dry_run=args.dry_run)
    return 1 if result.errors else 0


def _print_inbox(result: InboxResult, *, dry_run: bool) -> None:
    verb = "would change" if dry_run else "changed"
    for decision in result.decisions:
        if decision.job is None and not decision.needs_review:
            continue
        who = (f"#{decision.job.id} {decision.job.company}" + "".join(f" +#{job_id}" for job_id in decision.copies)
               if decision.job else decision.email.from_addr.split("@")[-1])
        outcome = f"{verb} → {decision.change}" if decision.change else ("NEEDS REVIEW" if decision.needs_review else "no change")
        print(f"{who}: {decision.classification} ({decision.confidence:.2f}) {outcome}"
              + (f" [{decision.reason}]" if decision.reason else ""))
    for key, value in result.summary().items():
        print(f"{key}: {value}")
    if dry_run:
        print("Dry run: nothing was written.")


def _run_schedule(args: argparse.Namespace) -> int:
    if args.schedule_command == "status": print("Schedule installation is manual; no generated launchd files are tracked."); return 0
    if args.schedule_command == "bot": return _schedule_bot(args.install)
    plist = launchd_plist(Path.cwd(), Path(os.environ.get("UV_EXECUTABLE", "uv")), "08:00", ["run", "cycle"], "com.jobagent.cycle")
    print(plist); return 0


def _schedule_bot(install: bool) -> int:
    uv = Path(shutil.which("uv") or "uv").resolve()
    dirs = [str(uv.parent)] + [str(Path(c).parent) for c in map(shutil.which, ("claude", "codex")) if c]
    path_env = ":".join(dict.fromkeys([*dirs, "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]))
    plist = bot_launchd_plist(Path.cwd().resolve(), uv, path_env)
    if not install: print(plist); return 0
    (Path.cwd() / "logs").mkdir(exist_ok=True)
    target = Path.home() / "Library" / "LaunchAgents" / "com.jobagent.bot.plist"
    target.parent.mkdir(parents=True, exist_ok=True); target.write_text(plist)
    print(f"Wrote {target}\nLoad it: launchctl bootstrap gui/$(id -u) {target}"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
