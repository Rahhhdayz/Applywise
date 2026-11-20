"""Validated candidate truth configuration and deterministic fingerprints."""

from jobagent.candidate.loader import CandidateConfigurationError, load_candidate_truth
from jobagent.candidate.models import CandidateTruth

__all__ = ["CandidateConfigurationError", "CandidateTruth", "load_candidate_truth"]
