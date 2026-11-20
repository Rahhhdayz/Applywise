"""Pure, browser-free matching of ATS form fields to applicant answers.

Page text (labels, option text) is untrusted data from a third-party site: here it is
only ever compared against local patterns, never executed or interpreted as instructions.
Nothing here touches a browser or an AI model. Elsewhere, page text does reach two restricted,
tool-less Claude calls, always as delimited UNTRUSTED data with schema-validated output: the
fill reviewer (``apply/review.py``; its correct values must come from the candidate's own values)
and the free-text answer drafter (``apply/answers.py``; facts only from the candidate truth).
Every action carries the ``source`` rule key that decided it, so findings map back to a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from jobagent.apply.certifications import FALLBACK_NOTE, choose_certification
from jobagent.apply.profile import ApplicationProfile
from jobagent.normalization import normalize_title

FieldKind = Literal["text", "textarea", "select", "radio", "checkbox", "file", "combobox"]

_DECLINE_MARKERS = ("decline", "prefer not", "do not wish", "don t wish")


@dataclass(frozen=True)
class FieldDescriptor:
    """A single labelled form field, as observed on the page."""

    label: str
    kind: FieldKind
    name: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    # How many earlier controls on the page carry this same label: "Field of Study" #1 belongs to
    # education entry 1, the second "Field of Study" to entry 2.
    occurrence: int = 0


@dataclass(frozen=True)
class FillAction:
    """What to do with one field: enter a value, attach a file, or skip it."""

    kind: Literal["value", "file", "skip"]
    value: str | None = None
    file_path: Path | None = None
    reason: str = ""
    # Which rule decided this ("contact.email", "answer:referred", "education.field_of_study", "custom"):
    # snapshots and fill reports carry it, and review findings become per-rule regression fixtures.
    source: str = ""


def decide(
    descriptor: FieldDescriptor,
    profile: ApplicationProfile,
    *,
    resume_path: Path | None,
    cover_letter_path: Path | None = None,
) -> FillAction:
    """Return exactly what should be entered into one field, or why it was skipped."""
    source, action = _decide(descriptor, profile, resume_path=resume_path, cover_letter_path=cover_letter_path)
    return replace(action, source=action.source or source)


def _decide(
    descriptor: FieldDescriptor,
    profile: ApplicationProfile,
    *,
    resume_path: Path | None,
    cover_letter_path: Path | None,
) -> tuple[str, FillAction]:
    label = normalize_title(descriptor.label)

    if descriptor.kind == "file":
        if "cover letter" in label or "cover_letter" in label:
            if cover_letter_path is not None and cover_letter_path.is_file():
                return "file.cover_letter", FillAction("file", file_path=cover_letter_path)
            return "file.cover_letter", _skip("No cover letter file is available")
        if "resume" in label or "cv" in label:
            if resume_path is not None and resume_path.is_file():
                return "file.resume", FillAction("file", file_path=resume_path)
            return "file.resume", _skip("No resume file is available")
        if "certificat" in label and profile.certifications and profile.certifications[0].file:
            path = Path(profile.certifications[0].file).expanduser()
            if path.is_file():
                return "file.certification", FillAction("file", file_path=path)
            return "file.certification", _skip("Certificate file is not available locally")
        return "file.unknown", _skip("Unrecognized file upload")

    if "password" in label and "forgot" not in label:
        return "password", _value_or_skip(descriptor, profile.account_password)

    terms = _terms_action(label, descriptor)
    if terms is not None:
        return terms

    screening = _screening_action(label, descriptor, profile)
    if screening is not None:
        return screening

    auth = _work_authorization_value(label, profile)
    if auth is not None:
        return auth[0], _value_or_skip(descriptor, auth[1])

    # A checkbox is yes/no: "Kansas City MO" or "GitHub CI" boxes must never take the city or GitHub URL.
    # A yes/no question ("Do you live in the city of Boston?") is never answered with a contact value either.
    contact = None if descriptor.kind == "checkbox" or is_yes_no_question(label) else _contact_value(label, profile)
    if contact is not None and contact[1] is not None:
        return contact[0], _value_or_skip(descriptor, contact[1])

    asks_history = any(word in label for word in ("previously", "formerly", "ever been employed", "ever worked"))
    if asks_history and any(word in label for word in ("employed", "worked", "employee")):
        # Answered from candidate/experience.yaml: Yes only if the question names a past employer.
        worked_there = any(normalize_title(name) in label for name in profile.past_employers)
        return "history.employer", _value_or_skip(descriptor, "Yes" if worked_there else "No")

    # One checkbox per race (Oracle): check only the box that names the configured race.
    if descriptor.kind == "checkbox" and profile.eeo.race.casefold() != "decline" and label == normalize_title(profile.eeo.race):
        return "eeo.race", FillAction("value", value="Yes")

    eeo = _eeo_value(label, profile)
    if eeo is not None:
        return eeo[0], _resolve_eeo(descriptor, eeo[1])

    education = _education_action(label, descriptor, profile)
    if education is not None:
        return education

    certification = _certification_action(label, descriptor, profile)
    if certification is not None:
        return certification

    first_skip: tuple[str, FillAction] | None = None
    long_label = len(label.split()) > _LONG_LABEL_WORDS
    topic = " ".join(label.split()[:_TOPIC_WORDS])
    other_country = names_other_country(label)
    for canned in profile.answers:
        # Word start, so "referred" never fires on "I have a preferred name" but "relative" finds "relatives".
        # On a long label every phrase must be in its opening words (the topic), so "start date" never
        # answers "If you cannot start on the start date listed, explain why".
        where = topic if long_label else label
        if other_country and _US_AUTH_WORDS.search(" ".join(canned.match)) and not any(
                names_other_country(phrase) for phrase in canned.match):
            continue  # a US authorization answer ("eligible to work: Yes") never answers "... in Canada?"
        if all(re.search(rf"(?<!\w){re.escape(normalize_title(phrase))}", where) for phrase in canned.match):
            key = "answer:" + "+".join(canned.match)
            action = _value_or_skip(descriptor, canned.value)
            if action.kind != "skip":
                return key, action
            first_skip = first_skip or (key, action)  # e.g. "in office: Yes" on a menu of relocation options: try the next
    if first_skip is not None:
        return first_skip

    return "custom", _skip("Custom question — left for you")


_LONG_LABEL_WORDS = 12
_TOPIC_WORDS = 8
_YES_NO_START = ("do", "does", "did", "are", "is", "was", "were", "have", "has", "had", "will", "would",
                 "can", "could", "should", "may", "shall")


def is_yes_no_question(label: str) -> bool:
    """ "Do you currently live in Boston?" asks yes/no; "What is your city?" asks for the city."""
    words = normalize_title(label).split()
    return len(words) >= 3 and words[0] in _YES_NO_START


def _word(phrase: str, label: str) -> bool:
    """Whole-word test, so "ethnicity" is not a "city" and "United States" is not a "state"."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", label) is not None


