"""Every reviewer finding JobAgent fixed stays fixed: one JSON fixture per finding (no personal values).

Fixtures come from `jobagent apply fixture --job-id N` (or are written by hand for known failures).
Each names the rule that fired wrongly (``wrong_source``) and what should happen now (``expected``:
"skip", a rule key, or "" for "anything but the wrong rule"). ``answers``/``education`` add synthetic
profile entries so the rule that fired can fire again in the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobagent.apply.fields import FieldDescriptor, decide
from jobagent.apply.profile import ApplicationProfile, CannedAnswer, EducationEntry

ROOT = Path(__file__).parent
FIXTURES = sorted((ROOT / "fixtures" / "fill_findings").glob("*.json"))
EXAMPLE = ApplicationProfile.model_validate(yaml.safe_load((ROOT.parent / "candidate" / "application.example.yaml").read_text()))


@pytest.mark.parametrize("path", FIXTURES, ids=[path.stem for path in FIXTURES])
def test_fill_finding_stays_fixed(path: Path) -> None:
    item = json.loads(path.read_text())
    profile = EXAMPLE.model_copy(update={
        "answers": [*EXAMPLE.answers, *(CannedAnswer.model_validate(a) for a in item.get("answers", []))],
        "education": [EducationEntry.model_validate(e) for e in item.get("education", [])] or EXAMPLE.education,
    })
    kind = "text" if item["kind"] == "combobox" else item["kind"]  # the filler decides a combobox as text first
    descriptor = FieldDescriptor(label=item["label"], kind=kind, options=tuple(item["options"]),
                                 occurrence=item.get("occurrence", 0))
    action = decide(descriptor, profile, resume_path=None)
    if item["wrong_source"]:
        assert action.kind == "skip" or action.source != item["wrong_source"], action
    if item["expected"] == "skip":
        assert action.kind == "skip", action
    elif item["expected"]:
        assert action.kind != "skip" and action.source == item["expected"], action


def test_there_are_finding_fixtures() -> None:
    assert FIXTURES
