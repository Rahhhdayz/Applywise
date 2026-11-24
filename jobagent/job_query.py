"""Whitelisted job searches: bound SQLAlchemy queries and Python-only presentation."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, create_engine, func, or_, select
from sqlalchemy.sql import ColumnElement

from jobagent.db.models import (
    ApplicationPacketRecord,
    ApplicationStatusRecord,
    EmailResponseRecord,
    JobAssessmentRecord,
    JobRecord,
)
from jobagent.lifecycle import APPLICATION_STATUSES

WORD = r"^[A-Za-z0-9 .+#/&-]{1,40}$"
Status = Literal[tuple(sorted(APPLICATION_STATUSES))]
Word = Annotated[str, Field(pattern=WORD)]
ATS_HOSTS = {
    "workday": ("%myworkdayjobs.com%", "%myworkdaysite.com%"),
    "greenhouse": ("%greenhouse.io%",),
    "lever": ("%lever.co%",),
    "ashby": ("%ashbyhq.com%",),
    "icims": ("%icims.com%",),
    "smartrecruiters": ("%smartrecruiters.com%",),
    "oracle": ("%oraclecloud.com%",),
}


class JobQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_words: list[Word] = Field(default_factory=list, max_length=5)
    company: Annotated[str, Field(pattern=r"^[A-Za-z0-9 .+#/&,'-]{1,80}$")] | None = None
    status_in: list[Status] = Field(default_factory=list, max_length=11)
    status_from: date | None = None
    status_to: date | None = None
    found_from: date | None = None
    found_to: date | None = None
    source_in: list[
        Literal["linkedin", "indeed", "glassdoor", "google", "zip_recruiter", "manual"]
    ] = Field(default_factory=list)
    ats_in: list[
        Literal["workday", "greenhouse", "lever", "ashby", "icims", "smartrecruiters", "oracle"]
    ] = Field(default_factory=list)
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)
    location_words: list[Word] = Field(default_factory=list, max_length=3)
    remote: bool | None = None
    has_reply: bool | None = None
    reply_kind_in: list[
        Literal[
            "confirmation",
            "rejected",
            "recruiter_screen",
            "interview",
            "assessment",
            "offer",
            "other",
        ]
    ] = Field(default_factory=list)
    packet_ready: bool | None = None
    sort: Literal["newest", "score", "status_date"] = "newest"
    count_only: bool = False
    limit: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def ranges(self) -> Self:
        for start, end in ((self.status_from, self.status_to), (self.found_from, self.found_to)):
            if start and end and not 0 <= (end - start).days <= 730:
                raise ValueError("date ranges must be ordered and span at most 730 days")
        if (
            self.min_score is not None
            and self.max_score is not None
            and self.min_score > self.max_score
        ):
            raise ValueError("min_score must not exceed max_score")
        return self


def readonly_engine(database_path: Path) -> Engine:
    # as_uri escapes URI delimiters in filenames, preventing accidental query options.
    return create_engine(
        f"sqlite:///{database_path.resolve().as_uri()}?mode=ro&uri=true", connect_args={"uri": True}
    )


@dataclass(frozen=True)
class QueryRow:
    job_id: int
    company: str
    title: str
    status: str | None
    status_at: datetime | None
    score: int | None


class QueryRows(list[QueryRow]):
    """Limited rows plus full grouped totals for the existing (total, rows) interface."""

    def __init__(self) -> None:
        super().__init__()
        self.status_counts: dict[str | None, int] = {}


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _dates(column: ColumnElement, start: date | None, end: date | None) -> list[ColumnElement]:
    filters = []
    if start:
        filters.append(column >= datetime.combine(start, time.min).astimezone(UTC))
    if end and end < date.max:
        filters.append(column < datetime.combine(end + timedelta(days=1), time.min).astimezone(UTC))
    return filters


def run_query(engine: Engine, query: JobQuery) -> tuple[int, list[QueryRow]]:
    latest_status = (
        select(ApplicationStatusRecord.job_id, func.max(ApplicationStatusRecord.id).label("id"))
        .group_by(ApplicationStatusRecord.job_id)
        .subquery()
    )
    current = (
        select(ApplicationStatusRecord)
        .join(latest_status, ApplicationStatusRecord.id == latest_status.c.id)
        .subquery()
    )
    ranked_scores = (
        select(
            JobAssessmentRecord.job_id,
            JobAssessmentRecord.overall_score,
            func.row_number()
            .over(
                partition_by=JobAssessmentRecord.job_id,
                order_by=(JobAssessmentRecord.created_at.desc(), JobAssessmentRecord.id.desc()),
            )
            .label("rank"),
        )
        .where(JobAssessmentRecord.error_category.is_(None))
        .subquery()
    )
    assessment = (
        select(ranked_scores.c.job_id, ranked_scores.c.overall_score)
        .where(ranked_scores.c.rank == 1)
        .subquery()
    )
    score = assessment.c.overall_score
    statement = (
        select(
            JobRecord.id.label("job_id"),
            JobRecord.company,
            JobRecord.title,
            current.c.new_status.label("status"),
            current.c.created_at.label("status_at"),
            score.label("score"),
        )
        .outerjoin(current, current.c.job_id == JobRecord.id)
        .outerjoin(assessment, assessment.c.job_id == JobRecord.id)
    )
    filters = []
    for column, words in (
        (JobRecord.title, query.title_words),
        (JobRecord.location, query.location_words),
    ):
        if words:
            filters.append(or_(*(column.ilike(f"%{escape(word)}%", escape="\\") for word in words)))
    if query.company is not None:
        filters.append(JobRecord.company.ilike(f"%{escape(query.company)}%", escape="\\"))
    if query.status_in:
        filters.append(current.c.new_status.in_(query.status_in))
    if query.status_from or query.status_to:
        history = select(ApplicationStatusRecord.id).where(
            ApplicationStatusRecord.job_id == JobRecord.id,
            *_dates(ApplicationStatusRecord.created_at, query.status_from, query.status_to),
        )
        if query.status_in:
            history = history.where(ApplicationStatusRecord.new_status.in_(query.status_in))
        filters.append(history.exists())
    filters.extend(_dates(JobRecord.first_seen_at, query.found_from, query.found_to))
    if query.source_in:
        sources = [*query.source_in, *(["manual_url"] if "manual" in query.source_in else [])]
        filters.append(JobRecord.canonical_source.in_(sources))
    if query.ats_in:
        filters.append(
            or_(
                JobRecord.canonical_source.in_(query.ats_in),
                *(
                    JobRecord.apply_url.ilike(host)
                    for ats in query.ats_in
                    for host in ATS_HOSTS[ats]
                ),
            )
        )
    if query.min_score is not None:
        filters.append(score >= query.min_score)
    if query.max_score is not None:
        filters.append(score <= query.max_score)
    if query.remote is not None:
        remote = or_(
            func.coalesce(JobRecord.location, "").ilike("%remote%"),
            JobRecord.title.ilike("%remote%"),
        )
        filters.append(remote if query.remote else ~remote)
    email = select(EmailResponseRecord.id).where(EmailResponseRecord.job_id == JobRecord.id)
    if query.has_reply is not None:
        filters.append(email.exists() if query.has_reply else ~email.exists())
    if query.reply_kind_in:
        filters.append(
            email.where(EmailResponseRecord.classification.in_(query.reply_kind_in)).exists()
        )
    if query.packet_ready is not None:
        packet = (
            select(ApplicationPacketRecord.id)
            .where(
                ApplicationPacketRecord.job_id == JobRecord.id,
                ApplicationPacketRecord.status == "ready",
            )
            .exists()
        )
        filters.append(packet if query.packet_ready else ~packet)
    statement = statement.where(*filters)
    order = {
        "newest": JobRecord.first_seen_at.desc(),
        "score": score.desc().nulls_last(),
        "status_date": current.c.created_at.desc(),
    }[query.sort]
    rows = QueryRows()
    with engine.connect() as connection:
        filtered = statement.subquery()
        total = connection.scalar(select(func.count()).select_from(filtered)) or 0
        if query.count_only:
            rows.status_counts = dict(
                connection.execute(
                    select(filtered.c.status, func.count()).group_by(filtered.c.status)
                )
                .tuples()
                .all()
            )
        for row in connection.execute(
            statement.order_by(order, JobRecord.id.desc()).limit(query.limit)
        ):
            # SQLite drops timezone information; persisted timestamps are always UTC.
            at = row.status_at.replace(tzinfo=UTC) if row.status_at else None
            rows.append(QueryRow(row.job_id, row.company, row.title, row.status, at, row.score))
    return total, rows


def format_results(query: JobQuery, total: int, rows: list[QueryRow]) -> str:
    if query.count_only:
        counts = rows.status_counts if isinstance(rows, QueryRows) else {}
        return f"{total} job(s) match." + "".join(
            f"\n{status or 'no status'}: {count}"
            for status, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0] or ""))
        )
    if not total:
        filters = query.model_dump(mode="json", exclude_defaults=True)
        return "No jobs match.\nFilters: " + (
            ", ".join(f"{key}={value}" for key, value in filters.items()) or "none"
        )
    lines = [f"{total} match(es)" + (f", showing {len(rows)}" if total > len(rows) else "") + ":"]
    for row in rows:
        when = f" {row.status_at.astimezone():%Y-%m-%d}" if row.status_at else ""
        score = f" · score {row.score}" if row.score is not None else ""
        company, title = " ".join(row.company.split())[:60], " ".join(row.title.split())[:60]
        lines.append(
            f"#{row.job_id} {company} / {title} — {row.status or 'no status'}{when}{score}"
        )
    return "\n".join(lines)