def _contact_value(label: str, profile: ApplicationProfile) -> tuple[str, str | None] | None:
    """(rule key, value); a None value means "leave the form's default alone"."""
    contact, links = profile.contact, profile.links
    if _word("first name", label):
        return "contact.first_name", contact.first_name
    if _word("last name", label):
        return "contact.last_name", contact.last_name
    if label == "name" or _word("full name", label) or _word("legal name", label):
        return "contact.full_name", f"{contact.first_name} {contact.last_name}".strip()
    if _word("email", label):
        return "contact.email", contact.email
    if _word("phone", label) and (_word("country", label) or _word("code", label)):
        return "contact.phone_country", contact.country  # "Country Phone Code": pick the country, never the first "+1"
    if _word("phone", label) and _word("type", label):
        return "contact.phone_device_type", contact.phone_device_type or None  # blank on file: keep the default
    if _word("extension", label):
        return "contact.phone_default", None  # never fill Phone Extension
    if _word("phone", label):
        return "contact.phone", contact.phone
    if _word("linkedin", label):
        return "contact.linkedin", links.linkedin
    if _word("github", label):
        return "contact.github", links.github
    if _word("portfolio", label) or _word("personal website", label):
        return "contact.portfolio", links.portfolio
    if _word("postal", label) or _word("zip", label):
        return "contact.postal_code", contact.postal_code or None
    if _word("city", label):
        return "contact.city", contact.city
    if label == "location":
        return "contact.location", ", ".join(part for part in (contact.city, contact.state) if part)
    if _word("state", label) or _word("province", label):
        return "contact.state", contact.state
    if _word("county", label):
        return "contact.county", contact.county or None
    if _word("country", label):
        return "contact.country", contact.country
    return None


