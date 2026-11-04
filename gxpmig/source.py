"""Build the *legacy* side: a document store with messy metadata, and a legacy CTMS database to retire.

Real migrations start from years of inconsistent data entry, so the generator reproduces that on purpose:
- the same document type typed five different ways, dates in several formats, versions like "v1.0" or "2",
- plus known **defects** (~12 % of documents) with a label, so we can measure whether validation catches them.

Trial facts come from a frozen snapshot of public ClinicalTrials.gov records (source_snapshot/trials.json).

Run:  uv run python -m gxpmig.source --snapshot   # once: download the snapshot (needs internet)
      uv run python -m gxpmig.source              # rebuild data/legacy/ from the snapshot (offline, seeded)
"""

import argparse
import csv
import hashlib
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "source_snapshot" / "trials.json"
LEGACY = ROOT / "data" / "legacy"
SEED = 0
MIGRATION_DATE = date(2026, 11, 1)  # "today" of the migration, fixed so future-date checks are reproducible

DOC_TYPES = {  # target type → how people typed it in the legacy system
    "Protocol Synopsis": ["Protocol Synopsis", "protocol synopsis", "PROT-SYN", "Prot. Synopsis"],
    "Informed Consent Form": ["Informed Consent Form", "ICF", "Informed consent form", "Consent Form"],
    "Clinical Study Report Synopsis": ["CSR Synopsis", "CSR-SYN", "Clinical study report - synopsis"],
}
STATES = {"Approved": ["Final", "Approved", "APPROVED", "Effective"], "Draft": ["draft", "In Review"],
          "Superseded": ["Superseded", "Obsolete"]}  # fmt: skip
COUNTRIES = {"US": ["United States", "USA", "US"], "DE": ["Germany", "DE"], "GB": ["United Kingdom", "UK"],
             "FR": ["France", "FR"], "JP": ["Japan", "JP"], "PL": ["Poland", "PL"], "ES": ["Spain", "ES"],
             "CA": ["Canada", "CA"], "SK": ["Slovakia", "SK"], "CZ": ["Czechia", "Czech Republic"]}  # fmt: skip
DEFECTS = ["unknown_doc_type", "invalid_date", "future_date", "unknown_study", "missing_file",
           "checksum_mismatch", "duplicate", "invalid_version", "missing_title"]  # fmt: skip
N_PER_DEFECT = 5  # balanced, so every defect type gets a measurable detection rate


def download_snapshot(n_per_condition: int = 20) -> None:
    import httpx

    fields = ("NCTId,BriefTitle,LeadSponsorName,Phase,OverallStatus,StartDate,CompletionDate,EnrollmentCount,"
              "BriefSummary,MinimumAge,PrimaryOutcomeMeasure,LocationCountry")  # fmt: skip
    trials = {}
    for cond in ["asthma", "psoriasis", "type 2 diabetes", "breast cancer", "hypertension", "rheumatoid arthritis"]:
        params = {
            "query.cond": cond,
            "filter.overallStatus": "COMPLETED",
            "pageSize": n_per_condition,
            "filter.advanced": "AREA[Phase]PHASE3",
            "fields": fields,
            "sort": "LastUpdatePostDate:desc",
        }
        for s in httpx.get("https://clinicaltrials.gov/api/v2/studies", params=params, timeout=30).json()["studies"]:
            p = s["protocolSection"]
            outcomes = p.get("outcomesModule", {}).get("primaryOutcomes", [])
            locations = p.get("contactsLocationsModule", {}).get("locations", [])
            trials[p["identificationModule"]["nctId"]] = {
                "nct_id": p["identificationModule"]["nctId"],
                "title": p["identificationModule"]["briefTitle"],
                "sponsor": p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
                "phase": ", ".join(p.get("designModule", {}).get("phases", [])),
                "status": p.get("statusModule", {}).get("overallStatus", ""),
                "start": p.get("statusModule", {}).get("startDateStruct", {}).get("date", ""),
                "completion": p.get("statusModule", {}).get("completionDateStruct", {}).get("date", ""),
                "enrollment": p.get("designModule", {}).get("enrollmentInfo", {}).get("count"),
                "summary": p.get("descriptionModule", {}).get("briefSummary", ""),
                "min_age": p.get("eligibilityModule", {}).get("minimumAge", ""),
                "primary_outcome": next(iter(outcomes), {}).get("measure", ""),
                "countries": sorted({loc.get("country", "") for loc in locations} - {""}),
            }
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(sorted(trials.values(), key=lambda t: t["nct_id"]), indent=1, ensure_ascii=False))
    print(f"{len(trials)} trials → {SNAPSHOT}")


