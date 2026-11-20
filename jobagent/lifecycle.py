"""Application lifecycle: which statuses exist and which moves a pulled (Notion) decision may make."""
from __future__ import annotations

# Forward progress, earliest first. A pulled status may only move a job further along this line.
PROGRESS = ("ready", "prefilled", "applied", "recruiter_screen", "interview", "offer")
# Outcomes that close an application. "ghosted" can still be reopened by a reply (forward move).
TERMINAL = ("rejected", "ghosted", "skipped", "expired")
APPLICATION_STATUSES = frozenset(PROGRESS + TERMINAL)
# Still waiting on the employer: these are the applications a follow-up can chase.
AWAITING_REPLY = frozenset({"applied", "recruiter_screen", "interview"})


def status_from_label(label: str) -> str:
    """Notion select label -> internal status ("Recruiter Screen" -> "recruiter_screen")."""
    return label.strip().lower().replace(" ", "_")


def accepts_pulled(current: str | None, new: str) -> bool:
    """Whether a decision recorded outside SQLite (Notion) may replace the current status.

    Forward moves and closing outcomes are accepted; nothing undoes a later local state, and a
    closed application stays closed, except that a reply after "ghosted" reopens it.
    """
    if new == current or new not in APPLICATION_STATUSES:
        return False
    if current in {"rejected", "skipped", "expired"}:
        return False
    base = "applied" if current == "ghosted" else current
    if new in TERMINAL:
        return True
    rank = -1 if base is None else PROGRESS.index(base)
    return PROGRESS.index(new) > rank