# Countries other than the US that authorization questions name ("authorized to work in Canada?").
_OTHER_COUNTRIES = (
    "canada", "canadian", "united kingdom", "uk", "britain", "england", "ireland", "india", "australia",
    "new zealand", "mexico", "germany", "france", "netherlands", "spain", "portugal", "italy", "poland",
    "sweden", "norway", "denmark", "finland", "switzerland", "austria", "belgium", "europe", "european union",
    "eu", "eea", "singapore", "japan", "china", "hong kong", "korea", "taiwan", "philippines", "israel",
    "brazil", "argentina", "colombia", "costa rica", "romania", "czech", "hungary", "ukraine", "uae",
    "dubai", "south africa", "malaysia", "vietnam", "indonesia", "puerto rico",
)


_US_AUTH_WORDS = re.compile(r"eligib|authori|sponsor|visa|permit|right to work|citizen")


def names_other_country(label: str) -> bool:
    """The question is about a country other than the United States."""
    return any(_word(country, label) for country in _OTHER_COUNTRIES)


def _work_authorization_value(label: str, profile: ApplicationProfile) -> tuple[str, str] | None:
    """US answers only. A question naming another country never gets them: a canned answer written
    for that country (``match: ["authorized", "canada"]``) may answer it, otherwise it is left blank."""
    auth = profile.work_authorization
    if names_other_country(label):
        return None
    if "sponsorship" in label or "sponsor" in label:
        return "auth.sponsorship", auth.requires_sponsorship
    if "authorized" in label and "work" in label:
        return "auth.authorized_us", auth.authorized_us
    return None


def _eeo_value(label: str, profile: ApplicationProfile) -> tuple[str, str] | None:
    eeo = profile.eeo
    if "gender" in label or ("sex at birth" in label and "visually" not in label):
        return "eeo.gender", eeo.gender
    if "hispanic" in label or "latino" in label:
        return "eeo.hispanic", eeo.hispanic
    if "race" in label or "ethnicity" in label:
        return "eeo.race", eeo.race
    if "veteran" in label:
        return "eeo.veteran", eeo.veteran
    if "disability" in label:
        return "eeo.disability", eeo.disability
    return None


_STUDY_PHRASES = ("field of study", "area of study", "course of study")
_STUDY_WORDS = ("discipline", "major", "specialization")  # only in short labels: never "subject to discipline?"
_NOT_MAIN_STUDY = ("minor", "secondary", "dual", "double", "second", "concentration")
_SCHOOL_LABELS = {"school", "school name", "school or university", "school university", "university", "college",
                  "college university", "institution"}
_DEGREE_LABELS = {"degree", "degree type", "degree level"}


def _education_action(label: str, descriptor: FieldDescriptor, profile: ApplicationProfile) -> tuple[str, FillAction] | None:
    """School, degree and field of study come from the education entry this field belongs to.

    The nth "Field of Study" on the page is education entry n (Riverside first, then Lakeside), so
    one degree's value never lands on the other. A field of study must match an option exactly
    (after normalization): "Cybersecurity" never picks "Cybersecurity Law and Policy" and no
    entry ever falls back to the other degree's subject. No confident match: skipped, left blank.
    """
    short = len(label.split()) <= 4
    if any(_word(phrase, label) for phrase in _STUDY_PHRASES) or (short and any(_word(w, label) for w in _STUDY_WORDS)):
        if any(_word(word, label) for word in _NOT_MAIN_STUDY):
            return "education.minor", _skip("No minor or second major on file")
        part = "field_of_study"
    elif label in _SCHOOL_LABELS:
        part = "school"
    elif label in _DEGREE_LABELS:
        part = "degree"
    else:
        return None
    key = f"education.{part}"
    if descriptor.occurrence >= len(profile.education):
        return (key, _skip(f"No education entry #{descriptor.occurrence + 1} on file")) if profile.education else None
    entry = profile.education[descriptor.occurrence]
    if part == "school":
        return key, _value_or_skip(descriptor, " | ".join(v for v in (entry.school, entry.school_search) if v))
    if part == "degree":
        return key, _value_or_skip(descriptor, entry.degree)
    if not entry.field_of_study:
        return key, _skip(f"No field of study on file for {entry.school}")
    if descriptor.kind not in {"select", "radio"}:
        return key, _value_or_skip(descriptor, entry.field_of_study)
    matched = exact_option(descriptor.options, entry.field_of_study)
    if matched is None:
        return key, _skip(f"No exact option for {entry.school} field of study: {entry.field_of_study!r}")
    return key, FillAction("value", value=matched)


def exact_option(options: tuple[str, ...], value: str) -> str | None:
    """The option equal (after normalization) to the first alternative in ``value`` that has one; no partials."""
    for alternative in (part.strip() for part in value.split("|")):
        wanted = normalize_title(alternative)
        if wanted and (matched := next((o for o in options if normalize_title(o) == wanted), None)) is not None:
            return matched
    return None


