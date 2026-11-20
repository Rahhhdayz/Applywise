"""Terms / privacy acknowledgements are ticked (Master, 2026-11-19); certifications, signatures, EEO, arbitration,
background checks and marketing opt-ins never are."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobagent.apply.fields import FieldDescriptor, decide, is_terms_acknowledgement
from jobagent.apply.profile import ApplicationProfile

PROFILE = ApplicationProfile.model_validate(
    yaml.safe_load((Path(__file__).parent.parent / "candidate" / "application.example.yaml").read_text()))


@pytest.mark.parametrize("label", [
    "I understand and acknowledge the terms of use for Northwind.",
    "I acknowledge the Job Applicant Privacy Notice",
    "I consent to the processing of my personal data as described in the privacy policy",
    "Terms and Conditions",
])
def test_terms_and_privacy_acknowledgements_are_ticked(label: str) -> None:
    assert is_terms_acknowledgement(label)
    action = decide(FieldDescriptor(label, "checkbox"), PROFILE, resume_path=None)
    assert (action.kind, action.value, action.source) == ("value", "Yes", "consent.terms")


def test_a_consent_dropdown_picks_the_acknowledge_option() -> None:  # Contoso's T2 consent
    action = decide(FieldDescriptor("Data privacy consent: please review the T2 privacy notice", "select",
                                    options=("Select One", "I Acknowledge")), PROFILE, resume_path=None)
    assert (action.value, action.source) == ("I Acknowledge", "consent.terms")


@pytest.mark.parametrize("label", [
    "I certify that the information provided is true and complete",
    "By typing my name below I agree this is my electronic signature and I accept the terms and conditions",
    "I agree to binding arbitration under the terms and conditions of employment",
    "I authorize a background check as described in the privacy notice",
    "I consent to drug testing per the privacy policy",
    "Send me job alerts and marketing updates (privacy policy applies)",
    "Voluntary self-identification of disability: I acknowledge the privacy notice",
])
def test_attestations_signatures_and_opt_ins_are_never_ticked(label: str) -> None:
    assert not is_terms_acknowledgement(label)
    assert decide(FieldDescriptor(label, "checkbox"), PROFILE, resume_path=None).source != "consent.terms"
