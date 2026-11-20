"""SQLAlchemy persistence models, private to the database layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobagent.db.base import Base


class JobRecord(Base):
    """Canonical job record stored in SQLite."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    canonical_source: Mapped[str] = mapped_column(String(255))
    source_job_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    discoveries: Mapped[list[JobDiscoveryRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobDiscoveryRecord(Base):
    """One source observation associated with a canonical job."""

    __tablename__ = "job_discoveries"
    __table_args__ = (UniqueConstraint("source_identity", name="uq_job_discoveries_source_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    source: Mapped[str] = mapped_column(String(255))
    source_identity: Mapped[str] = mapped_column(String(2048))
    source_job_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # ponytail: legacy ATS-board columns, kept nullable so old SQLite files still load.
    source_target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_observed_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    job: Mapped[JobRecord] = relationship(back_populates="discoveries")


class EligibilityRecord(Base):
    """Latest deterministic eligibility evaluation for a canonical job and rule set."""

    __tablename__ = "job_eligibility"
    __table_args__ = (UniqueConstraint("job_id", "rules_fingerprint", name="uq_job_eligibility_rule"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    detected_requirements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rules_version: Mapped[str] = mapped_column(String(64))
    rules_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JobAssessmentRecord(Base):
    """A concise, validated semantic assessment; never a model chain of thought."""

    __tablename__ = "job_assessments"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "candidate_fingerprint",
            "job_content_fingerprint",
            "scoring_version",
            "model_identifier",
            name="uq_job_assessment_cache",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    overall_score: Mapped[int] = mapped_column(Integer)
    role_alignment_score: Mapped[int] = mapped_column(Integer)
    skills_score: Mapped[int] = mapped_column(Integer)
    experience_score: Mapped[int] = mapped_column(Integer)
    career_direction_score: Mapped[int] = mapped_column(Integer)
    recommendation: Mapped[str] = mapped_column(String(32))
    matched_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    transferable_skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    meaningful_gaps: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_ids_used: Mapped[list[str]] = mapped_column(JSON, default=list)
    strengths: Mapped[list[str]] = mapped_column(JSON, default=list)
    concerns: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary_reason: Mapped[str] = mapped_column(Text)
    scorer: Mapped[str] = mapped_column(String(64))
    model_identifier: Mapped[str] = mapped_column(String(256))
    scoring_version: Mapped[str] = mapped_column(String(64))
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    job_content_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApplicationQueueRecord(Base):
    """A deterministic daily set of jobs selected for human application preparation."""

    __tablename__ = "application_queue"
    __table_args__ = (UniqueConstraint("queue_date", "job_id", name="uq_application_queue_date_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_date: Mapped[str] = mapped_column(String(10), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    ranking_score: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="queued")
    exceptional: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApplicationPacketRecord(Base):
    __tablename__ = "application_packets"
    __table_args__ = (UniqueConstraint("job_id", "preparation_fingerprint", name="uq_packet_fingerprint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    queue_id: Mapped[int | None] = mapped_column(ForeignKey("application_queue.id"), nullable=True)
    version: Mapped[str] = mapped_column(String(64))
    preparation_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    packet_directory: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_letter_docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_letter_pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    writing_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    skill_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notion_page_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ready_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApplicationStatusRecord(Base):
    __tablename__ = "application_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    prior_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkflowRunRecord(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutreachContactRecord(Base):
    """One person to contact per job, with the drafted email. Nothing here is ever sent automatically."""

    __tablename__ = "outreach_contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(320))
    email_source: Mapped[str] = mapped_column(String(32))  # "pattern" (unverified guess) | "hunter"
    contact_source: Mapped[str] = mapped_column(String(32))  # "linkedin_poster" | "hunter"
    verified: Mapped[bool] = mapped_column(default=False)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    draft_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FollowupRecord(Base):
    """One drafted follow-up on a submitted application. Drafts only; Master sends them himself."""

    __tablename__ = "followups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)  # 1st, 2nd follow-up on this application
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    draft_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailResponseRecord(Base):
    """One processed employer email (by Message-ID), so the inbox check never handles it twice."""

    __tablename__ = "email_responses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(998), unique=True, index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    from_addr: Mapped[str] = mapped_column(String(512), default="")
    from_name: Mapped[str] = mapped_column(String(512), default="")
    mailbox: Mapped[str | None] = mapped_column(String(200), nullable=True)  # Gmail label/folder it was read from
    subject: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(default=0.0)
    applied_status_change: Mapped[str | None] = mapped_column(String(32), nullable=True)
    needs_review: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