def _resolve_eeo(descriptor: FieldDescriptor, configured_value: str) -> FillAction:
    if configured_value.strip().casefold() == "decline":
        if descriptor.kind in {"select", "radio"}:
            declined = _find_option(descriptor.options, _DECLINE_MARKERS)
            if declined is not None:
                return FillAction("value", value=declined)
            return _skip("No decline-to-answer option was found")
        return _skip("Cannot decline a free-text EEO question safely")
    return _value_or_skip(descriptor, configured_value)


def _value_or_skip(descriptor: FieldDescriptor, value: str) -> FillAction:
    """Never guess: a select/radio target must match an existing option.

    ``value`` may list alternatives in preference order, "Company website | Corporate Website |
    Career Websites": the first one the options contain wins (and for nested menus, a category
    alternative opens the level where a leaf alternative matches). Text fields get the first.
    """
    alternatives = [part.strip() for part in value.split("|") if part.strip()]
    if not alternatives:
        return _skip("No configured answer for this field")
    if descriptor.kind in {"select", "radio"}:
        if _DATE.fullmatch(alternatives[0]):
            return _skip("A date never goes into a choice field")
        if is_yes_no_choice(descriptor.options) and not all(normalize_title(a) in {"yes", "no"} for a in alternatives):
            return _skip(f"Yes/No question; configured answer {value!r} is not Yes or No")
        matched = match_option(descriptor.options, value)
        if matched is None:
            return _skip(f"No option matched configured answer: {value!r}")
        return FillAction("value", value=matched)
    return FillAction("value", value=alternatives[0])


_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}")
_PLACEHOLDERS = {"", "select one", "select", "please select", "choose one", "none selected"}


def is_yes_no_choice(options: tuple[str, ...]) -> bool:
    """Options are just Yes and No (plus a "Select One" placeholder)."""
    real = {normalize_title(option) for option in options} - _PLACEHOLDERS
    return bool(real) and real <= {"yes", "no"}


def match_option(options: tuple[str, ...], value: str) -> str | None:
    """The option for the first alternative in ``value`` ("A | B") that the options contain."""
    for alternative in (part.strip() for part in value.split("|")):
        if alternative and (matched := _find_option(options, (alternative,))) is not None:
            return matched
    return None


def _find_option(options: tuple[str, ...], markers: tuple[str, ...]) -> str | None:
    """Prefer an exact normalized match, else the first option containing any marker,
    so "Male" never lands on "Female" and "No" beats a longer option containing "no"."""
    for option in options:
        if normalize_title(option) in {normalize_title(marker) for marker in markers}:
            return option
    # Among partial matches take the shortest, so "United States" picks "United States of America"
    # over "United States Minor Outlying Islands".
    # Short answers ("MA", "No") match exactly or as the option's whole first word ("No, I was not
    # referred"); never as a fragment, or "MA" would land on "Maine".
    short = [normalize_title(marker) for marker in markers if len(normalize_title(marker)) < 4]
    for option in options:
        if any(re.match(rf"{re.escape(marker)}(?!\w)", normalize_title(option)) for marker in short):
            return option
    long_markers = [normalize_title(marker) for marker in markers if len(normalize_title(marker)) >= 4]
    partial = [option for option in options if any(marker in normalize_title(option) for marker in long_markers)]
    return min(partial, key=len) if partial else None


_SITUATION = re.compile(r"(fits|describes) your (current )?(situation|status|work authori[sz]ation)"
                        r"|work authori[sz]ation status|immigration status")
_CLEARANCE = re.compile(r"(?<!\w)(security )?clearance")
_GOVERNMENT = re.compile(r"federal government|government (civilian|employee)|(?<!\w)military|armed forces")
_PERCENT = re.compile(r"(\d{1,3})\s*%")


_ACK_TERMS = re.compile(
    r"terms (of use|and conditions|of service)|privacy (notice|policy|statement)|data (privacy|protection)"
    r"|consent to the (processing|collection|use)|processing of my (personal )?(data|information)")
_ACK_EXCLUDED = re.compile(  # each alternative starts at a word start: "race" never matches "trace"
    r"(?<!\w)(?:certif|true and (complete|correct|accurate)|accurate and complete|signature|e ?sign|(?<!\w)sign(ed)?(?!\w)"
    r"|arbitrat|background|drug|criminal|self ?identif|disabilit|veteran|equal employment|eeo|gender|race"
    r"|job alert|marketing|newsletter|opt ?in|text message|sms|future (job )?opportunit|talent (community|network)"
    r"|subscribe|promotional)")
