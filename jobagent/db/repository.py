"""High-level persistence operations for canonical jobs."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from jobagent.db.base import Base, create_sqlite_engine
from jobagent.db.models import (
    ApplicationPacketRecord,
    ApplicationQueueRecord,
    ApplicationStatusRecord,
    EligibilityRecord,
    EmailResponseRecord,
    FollowupRecord,
    JobAssessmentRecord,
    JobDiscoveryRecord,
    JobRecord,
    OutreachContactRecord,
    WorkflowRunRecord,
)
from jobagent.lifecycle import APPLICATION_STATUSES
from jobagent.models import JobPosting
from jobagent.normalization import canonical_dedupe_key, opening_key


@dataclass(frozen=True)
class Outreach:
    """A contact for one job plus the drafted email; Master reviews and sends it himself."""

    job_id: int
    name: str
    title: str
    email: str
    email_source: str  # "pattern" (unverified guess) | "hunter"
    contact_source: str  # "linkedin_poster" | "hunter"
    verified: bool = False
    confidence: int | None = None
    linkedin_url: str | None = None
    subject: str = ""
    body: str = ""
    draft_path: str | None = None
    created_at: datetime | None = None


def _to_outreach(record: OutreachContactRecord) -> Outreach:
    return Outreach(
        record.job_id, record.name, record.title, record.email, record.email_source, record.contact_source,
        record.verified, record.confidence, record.linkedin_url, record.subject, record.body, record.draft_path, record.created_at,
    )


@dataclass(frozen=True)
class Followup:
    id: int
    job_id: int
    number: int
    subject: str
    body: str
    draft_path: str | None
    created_at: datetime
    sent_at: datetime | None


def _to_followup(record: FollowupRecord) -> Followup:
    return Followup(
        record.id, record.job_id, record.number, record.subject, record.body, record.draft_path,
        _as_utc(record.created_at), _as_utc(record.sent_at) if record.sent_at else None,
    )


@dataclass(frozen=True)
class EmailResponse:
    """A processed employer email: what it was matched to, how it was read, and what changed."""

    message_id: str
    job_id: int | None
    received_at: datetime | None
    from_addr: str
    subject: str
    classification: str
    confidence: float
    applied_status_change: str | None
    needs_review: bool
    from_name: str = ""
    mailbox: str | None = None  # Gmail label/folder; None for rows recorded before it was stored
    id: int | None = None  # set when read back from the database


def _to_email_response(record: EmailResponseRecord) -> EmailResponse:
    return EmailResponse(record.message_id, record.job_id, _as_utc(record.received_at) if record.received_at else None,
                         record.from_addr, record.subject, record.classification, record.confidence,
                         record.applied_status_change, record.needs_review, record.from_name or "", record.mailbox, record.id)


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for persistence bookkeeping."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class StoredJob:
    """Database-independent canonical-job data returned to callers."""

    id: int
    canonical_dedupe_key: str
    canonical_source: str
    company: str
    title: str
    location: str | None
    source_job_id: str | None
    canonical_url: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    employment_type: str | None = None
    description_text: str | None = None
    apply_url: str | None = None
    resolved_destination_url: str | None = None
    resolved_ats: str | None = None
    resolution_evidence: str | None = None
    resolution_confidence: float | None = None
    resolved_at: datetime | None = None
    posted_at: datetime | None = None
    has_active_observation: bool | None = None
    source_metadata: dict[str, object] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class UpsertJobResult:
    """The canonical job and whether the current call created it."""

    job: StoredJob
    created: bool
    updated: bool = False


@dataclass(frozen=True)
class StoredEligibility:
    """Persisted deterministic eligibility result."""

    job_id: int
    status: str
    reasons: list[str]
    flags: list[str]
    detected_requirements: dict[str, object]
    rules_version: str
    rules_fingerprint: str
    evaluated_at: datetime


@dataclass(frozen=True)
class StoredAssessment:
    """Persisted, schema-validated semantic assessment."""

    id: int
    job_id: int
    overall_score: int
    role_alignment_score: int
    skills_score: int
    experience_score: int
    career_direction_score: int
    recommendation: str
    matched_skills: list[str]
    transferable_skills: list[str]
    meaningful_gaps: list[str]
    evidence_ids_used: list[str]
    strengths: list[str]
    concerns: list[str]
    summary_reason: str
    scorer: str
    model_identifier: str
    scoring_version: str
    candidate_fingerprint: str
    job_content_fingerprint: str
    created_at: datetime


@dataclass(frozen=True)
class QueueEntry:
    """Database-independent daily application queue entry."""

    id: int
    queue_date: str
    job_id: int
    rank: int
    ranking_score: float
    status: str
    exceptional: bool
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class ApplicationPacket:
    id: int
    job_id: int
    status: str
    preparation_fingerprint: str
    packet_directory: str | None
    notion_page_id: str | None
    ready_notified_at: datetime | None
    failure_reason: str | None
    resume_pdf_path: str | None = None
    cover_letter_pdf_path: str | None = None


class JobRepository:
    """Encapsulate all SQLAlchemy use for JobAgent callers."""

    def __init__(self, database_path: str | Path | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_path is None:
                raise ValueError("database_path is required when engine is not supplied")
            engine = create_sqlite_engine(database_path)
        self._engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def initialize(self) -> None:
        """Create the schema and migrate the Milestone 1 dedupe constraint if needed."""
        self._remove_legacy_dedupe_constraint()
        Base.metadata.create_all(self._engine)
        self._add_milestone_3_columns()
        self._add_email_columns()
        self._add_resolution_columns()

    def add_or_get_job(self, job: JobPosting) -> UpsertJobResult:
        """Insert a canonical job once and record the current source observation."""
        dedupe_key = canonical_dedupe_key(job)
        seen_at = job.discovered_at

        with self._sessions.begin() as session:
            existing_discovery = session.scalar(
                select(JobDiscoveryRecord).where(JobDiscoveryRecord.source_identity == job.source_identity)
            )
            if existing_discovery is not None:
                updated = self._refresh_exact_source_job(existing_discovery.job, job)
                existing_discovery.last_seen_at = _latest_timestamp(existing_discovery.last_seen_at, seen_at)
                existing_discovery.source_metadata = job.source_metadata_dict()
                existing_discovery.is_active = True
                existing_discovery.job.last_seen_at = _latest_timestamp(
                    existing_discovery.job.last_seen_at, seen_at
                )
                return UpsertJobResult(
                    job=self._to_stored(existing_discovery.job), created=False, updated=updated
                )

            record = self._find_equivalent_canonical_url(session, job)
            created = record is None
            if record is None:
                record = JobRecord(
                    canonical_dedupe_key=dedupe_key,
                    canonical_source=job.source,
                    source_job_id=job.source_job_id,
                    company=job.company,
                    title=job.title,
                    location=job.location,
                    employment_type=job.employment_type,
                    description_text=job.description_text,
                    apply_url=job.apply_url,
                    canonical_url=job.canonical_url,
                    posted_at=job.posted_at,
                    discovered_at=job.discovered_at,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    source_metadata=job.source_metadata_dict(),
                )
                session.add(record)
                session.flush()
            else:
                record.last_seen_at = _latest_timestamp(record.last_seen_at, seen_at)

            session.add(
                JobDiscoveryRecord(
                    job_id=record.id,
                    source=job.source,
                    source_identity=job.source_identity,
                    source_job_id=job.source_job_id,
                    canonical_url=job.canonical_url,
                    source_metadata=job.source_metadata_dict(),
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    is_active=True,
                )
            )
            return UpsertJobResult(job=self._to_stored(record), created=created)

    def get_job(self, job_id: int) -> StoredJob | None:
        """Return one canonical job by its local database identifier."""
        with self._sessions() as session:
            record = session.get(JobRecord, job_id)
            return self._to_stored(record) if record is not None else None

    def list_jobs(self) -> list[StoredJob]:
        """Return canonical jobs in their original discovery order."""
        with self._sessions() as session:
            records = session.scalars(select(JobRecord).order_by(JobRecord.first_seen_at, JobRecord.id)).all()
            return [self._to_stored(record) for record in records]

    def find_opening(self, company: str, title: str) -> list[StoredJob]:
        """Every stored listing of one opening (same company + title), oldest first."""
        # ponytail: Python-side scan of all jobs (~6K, milliseconds); add a normalized column if it grows
        key = opening_key(company, title)
        return [job for job in self.list_jobs() if opening_key(job.company, job.title) == key]

    def set_job_apply_url(self, job_id: int, url: str) -> None:
        """Point a job at its real application form (e.g. the ATS behind a LinkedIn listing)."""
        with self._sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            if record: record.apply_url = url

    def set_job_resolution(
        self, job_id: int, *, destination_url: str | None, ats: str,
        evidence: str, confidence: float,
    ) -> None:
        """Save deterministic resolution separately from original URLs and provenance."""
        if not 0 <= confidence <= 1:
            raise ValueError("Resolution confidence must be between zero and one")
        with self._sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise ValueError("Job not found")
            record.resolved_destination_url = destination_url
            record.resolved_ats = ats
            record.resolution_evidence = evidence
            record.resolution_confidence = confidence
            record.resolved_at = utc_now()

    def list_pending_resolutions(self, limit: int | None = None) -> list[StoredJob]:
        """Eligible (latest evaluation) or queued jobs, excluding already handled applications."""
        latest_eligibility = (
            select(EligibilityRecord.status).where(EligibilityRecord.job_id == JobRecord.id)
            .order_by(EligibilityRecord.evaluated_at.desc(), EligibilityRecord.id.desc())
            .limit(1).correlate(JobRecord).scalar_subquery()
        )
        queued = select(ApplicationQueueRecord.id).where(
            ApplicationQueueRecord.job_id == JobRecord.id, ApplicationQueueRecord.status == "queued"
        ).exists()
        handled = select(ApplicationStatusRecord.id).where(ApplicationStatusRecord.job_id == JobRecord.id).exists()
        statement = select(JobRecord).where(
            JobRecord.resolved_destination_url.is_(None),
            (JobRecord.resolved_ats.is_(None) | (JobRecord.resolved_ats != "linkedin_easy_apply")),
            (latest_eligibility == "eligible") | queued, ~handled,
        ).order_by(JobRecord.id)
        if limit is not None:
            statement = statement.limit(max(0, limit))
        with self._sessions() as session:
            return [self._to_stored(record) for record in session.scalars(statement).all()]

    def count_jobs(self) -> int:
        """Return the number of canonical jobs."""
        with self._sessions() as session:
            return len(session.scalars(select(JobRecord.id)).all())

    def upsert_eligibility(
        self,
        *,
        job_id: int,
        status: str,
        reasons: list[str],
        flags: list[str],
        detected_requirements: dict[str, object],
        rules_version: str,
        rules_fingerprint: str,
    ) -> StoredEligibility:
        """Store one current result for a job/rule fingerprint without duplicate rows."""
        if status not in {"eligible", "review", "ineligible"}:
            raise ValueError(f"Unsupported eligibility status: {status}")
        with self._sessions.begin() as session:
            record = session.scalar(
                select(EligibilityRecord).where(
                    EligibilityRecord.job_id == job_id,
                    EligibilityRecord.rules_fingerprint == rules_fingerprint,
                )
            )
            values = {
                "status": status,
                "reasons": reasons,
                "flags": flags,
                "detected_requirements": detected_requirements,
                "rules_version": rules_version,
                "evaluated_at": utc_now(),
            }
            if record is None:
                record = EligibilityRecord(job_id=job_id, rules_fingerprint=rules_fingerprint, **values)
                session.add(record)
                session.flush()
            else:
                for field, value in values.items():
                    setattr(record, field, value)
            return self._to_eligibility(record)

    def get_eligibility(self, job_id: int, rules_fingerprint: str | None = None) -> StoredEligibility | None:
        """Get a job's current result, optionally for one exact rules fingerprint."""
        with self._sessions() as session:
            statement = select(EligibilityRecord).where(EligibilityRecord.job_id == job_id)
            if rules_fingerprint:
                statement = statement.where(EligibilityRecord.rules_fingerprint == rules_fingerprint)
            record = session.scalar(statement.order_by(EligibilityRecord.evaluated_at.desc()))
            return self._to_eligibility(record) if record is not None else None

    def list_eligibility(self, *, status: str | None = None) -> list[StoredEligibility]:
        """List persisted results in deterministic job/id order."""
        with self._sessions() as session:
            statement = select(EligibilityRecord).order_by(EligibilityRecord.job_id, EligibilityRecord.id)
            if status:
                statement = statement.where(EligibilityRecord.status == status)
            return [self._to_eligibility(record) for record in session.scalars(statement).all()]

    def find_assessment(
        self,
        *,
        job_id: int,
        candidate_fingerprint: str,
        job_content_fingerprint: str,
        scoring_version: str,
        model_identifier: str,
    ) -> StoredAssessment | None:
        """Find exactly reusable assessment input/output provenance."""
        with self._sessions() as session:
            record = session.scalar(
                select(JobAssessmentRecord).where(
                    JobAssessmentRecord.job_id == job_id,
                    JobAssessmentRecord.candidate_fingerprint == candidate_fingerprint,
                    JobAssessmentRecord.job_content_fingerprint == job_content_fingerprint,
                    JobAssessmentRecord.scoring_version == scoring_version,
                    JobAssessmentRecord.model_identifier == model_identifier,
                )
            )
            return self._to_assessment(record) if record is not None else None

    def add_assessment(
        self,
        *,
        job_id: int,
        assessment: object,
        scorer: str,
        model_identifier: str,
        scoring_version: str,
        candidate_fingerprint: str,
        job_content_fingerprint: str,
    ) -> StoredAssessment:
        """Persist validated assessment fields, keeping only concise user-facing reasoning."""
        values = assessment.model_dump()  # type: ignore[attr-defined]
        with self._sessions.begin() as session:
            record = session.scalar(
                select(JobAssessmentRecord).where(
                    JobAssessmentRecord.job_id == job_id,
                    JobAssessmentRecord.candidate_fingerprint == candidate_fingerprint,
                    JobAssessmentRecord.job_content_fingerprint == job_content_fingerprint,
                    JobAssessmentRecord.scoring_version == scoring_version,
                    JobAssessmentRecord.model_identifier == model_identifier,
                )
            )
            if record is None:
                record = JobAssessmentRecord(
                    job_id=job_id,
                    scorer=scorer,
                    model_identifier=model_identifier,
                    scoring_version=scoring_version,
                    candidate_fingerprint=candidate_fingerprint,
                    job_content_fingerprint=job_content_fingerprint,
                    created_at=utc_now(),
                    **values,
                )
                session.add(record)
                session.flush()
            else:
                for field, value in values.items():
                    setattr(record, field, value)
                record.scorer = scorer
                record.created_at = utc_now()
            return self._to_assessment(record)

    def get_latest_assessment(self, job_id: int) -> StoredAssessment | None:
        """Return most recently stored assessment for inspection and queue building."""
        with self._sessions() as session:
            record = session.scalar(
                select(JobAssessmentRecord)
                .where(JobAssessmentRecord.job_id == job_id)
                .order_by(JobAssessmentRecord.created_at.desc(), JobAssessmentRecord.id.desc())
            )
            return self._to_assessment(record) if record is not None else None

    def list_queue(self, queue_date: str) -> list[QueueEntry]:
        """Return a day's stable queue order."""
        with self._sessions() as session:
            records = session.scalars(
                select(ApplicationQueueRecord)
                .where(ApplicationQueueRecord.queue_date == queue_date)
                .order_by(ApplicationQueueRecord.rank, ApplicationQueueRecord.id)
            ).all()
            return [self._to_queue_entry(record) for record in records]

    def add_queue_entries(self, queue_date: str, entries: list[dict[str, object]]) -> list[QueueEntry]:
        """Append new jobs to a day's queue (already-queued jobs are ignored); return the whole day."""
        with self._sessions.begin() as session:
            queued = set(session.scalars(select(ApplicationQueueRecord.job_id).where(ApplicationQueueRecord.queue_date == queue_date)).all())
            session.add_all(
                ApplicationQueueRecord(queue_date=queue_date, created_at=utc_now(), status="queued", **entry)
                for entry in entries
                if entry["job_id"] not in queued
            )
        return self.list_queue(queue_date)

    def latest_queue_date(self, job_id: int) -> str | None:
        """Return the most recent day a job was queued, if ever."""
        with self._sessions() as session:
            return session.scalar(
                select(ApplicationQueueRecord.queue_date)
                .where(ApplicationQueueRecord.job_id == job_id)
                .order_by(ApplicationQueueRecord.queue_date.desc())
            )

    def tracked_job_ids(self) -> set[int]:
        """Jobs already queued on any day or given any application status; never re-queued."""
        with self._sessions() as session:
            queued = session.scalars(select(ApplicationQueueRecord.job_id)).all()
            statused = session.scalars(select(ApplicationStatusRecord.job_id)).all()
            return set(queued) | set(statused)

    def get_packet_by_fingerprint(self, job_id: int, fingerprint: str) -> ApplicationPacket | None:
        with self._sessions() as session:
            record = session.scalar(select(ApplicationPacketRecord).where(ApplicationPacketRecord.job_id == job_id, ApplicationPacketRecord.preparation_fingerprint == fingerprint))
            return self._to_packet(record) if record else None

    def get_latest_ready_packet(self, job_id: int) -> ApplicationPacket | None:
        """Return the most recently prepared, ready-to-fill packet for a job, if any."""
        with self._sessions() as session:
            record = session.scalar(
                select(ApplicationPacketRecord)
                .where(ApplicationPacketRecord.job_id == job_id, ApplicationPacketRecord.status == "ready")
                .order_by(ApplicationPacketRecord.updated_at.desc(), ApplicationPacketRecord.id.desc())
            )
            return self._to_packet(record) if record else None

    def save_packet(self, *, job_id: int, fingerprint: str, status: str, packet_directory: str | None = None, paths: dict[str, str] | None = None, validation_state: str | None = None, writing_model: str | None = None, skill_metadata: dict[str, object] | None = None, failure_reason: str | None = None) -> ApplicationPacket:
        """Upsert a packet state so failed preparation is retryable and ready packets are immutable."""
        with self._sessions.begin() as session:
            record = session.scalar(select(ApplicationPacketRecord).where(ApplicationPacketRecord.job_id == job_id, ApplicationPacketRecord.preparation_fingerprint == fingerprint))
            now = utc_now()
            if record is not None and record.status == "ready" and status != "ready":
                return self._to_packet(record)  # a failed --force retry must not erase the good packet
            if record is None:
                # One Notion row per job: a re-prepared packet inherits the job's existing page.
                page_id = session.scalar(
                    select(ApplicationPacketRecord.notion_page_id)
                    .where(ApplicationPacketRecord.job_id == job_id, ApplicationPacketRecord.notion_page_id.is_not(None))
                    .order_by(ApplicationPacketRecord.id.desc())
                )
                record = ApplicationPacketRecord(job_id=job_id, version="v1", preparation_fingerprint=fingerprint, status=status, notion_page_id=page_id, created_at=now, updated_at=now)
                session.add(record)
            record.status, record.packet_directory, record.validation_state = status, packet_directory, validation_state
            record.writing_model, record.skill_metadata, record.failure_reason, record.updated_at = writing_model, skill_metadata or {}, _bounded_error(failure_reason), now
            for field, value in (paths or {}).items():
                if hasattr(record, field): setattr(record, field, value)
            session.flush()
            return self._to_packet(record)

    def list_packets(self, *, status: str | None = None) -> list[ApplicationPacket]:
        with self._sessions() as session:
            statement = select(ApplicationPacketRecord).order_by(ApplicationPacketRecord.id)
            if status: statement = statement.where(ApplicationPacketRecord.status == status)
            return [self._to_packet(item) for item in session.scalars(statement).all()]

    def set_packet_notion_page(self, packet_id: int, page_id: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(ApplicationPacketRecord, packet_id)
            if record: record.notion_page_id, record.updated_at = page_id, utc_now()

    def mark_packet_notified(self, packet_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(ApplicationPacketRecord, packet_id)
            if record and record.ready_notified_at is None: record.ready_notified_at = utc_now()

    def save_outreach(self, outreach: Outreach) -> Outreach:
        """Insert or replace the job's one outreach contact and draft."""
        values = {k: v for k, v in outreach.__dict__.items() if k != "created_at"}
        with self._sessions.begin() as session:
            record = session.scalar(select(OutreachContactRecord).where(OutreachContactRecord.job_id == outreach.job_id))
            if record is None:
                record = OutreachContactRecord(**values, created_at=utc_now())
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            session.flush()
            return _to_outreach(record)

    def get_outreach(self, job_id: int) -> Outreach | None:
        with self._sessions() as session:
            record = session.scalar(select(OutreachContactRecord).where(OutreachContactRecord.job_id == job_id))
            return _to_outreach(record) if record else None

    def add_followup(self, job_id: int, subject: str, body: str, draft_path: str | None) -> Followup:
        """Record a new drafted follow-up; its number is one more than the job's previous ones."""
        with self._sessions.begin() as session:
            count = len(session.scalars(select(FollowupRecord.id).where(FollowupRecord.job_id == job_id)).all())
            record = FollowupRecord(job_id=job_id, number=count + 1, subject=subject, body=body, draft_path=draft_path, created_at=utc_now())
            session.add(record)
            session.flush()
            return _to_followup(record)

    def list_followups(self, job_id: int | None = None) -> list[Followup]:
        """Follow-ups, oldest first; all jobs unless ``job_id`` is given."""
        with self._sessions() as session:
            query = select(FollowupRecord).order_by(FollowupRecord.id)
            if job_id is not None:
                query = query.where(FollowupRecord.job_id == job_id)
            return [_to_followup(record) for record in session.scalars(query).all()]

    def mark_followup_sent(self, job_id: int) -> Followup | None:
        """Mark the job's latest unsent follow-up as sent now; None if there is none."""
        with self._sessions.begin() as session:
            record = session.scalar(
                select(FollowupRecord).where(FollowupRecord.job_id == job_id, FollowupRecord.sent_at.is_(None)).order_by(FollowupRecord.id.desc())
            )
            if record is None:
                return None
            record.sent_at = utc_now()
            session.flush()
            return _to_followup(record)

    def processed_message_ids(self) -> set[str]:
        with self._sessions() as session:
            return set(session.scalars(select(EmailResponseRecord.message_id)).all())

    def add_email_response(self, response: EmailResponse) -> None:
        """Record one processed email; a Message-ID seen before is ignored."""
        with self._sessions.begin() as session:
            if session.scalar(select(EmailResponseRecord.id).where(EmailResponseRecord.message_id == response.message_id)):
                return
            values = {key: value for key, value in response.__dict__.items() if key != "id"}
            session.add(EmailResponseRecord(**{**values, "subject": response.subject[:1000], "from_name": response.from_name[:512]},
                                            created_at=utc_now()))

    def list_email_responses(self, *, needs_review: bool | None = None) -> list[EmailResponse]:
        """Processed emails, newest received first; only the review queue when ``needs_review`` is True."""
        with self._sessions() as session:
            query = select(EmailResponseRecord).order_by(EmailResponseRecord.received_at.desc(), EmailResponseRecord.id.desc())
            if needs_review is not None:
                query = query.where(EmailResponseRecord.needs_review.is_(needs_review))
            return [_to_email_response(record) for record in session.scalars(query).all()]

    def get_email_response(self, email_id: int) -> EmailResponse | None:
        with self._sessions() as session:
            record = session.get(EmailResponseRecord, email_id)
            return _to_email_response(record) if record else None

    def link_email_response(self, email_id: int, job_id: int) -> None:
        """Attach a processed email to a job; it no longer needs review."""
        with self._sessions.begin() as session:
            record = session.get(EmailResponseRecord, email_id)
            if record:
                record.job_id, record.needs_review = job_id, False

    def set_application_status(self, job_id: int, status: str, *, source: str = "cli") -> None:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"Unsupported application status: {status} (use one of {', '.join(sorted(APPLICATION_STATUSES))})")
        with self._sessions.begin() as session:
            prior = session.scalar(select(ApplicationStatusRecord.new_status).where(ApplicationStatusRecord.job_id == job_id).order_by(ApplicationStatusRecord.id.desc()))
            if prior != status: session.add(ApplicationStatusRecord(job_id=job_id, prior_status=prior, new_status=status, source=source, created_at=utc_now()))

    def latest_statuses(self) -> dict[int, str]:
        """Return each job's most recently recorded status."""
        with self._sessions() as session:
            rows = session.scalars(select(ApplicationStatusRecord).order_by(ApplicationStatusRecord.id)).all()
            return {row.job_id: row.new_status for row in rows}

    def status_history(self) -> list[tuple[int, str, datetime]]:
        """Every recorded status change, oldest first: (job_id, status, UTC timestamp)."""
        with self._sessions() as session:
            rows = session.scalars(select(ApplicationStatusRecord).order_by(ApplicationStatusRecord.id)).all()
            return [(row.job_id, row.new_status, _as_utc(row.created_at)) for row in rows]

    def status_since(self, job_id: int) -> datetime | None:
        """When the job's current status was recorded (UTC), or None if it has none."""
        with self._sessions() as session:
            when = session.scalar(select(ApplicationStatusRecord.created_at).where(ApplicationStatusRecord.job_id == job_id).order_by(ApplicationStatusRecord.id.desc()))
            return _as_utc(when) if when else None

    def status_counts(self) -> dict[str, int]:
        """Return counts of each job's most recently recorded status."""
        latest = list(self.latest_statuses().values())
        return {status: latest.count(status) for status in set(latest)}

    def start_workflow_run(self, workflow_type: str = "cycle") -> int:
        with self._sessions.begin() as session:
            record = WorkflowRunRecord(workflow_type=workflow_type, status="running", started_at=utc_now())
            session.add(record); session.flush(); return record.id

    def complete_workflow_run(self, run_id: int, *, status: str, metrics: dict[str, object], error: str | None = None) -> None:
        with self._sessions.begin() as session:
            record = session.get(WorkflowRunRecord, run_id)
            if record: record.status, record.metrics, record.error_message, record.completed_at = status, metrics, _bounded_error(error), utc_now()

    def cycle_run(self, queue_date: str) -> tuple[str, datetime, datetime | None, dict[str, object]] | None:
        """The latest cycle run for one queue date: (status, started UTC, completed UTC, metrics)."""
        with self._sessions() as session:
            runs = session.scalars(select(WorkflowRunRecord).where(WorkflowRunRecord.workflow_type == "cycle")
                                   .order_by(WorkflowRunRecord.id.desc())).all()
            for run in runs:
                if (run.metrics or {}).get("queue_date") == queue_date:
                    done = _as_utc(run.completed_at) if run.completed_at else None
                    return run.status, _as_utc(run.started_at), done, dict(run.metrics)
        return None

    def _remove_legacy_dedupe_constraint(self) -> None:
        """Migrate the original unique similarity key without losing existing SQLite data."""
        inspector = inspect(self._engine)
        if "jobs" not in inspector.get_table_names():
            return
        # Early databases enforce the similarity key through a UNIQUE index instead of a
        # constraint; real requisitions can share it, so make that index non-unique.
        for index in inspector.get_indexes("jobs"):
            if index.get("unique") and index.get("column_names") == ["canonical_dedupe_key"]:
                with self._engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP INDEX "{index["name"]}"')
                    connection.exec_driver_sql(
                        "CREATE INDEX ix_jobs_canonical_dedupe_key ON jobs (canonical_dedupe_key)"
                    )
        constraints = inspector.get_unique_constraints("jobs")
        is_legacy_schema = any(
            constraint.get("column_names") == ["canonical_dedupe_key"] for constraint in constraints
        )
        if not is_legacy_schema:
            return

        with self._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("ALTER TABLE job_discoveries RENAME TO job_discoveries_m1")
            connection.exec_driver_sql("ALTER TABLE jobs RENAME TO jobs_m1")
            Base.metadata.create_all(connection)
            connection.exec_driver_sql(
                """
                INSERT INTO jobs (
                    id, canonical_dedupe_key, canonical_source, source_job_id, company, title,
                    location, employment_type, description_text, apply_url, canonical_url,
                    posted_at, discovered_at, first_seen_at, last_seen_at, source_metadata
                )
                SELECT
                    id, canonical_dedupe_key, canonical_source, source_job_id, company, title,
                    location, employment_type, description_text, apply_url, canonical_url,
                    posted_at, discovered_at, first_seen_at, last_seen_at, source_metadata
                FROM jobs_m1
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO job_discoveries (
                    id, job_id, source, source_identity, source_job_id, canonical_url,
                    source_metadata, first_seen_at, last_seen_at
                )
                SELECT
                    id, job_id, source, source_identity, source_job_id, canonical_url,
                    source_metadata, first_seen_at, last_seen_at
                FROM job_discoveries_m1
                """
            )
            connection.exec_driver_sql("DROP TABLE job_discoveries_m1")
            connection.exec_driver_sql("DROP TABLE jobs_m1")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    def _add_milestone_3_columns(self) -> None:
        """Add source-observation columns to SQLite databases created before Milestone 3."""
        inspector = inspect(self._engine)
        if "job_discoveries" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("job_discoveries")}
        with self._engine.begin() as connection:
            if "source_target_id" not in columns:
                connection.exec_driver_sql("ALTER TABLE job_discoveries ADD COLUMN source_target_id INTEGER")
            if "last_observed_run_id" not in columns:
                connection.exec_driver_sql("ALTER TABLE job_discoveries ADD COLUMN last_observed_run_id INTEGER")
            if "is_active" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE job_discoveries ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_job_discoveries_source_target_id "
                "ON job_discoveries (source_target_id)"
            )

    def _add_email_columns(self) -> None:
        """Add sender-name and mailbox columns to email_responses tables created before they existed."""
        columns = {column["name"] for column in inspect(self._engine).get_columns("email_responses")}
        with self._engine.begin() as connection:
            if "from_name" not in columns:
                connection.exec_driver_sql("ALTER TABLE email_responses ADD COLUMN from_name VARCHAR(512) NOT NULL DEFAULT ''")
            if "mailbox" not in columns:
                connection.exec_driver_sql("ALTER TABLE email_responses ADD COLUMN mailbox VARCHAR(200)")

    def _add_resolution_columns(self) -> None:
        """Idempotent additive migration; existing source URLs remain untouched."""
        columns = {column["name"] for column in inspect(self._engine).get_columns("jobs")}
        additions = {
            "resolved_destination_url": "TEXT", "resolved_ats": "VARCHAR(64)",
            "resolution_evidence": "TEXT", "resolution_confidence": "FLOAT", "resolved_at": "DATETIME",
        }
        with self._engine.begin() as connection:
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _find_equivalent_canonical_url(session, job: JobPosting) -> JobRecord | None:  # type: ignore[no-untyped-def]
        """Match only an exact canonical URL after source identity misses.

        The normalized company/title/location key remains a similarity/grouping key;
        it is intentionally not a uniqueness rule because real requisitions can share
        those fields.
        """
        if job.canonical_url is None:
            return None
        return session.scalar(select(JobRecord).where(JobRecord.canonical_url == job.canonical_url))

    @staticmethod
    def _to_stored(record: JobRecord) -> StoredJob:
        active_observations = [discovery.is_active for discovery in record.discoveries]
        return StoredJob(
            id=record.id,
            canonical_dedupe_key=record.canonical_dedupe_key,
            canonical_source=record.canonical_source,
            company=record.company,
            title=record.title,
            location=record.location,
            source_job_id=record.source_job_id,
            canonical_url=record.canonical_url,
            first_seen_at=_as_utc(record.first_seen_at),
            last_seen_at=_as_utc(record.last_seen_at),
            employment_type=record.employment_type,
            description_text=record.description_text,
            apply_url=record.apply_url,
            resolved_destination_url=record.resolved_destination_url,
            resolved_ats=record.resolved_ats,
            resolution_evidence=record.resolution_evidence,
            resolution_confidence=record.resolution_confidence,
            resolved_at=_as_utc(record.resolved_at) if record.resolved_at else None,
            posted_at=_as_utc(record.posted_at) if record.posted_at else None,
            has_active_observation=any(active_observations) if active_observations else None,
            source_metadata=dict(record.source_metadata or {}),
        )

    @staticmethod
    def _to_eligibility(record: EligibilityRecord) -> StoredEligibility:
        return StoredEligibility(
            job_id=record.job_id,
            status=record.status,
            reasons=list(record.reasons or []),
            flags=list(record.flags or []),
            detected_requirements=dict(record.detected_requirements or {}),
            rules_version=record.rules_version,
            rules_fingerprint=record.rules_fingerprint,
            evaluated_at=_as_utc(record.evaluated_at),
        )

    @staticmethod
    def _to_assessment(record: JobAssessmentRecord) -> StoredAssessment:
        return StoredAssessment(
            id=record.id,
            job_id=record.job_id,
            overall_score=record.overall_score,
            role_alignment_score=record.role_alignment_score,
            skills_score=record.skills_score,
            experience_score=record.experience_score,
            career_direction_score=record.career_direction_score,
            recommendation=record.recommendation,
            matched_skills=list(record.matched_skills or []),
            transferable_skills=list(record.transferable_skills or []),
            meaningful_gaps=list(record.meaningful_gaps or []),
            evidence_ids_used=list(record.evidence_ids_used or []),
            strengths=list(record.strengths or []),
            concerns=list(record.concerns or []),
            summary_reason=record.summary_reason,
            scorer=record.scorer,
            model_identifier=record.model_identifier,
            scoring_version=record.scoring_version,
            candidate_fingerprint=record.candidate_fingerprint,
            job_content_fingerprint=record.job_content_fingerprint,
            created_at=_as_utc(record.created_at),
        )

    @staticmethod
    def _to_queue_entry(record: ApplicationQueueRecord) -> QueueEntry:
        return QueueEntry(
            id=record.id,
            queue_date=record.queue_date,
            job_id=record.job_id,
            rank=record.rank,
            ranking_score=float(record.ranking_score),
            status=record.status,
            exceptional=record.exceptional,
            reason=record.reason,
            created_at=_as_utc(record.created_at),
        )

    @staticmethod
    def _to_packet(record: ApplicationPacketRecord) -> ApplicationPacket:
        return ApplicationPacket(record.id, record.job_id, record.status, record.preparation_fingerprint, record.packet_directory, record.notion_page_id, _as_utc(record.ready_notified_at) if record.ready_notified_at else None, record.failure_reason, record.resume_pdf_path, record.cover_letter_pdf_path)

    @staticmethod
    def _refresh_exact_source_job(record: JobRecord, job: JobPosting) -> bool:
        """Refresh mutable job data only after an exact source-identity match."""
        fields = (
            "company",
            "title",
            "location",
            "employment_type",
            "description_text",
            "apply_url",
            "canonical_url",
            "posted_at",
            "source_metadata",
        )
        updated = False
        for field in fields:
            incoming = job.source_metadata_dict() if field == "source_metadata" else getattr(job, field)
            current = getattr(record, field)
            if not _refresh_improves(field, current, incoming):
                continue
            if field == "source_metadata" and "submitted_url" in (current or {}):
                incoming = {**incoming, "submitted_url": current["submitted_url"]}  # stays hand-picked
            if isinstance(current, datetime):
                current = _as_utc(current)  # SQLite returns naive datetimes; never equal to aware ones
            if current != incoming:
                setattr(record, field, incoming)
                updated = True
        return updated


def _refresh_improves(field: str, current: object, incoming: object) -> bool:
    """A re-scrape must not undo a resolved ATS link or a fuller description (e.g. LinkedIn over Ashby)."""
    from jobagent.intake.urls import detect_source  # local: jobagent.intake imports this module
    if field in {"apply_url", "canonical_url"} and isinstance(current, str):
        return not (detect_source(current) != "manual_url" and detect_source(incoming or "") == "manual_url")
    if field == "description_text" and isinstance(current, str):
        # ponytail: longer wins; a posting genuinely shortened upstream keeps its old text
        return len(incoming or "") >= len(current)
    return True


def _as_utc(value: datetime) -> datetime:
    """Restore SQLite timestamps to the project's UTC-aware domain convention."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _latest_timestamp(current: datetime, candidate: datetime) -> datetime:
    """Keep last-seen bookkeeping monotonic when source events arrive out of order."""
    return max(_as_utc(current), _as_utc(candidate))


def _bounded_error(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()[:1_000] or None
