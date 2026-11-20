"""User-safe error types for URL intake."""


class IntakeError(Exception):
    """An expected fetch, safety, or extraction failure suitable for CLI display."""