_ACK_OPTION = re.compile(r"^(i )?(acknowledge|agree|accept|consent|understand)")


def is_terms_acknowledgement(label: str) -> bool:
    """Terms of use / privacy notice / data-processing consent (Master, 2026-11-19), never anything excluded:
    truthfulness certifications, signatures, EEO or self-identification, arbitration, background or drug checks,
    marketing or job-alert opt-ins."""
    text = normalize_title(label)
    return bool(_ACK_TERMS.search(text)) and not _ACK_EXCLUDED.search(text)


def _terms_action(label: str, descriptor: FieldDescriptor) -> tuple[str, FillAction] | None:
    if not is_terms_acknowledgement(descriptor.label):
        return None
    if descriptor.kind == "checkbox":
        return "consent.terms", FillAction("value", value="Yes")
    agree = [option for option in descriptor.options if _ACK_OPTION.match(normalize_title(option))]
    if agree:  # a consent dropdown or radio ("I Acknowledge")
        return "consent.terms", FillAction("value", value=agree[0])
    return None


def _screening_action(label: str, descriptor: FieldDescriptor,
                      profile: ApplicationProfile) -> tuple[str, FillAction] | None:
    """Master's Northwind answers, by meaning: work-permit status, clearance (always none), travel %, government."""
    screening, options = profile.screening, descriptor.options
    if _SITUATION.search(label) and profile.work_authorization.status and not names_other_country(label):
        return "auth.status", _value_or_skip(descriptor, profile.work_authorization.status)
    if _CLEARANCE.search(label):
        if is_yes_no_question(label) or (options and {normalize_title(o) for o in options} <= {"yes", "no"}):
            return "screening.clearance", _value_or_skip(descriptor, "No")  # "Do you hold a clearance?" -> No
        return "screening.clearance", _value_or_skip(descriptor, screening.clearance)  # never claims one
    if "travel" in label and (options or "%" in label or "percent" in label):
        if options and not {normalize_title(o) for o in options} <= {"yes", "no"}:
            percents = [(max(map(int, _PERCENT.findall(o))), o) for o in options if _PERCENT.search(o)]
            within = [item for item in percents if item[0] <= screening.travel_max_percent]
            if within:
                return "screening.travel", FillAction("value", value=max(within)[1])
            return "screening.travel", _skip("No travel option at or below the limit")
        return "screening.travel", _value_or_skip(descriptor, "Yes" if options else f"Up to {screening.travel_max_percent}%")
    if _GOVERNMENT.search(label) and "veteran" not in label and "protected" not in label:
        return "screening.former_government", _value_or_skip(descriptor, screening.former_government)
    return None


def _certification_action(label: str, descriptor: FieldDescriptor,
                          profile: ApplicationProfile) -> tuple[str, FillAction] | None:
    """A certification field on a generic form (Workday's Certifications section has its own filler).

    Choice fields follow Master's order (exact, Other, Security+ flagged). Text fields get the name, number or a
    date by what the label asks; a score is left blank. A yes/no question ("Do you hold ...?") is not handled here.
    """
    if not profile.certifications or "certific" not in label or is_yes_no_question(label):
        return None
    cert = profile.certifications[0]
    if descriptor.options:
        choice = choose_certification(list(descriptor.options), cert.name)
        if choice is None:
            return "certification", _skip("No certification option for SecAI+, Other or Security+")
        if choice.branch == "security_plus":
            return "certification.fallback", FillAction("value", value=choice.option, reason=FALLBACK_NOTE)
        if choice.branch == "other":  # the name goes into the "Other" text field (a certification-named label)
            return "certification.other", FillAction("value", value=choice.option, reason=f"Other: type {cert.name}")
        return "certification.exact", FillAction("value", value=choice.option)
    for words, key, value in (
        (("score",), "score", cert.score),
        (("number", "id", "license"), "number", cert.number),
        (("expir",), "expiration_date", cert.expiration_date),
        (("exam",), "exam_date", cert.exam_date),
        (("issue", "earned", "obtained", "date"), "issued_date", cert.issued_date),
    ):
        if any(re.search(rf"(?<!\w){word}", label) for word in words):  # word start: "expir" finds "expiration"
            return f"certification.{key}", (_value_or_skip(descriptor, value) if value else _skip("Left blank"))
    return "certification.name", _value_or_skip(descriptor, cert.name)


def _skip(reason: str) -> FillAction:
    return FillAction("skip", reason=reason)
