"""Collect test evidence for the traceability matrix: which requirement each test verifies and whether it passed."""

import json
from pathlib import Path

import pytest

EVIDENCE: list[dict] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when == "setup" and rep.outcome != "passed"):
        reqs = [m.args[0] for m in item.iter_markers("req")]
        EVIDENCE.append({"test": item.nodeid, "requirements": reqs, "outcome": rep.outcome})


def pytest_sessionfinish(session, exitstatus):
    out = Path(__file__).resolve().parent.parent / "reports" / "test_evidence.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(EVIDENCE, indent=1))


@pytest.fixture(scope="session")
def legacy():
    """Rebuild the legacy source from the committed snapshot (offline, seeded)."""
    from gxpmig.source import LEGACY, build_legacy

    build_legacy()
    return LEGACY
