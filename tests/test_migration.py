"""Verification tests (OQ-style). Each test names the requirement it verifies with @pytest.mark.req."""

import json
import sqlite3
from datetime import date

import pytest

from gxpmig import archive, audit
from gxpmig.migrate import run
from gxpmig.transform import (
    E_COUNTRY,
    E_DATE,
    E_DATE_FUTURE,
    E_DOC_TYPE,
    E_STATE,
    E_STUDY_FORMAT,
    E_STUDY_UNKNOWN,
    E_TITLE,
    E_VERSION,
    E_VERSION_STATE,
    load_mapping,
    parse_date,
    transform,
)  # fmt: skip
from gxpmig.vault import Vault

TODAY = date(2026, 11, 1)
MAPS = {m: load_mapping(m) for m in ("doc_types", "states", "countries")}
GOOD = {"legacy_id": "LEG-1", "title": "Protocol Synopsis - X", "doc_type": "PROT-SYN", "study": "nct 01234567",
        "version": "v2.0", "status": "Final", "effective_date": "04/03/2019", "country": ""}  # fmt: skip


def t(**kw):
    return transform({**GOOD, **kw}, MAPS, {"NCT01234567"}, TODAY)


@pytest.mark.req("URS-02")
def test_mapping_uses_only_the_approved_tables():
    r = t()
    assert not r.issues
    assert r.meta["document_type"] == "Protocol Synopsis" and r.meta["lifecycle_state"] == "Approved"
    assert E_DOC_TYPE in t(doc_type="Protocol Synopsys").issues  # a typo is not "close enough"
    assert E_STATE in t(status="Released").issues
    assert E_COUNTRY in t(country="Atlantis").issues


@pytest.mark.req("URS-03")
def test_dates_only_in_documented_formats():
    assert parse_date("04/03/2019") == date(2019, 3, 4)  # dd/mm/yyyy by documented convention
    assert parse_date("4 Mar 2019") == parse_date("20190304") == parse_date("2019-03-04")
    for bad in ["31/02/2019", "TBD", "", "03-04-2019", "2019/03/04"]:
        assert E_DATE in t(effective_date=bad).issues
    assert E_DATE_FUTURE in t(effective_date="2027-01-01").issues


@pytest.mark.req("URS-04")
def test_study_must_exist():
    assert t(study="NCT 01234567").meta["study_number"] == "NCT01234567"
    assert E_STUDY_FORMAT in t(study="ABC-123").issues
    assert E_STUDY_UNKNOWN in t(study="NCT99999999").issues


@pytest.mark.req("URS-10")
def test_approved_documents_need_a_major_version():
    assert E_VERSION_STATE in t(version="2.1").issues
    assert not t(version="2.1", status="draft").issues
    assert E_VERSION in t(version="final-ish").issues


@pytest.mark.req("URS-13")
def test_required_metadata():
    assert E_TITLE in t(title=" ").issues
    assert E_COUNTRY in t(doc_type="ICF").issues  # consent form without a country
    assert t(doc_type="ICF", country="Czech Republic").meta["country"] == "CZ"


@pytest.fixture
def migrated(legacy, tmp_path):
    rep = run(tmp_path / "vault", "B001", legacy=legacy)
    return rep, tmp_path / "vault"


@pytest.mark.req("URS-01")
def test_every_document_has_exactly_one_outcome(migrated, legacy):
    rep, _ = migrated
    truth = json.loads((legacy / "ground_truth.json").read_text())
    assert rep.reconciled and rep.source_count == len(truth)
    assert set(rep.loaded) == {k for k, v in truth.items() if v["defect"] is None}


@pytest.mark.req("URS-05")
def test_content_verified_and_corrupt_files_rejected(migrated, legacy):
    rep, vdir = migrated
    assert rep.verified_content == len(rep.loaded)
    truth = json.loads((legacy / "ground_truth.json").read_text())
    for lid, v in truth.items():
        if v["defect"] == "checksum_mismatch":
            assert "E_CHECKSUM_MISMATCH" in rep.quarantined[lid]
    v = Vault(vdir)
    src = next((legacy / "files").glob("*.txt"))
    with pytest.raises(OSError):
        v.load({"legacy_id": "X"}, src, "0" * 64, "B999", "test")  # wrong hash → refused, nothing inserted
    assert v.get("X") is None
    v.close()


@pytest.mark.req("URS-06")
def test_duplicates_not_loaded(migrated):
    rep, vdir = migrated
    con = sqlite3.connect(vdir / "vault.db")
    n, n_unique = con.execute("SELECT COUNT(*), COUNT(DISTINCT sha256) FROM documents").fetchone()
    assert n == n_unique and "E_DUPLICATE_CONTENT" in {c for cs in rep.quarantined.values() for c in cs}


@pytest.mark.req("URS-07")
def test_audit_trail_complete_append_only_tamper_evident(migrated):
    rep, vdir = migrated
    con = sqlite3.connect(vdir / "vault.db")
    actions = dict(con.execute("SELECT action, COUNT(*) FROM audit_trail GROUP BY action").fetchall())
    assert actions == {"MIGRATE_LOAD": len(rep.loaded), "MIGRATE_QUARANTINE": len(rep.quarantined)}
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM audit_trail WHERE seq = 1")
    assert audit.verify(con) == (True, None)
    con.execute("DROP TRIGGER audit_no_update")
    con.execute("UPDATE audit_trail SET actor = 'someone-else' WHERE seq = 7")
    assert audit.verify(con) == (False, 7)


@pytest.mark.req("URS-08")
def test_dry_run_changes_nothing(legacy, tmp_path):
    dry = run(tmp_path / "v", "B001", dry_run=True, legacy=legacy)
    con = sqlite3.connect(tmp_path / "v" / "vault.db")
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0] == 0
    real = run(tmp_path / "v", "B001", legacy=legacy)
    assert dry.quarantined == real.quarantined and dry.loaded == real.loaded


@pytest.mark.req("URS-09")
def test_rerun_is_idempotent(migrated, legacy):
    rep, vdir = migrated
    again = run(vdir, "B001", legacy=legacy)
    assert again.loaded == [] and sorted(again.already_loaded) == sorted(rep.loaded) and again.reconciled


@pytest.mark.req("URS-11")
@pytest.mark.req("URS-12")
def test_archive_complete_open_format_and_verifiable(legacy, tmp_path):
    m = archive.export(legacy / "legacy_ctms.db", tmp_path / "a")
    assert m["retain_until"] == "2051-11-01"
    assert all((tmp_path / "a" / t["file"]).suffix == ".csv" for t in m["tables"].values())
    assert archive.verify(tmp_path / "a")["all_ok"]
    p = tmp_path / "a" / "tables" / "sites.csv"
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[:-1]) + "\n")  # one row lost
    assert not archive.verify(tmp_path / "a")["sites"]["rows_ok"]
