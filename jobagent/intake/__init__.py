"""Safe, deterministic intake of individual public job URLs."""

from jobagent.intake.errors import IntakeError
from jobagent.intake.service import UrlIntakeService

__all__ = ["IntakeError", "UrlIntakeService"]

