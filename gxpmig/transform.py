"""Legacy row → target metadata, with every problem reported as an issue code instead of a guess.

The GxP rule behind every function here: never invent or "fix" data silently. A value is either mapped by an
approved rule (the CSV tables in mapping/, which are configuration under change control) or the document goes to
quarantine with a reason a person can act on. No fuzzy matching, no dateutil-style guessing: "04/03/2019" is
only read as 4 March because the legacy system's convention (dd/mm/yyyy) is documented.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%Y%m%d")  # documented legacy conventions, in this order
DOC_TYPES = ("Protocol Synopsis", "Informed Consent Form", "Clinical Study Report Synopsis")
STATES = ("Draft", "Approved", "Superseded")

# issue codes (also used in the URS and the exceptions report)
E_TITLE = "E_TITLE_MISSING"
E_DOC_TYPE = "E_DOC_TYPE_UNMAPPED"
E_STATE = "E_STATE_UNMAPPED"
E_DATE = "E_DATE_INVALID"
E_DATE_FUTURE = "E_DATE_FUTURE"
E_VERSION = "E_VERSION_INVALID"
E_VERSION_STATE = "E_VERSION_STATE_CONFLICT"
E_STUDY_FORMAT = "E_STUDY_FORMAT"
E_STUDY_UNKNOWN = "E_STUDY_UNKNOWN"
E_COUNTRY = "E_COUNTRY_INVALID"
E_FILE_MISSING = "E_FILE_MISSING"
E_CHECKSUM = "E_CHECKSUM_MISMATCH"
E_DUPLICATE = "E_DUPLICATE_CONTENT"


def load_mapping(name: str) -> dict[str, str]:
    with (ROOT / "mapping" / f"{name}.csv").open() as f:
        return {r["legacy_value"].strip().lower(): r["target_value"] for r in csv.DictReader(f)}


@dataclass
class Result:
    meta: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def parse_date(raw: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None  # covers "TBD", "", "31/02/2019" (strptime rejects impossible dates)


def parse_version(raw: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"[vV]?(\d+)(?:\.(\d+))?", raw.strip())
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def normalize_study(raw: str) -> str | None:
    m = re.fullmatch(r"NCT\s?(\d{8})", raw.strip().upper())
    return f"NCT{m.group(1)}" if m else None


def transform(row: dict, maps: dict[str, dict[str, str]], studies: set[str], today: date) -> Result:
    r = Result()
    title = row["title"].strip()
    if not title:
        r.issues.append(E_TITLE)
    doc_type = maps["doc_types"].get(row["doc_type"].strip().lower())
    if not doc_type:
        r.issues.append(E_DOC_TYPE)
    state = maps["states"].get(row["status"].strip().lower())
    if not state:
        r.issues.append(E_STATE)
    eff = parse_date(row["effective_date"])
    if eff is None:
        r.issues.append(E_DATE)
    elif eff > today:
        r.issues.append(E_DATE_FUTURE)  # an "effective" date in the future can't be true for a legacy record
    ver = parse_version(row["version"])
    if ver is None:
        r.issues.append(E_VERSION)
    elif state == "Approved" and ver[1] != 0:
        r.issues.append(E_VERSION_STATE)  # approved documents carry a major version (x.0) in the target DMS
    study = normalize_study(row["study"])
    if study is None:
        r.issues.append(E_STUDY_FORMAT)
    elif study not in studies:
        r.issues.append(E_STUDY_UNKNOWN)  # referential integrity: the study must exist in the target first
    country = None
    if row["country"].strip():
        country = maps["countries"].get(row["country"].strip().lower())
        if not country:
            r.issues.append(E_COUNTRY)
    elif doc_type == "Informed Consent Form":
        r.issues.append(E_COUNTRY)  # consent forms are country-specific
    r.meta = {
        "legacy_id": row["legacy_id"],
        "name": title,
        "document_type": doc_type,
        "study_number": study,
        "major_version": ver[0] if ver else None,
        "minor_version": ver[1] if ver else None,
        "lifecycle_state": state,
        "effective_date": eff.isoformat() if eff else None,
        "country": country,
    }
    return r
