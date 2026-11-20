"""Which certification option to pick (Master's order, 2026-11-19), as pure, testable data.

1. The certification itself when it is listed ("CompTIA SecAI+", "SecAI+ - CompTIA").
2. Otherwise "Other", with the name typed into the accompanying text field.
3. Otherwise "Security+" ("Security+ - CompTIA - CompTia"), always flagged to Master as a fallback.

Nothing else is ever picked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

FALLBACK_NOTE = "certification fallback: Security+ used (SecAI+ not listed)"


@dataclass(frozen=True)
class CertificationChoice:
    option: str
    branch: Literal["exact", "other", "security_plus"]


def _forms(label: str) -> set[str]:
    """"SecAI+ - CompTIA" -> {"secai+"}: case, spaces, punctuation (but "+") and the vendor name dropped."""
    forms = set()
    for part in [label, *label.split(" - ")]:
        form = re.sub(r"[^a-z0-9+]", "", part.casefold().replace("comptia", ""))
        if form:
            forms.add(form)
    return forms


def _wanted(name: str) -> set[str]:
    base = re.sub(r"[^a-z0-9+]", "", name.casefold().replace("comptia", ""))
    return {base, base.rstrip("+")} - {""}


def choose_certification(options: list[str], name: str) -> CertificationChoice | None:
    """The option to pick for ``name`` among ``options``, by Master's order; None when none of the three exist."""
    wanted = _wanted(name)
    for option in options:
        if _forms(option) & wanted:
            return CertificationChoice(option, "exact")
    for option in options:
        if re.match(r"\s*other\b", option, re.IGNORECASE):
            return CertificationChoice(option, "other")
    for option in options:
        if "security+" in _forms(option):
            return CertificationChoice(option, "security_plus")
    return None
