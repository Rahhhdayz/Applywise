"""SQLAlchemy declarative base and engine helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for JobAgent's database tables."""


def sqlite_url(database_path: str | Path) -> str:
    """Build a SQLite URL from a local path."""
    return f"sqlite:///{Path(database_path)}"


def create_sqlite_engine(database_path: str | Path) -> Engine:
    """Create an engine after ensuring the database directory exists."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(sqlite_url(path))