def _content(doc_type: str, t: dict, version: str) -> str:
    body = {
        "Protocol Synopsis": f"Sponsor: {t['sponsor']}\nPhase: {t['phase']}\nPlanned enrollment: {t['enrollment']}\n\n"
        f"Background and objectives\n{t['summary']}\n\nPrimary endpoint\n{t['primary_outcome']}",
        "Informed Consent Form": f'You are invited to take part in the study "{t["title"]}".\n'
        f"Participants must be at least {t['min_age']} old. Participation is voluntary and you may withdraw "
        f"at any time without giving a reason.\n\nWhat the study is about\n{t['summary']}",
        "Clinical Study Report Synopsis": f"Study period: {t['start']} to {t['completion']}\n"
        f"Number of participants: {t['enrollment']}\n\nPrimary endpoint\n{t['primary_outcome']}\n\n"
        f"Conclusions\nThe study was completed as planned ({t['status'].lower()}).",
    }[doc_type]
    return f"{doc_type.upper()}\n{t['nct_id']} · {t['title']}\nVersion {version}\n\n{body}\n"


def _fmt_date(d: date, rng: random.Random) -> str:
    # The legacy system had no date picker: ISO, the EU convention dd/mm/yyyy, "4 Mar 2019" and yyyymmdd all occur.
    return rng.choice([d.isoformat(), d.strftime("%d/%m/%Y"), f"{d.day} {d.strftime('%b %Y')}", d.strftime("%Y%m%d")])


def _fmt_version(major: int, minor: int, rng: random.Random) -> str:
    if minor == 0:
        return rng.choice([f"{major}", f"v{major}.0", f"{major}.0", f"V{major}"])
    return rng.choice([f"{major}.{minor}", f"v{major}.{minor}"])


def build_legacy() -> dict:
    rng = random.Random(SEED)
    trials = json.loads(SNAPSHOT.read_text())
    files = LEGACY / "files"
    files.mkdir(parents=True, exist_ok=True)
    for f in files.glob("*"):
        f.unlink()
    rows, truth = [], {}
    plan = [(t, dt) for t in trials for dt in rng.sample(list(DOC_TYPES), rng.randint(2, 3))]
    # defects go to random documents, never to the first 10 (a duplicate needs an earlier original)
    hit = rng.sample(range(10, len(plan)), N_PER_DEFECT * len(DEFECTS))
    defect_of = {i: DEFECTS[j // N_PER_DEFECT] for j, i in enumerate(hit)}
    for n, (t, doc_type) in enumerate(plan, start=1):
        lid = f"LEG-{n:05d}"
        state = rng.choices(list(STATES), weights=[0.75, 0.1, 0.15])[0]
        major, minor = rng.randint(1, 3), (0 if state != "Draft" else rng.randint(1, 4))
        eff = date(2012, 1, 1) + timedelta(days=rng.randint(0, 4800))
        country = rng.choice(list(COUNTRIES)) if doc_type == "Informed Consent Form" else ""
        text = _content(doc_type, t, f"{major}.{minor}")
        row = {
            "legacy_id": lid,
            "file_name": f"{lid}.txt",
            "title": f"{doc_type} - {t['title']}"[:200],
            "doc_type": rng.choice(DOC_TYPES[doc_type]),
            "study": rng.choice([t["nct_id"], t["nct_id"].lower(), f"NCT {t['nct_id'][3:]}"]),
            "version": _fmt_version(major, minor, rng),
            "status": rng.choice(STATES[state]),
            "effective_date": _fmt_date(eff, rng),
            "country": rng.choice(COUNTRIES[country]) if country else "",
            "author": rng.choice(["j.novak", "m.smith", "a.kovac", "l.garcia", "s.tanaka"]),
            "md5": hashlib.md5(text.encode()).hexdigest(),  # what the legacy system stored at check-in
        }
        expected = {"legacy_id": lid, "name": row["title"], "document_type": doc_type, "study_number": t["nct_id"],
                    "major_version": major, "minor_version": minor, "lifecycle_state": state,
                    "effective_date": eff.isoformat(), "country": country or None}  # fmt: skip
        defect = defect_of.get(n - 1)
        write = True
        match defect:
            case "unknown_doc_type":
                row["doc_type"] = rng.choice(["Misc scan", "Other", "FAX"])
            case "invalid_date":
                row["effective_date"] = rng.choice(["31/02/2019", "TBD", "2019-13-01", ""])
            case "future_date":
                row["effective_date"] = (MIGRATION_DATE + timedelta(days=rng.randint(30, 900))).isoformat()
            case "unknown_study":
                row["study"] = f"NCT{rng.randint(90000000, 99999999)}"
            case "missing_file":
                write = False
            case "checksum_mismatch":
                pass  # file is corrupted after the md5 was recorded, below
            case "duplicate":
                if rows:  # an exact copy of an earlier document checked in a second time
                    src = rng.choice([r for r in rows if truth[r["legacy_id"]]["defect"] is None])
                    row = {**src, "legacy_id": lid, "file_name": f"{lid}.txt"}
                    text = (files / src["file_name"]).read_text() if (files / src["file_name"]).exists() else text
                    row["md5"] = hashlib.md5(text.encode()).hexdigest()
                    expected = None
                else:
                    defect = None
            case "invalid_version":
                row["version"] = rng.choice(["final-ish", "1..2", "x"])
            case "missing_title":
                row["title"] = ""
        if write:
            content = text
            if defect == "checksum_mismatch":  # bit rot / a truncated copy on the old file share
                content = text[: len(text) // 2]
            (files / row["file_name"]).write_text(content)
        rows.append(row)
        truth[lid] = {"defect": defect, "expected": expected}

    with (LEGACY / "metadata.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (LEGACY / "ground_truth.json").write_text(json.dumps(truth, indent=1))
    (LEGACY / "study_registry.json").write_text(json.dumps(sorted(t["nct_id"] for t in trials)))
    build_legacy_ctms(trials, rows, rng)
    n_def = sum(1 for v in truth.values() if v["defect"])
    print(f"{len(rows)} legacy documents ({n_def} with an injected defect) for {len(trials)} trials → {LEGACY}")
    return truth


def build_legacy_ctms(trials: list[dict], docs: list[dict], rng: random.Random) -> None:
    """A small legacy clinical-trial-management app to retire: three tables and the document index."""
    db = LEGACY / "legacy_ctms.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE studies (nct_id TEXT PRIMARY KEY, title TEXT, sponsor TEXT, phase TEXT, status TEXT,
                              start_date TEXT, completion_date TEXT, enrollment INTEGER);
        CREATE TABLE sites (site_id INTEGER PRIMARY KEY, nct_id TEXT REFERENCES studies, country TEXT,
                            activated TEXT, n_randomized INTEGER);
        CREATE TABLE document_index (legacy_id TEXT PRIMARY KEY, nct_id TEXT, doc_type TEXT, file_name TEXT);
    """)
    con.executemany(
        "INSERT INTO studies VALUES (?,?,?,?,?,?,?,?)",
        [(t["nct_id"], t["title"], t["sponsor"], t["phase"], t["status"], t["start"], t["completion"], t["enrollment"])
         for t in trials],
    )  # fmt: skip
    site_rows = []
    for t in trials:
        for c in t["countries"][:6] or ["United States"]:
            site_rows.append((None, t["nct_id"], c, t["start"], rng.randint(0, 60)))
    con.executemany("INSERT INTO sites VALUES (?,?,?,?,?)", site_rows)
    con.executemany(
        "INSERT INTO document_index VALUES (?,?,?,?)",
        [(d["legacy_id"], d["study"], d["doc_type"], d["file_name"]) for d in docs],
    )
    con.commit()
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true")
    args = ap.parse_args()
    if args.snapshot:
        download_snapshot()
    build_legacy()


if __name__ == "__main__":
    main()
