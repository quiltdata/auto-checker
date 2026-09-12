"""The findings topic carries prose, and the prose carries the actual characters.

SNS email delivery is plain text, so what a subscriber received was the report's
JSON, with `detail` — the one field carrying a finding's substance — nested
deepest and every em dash escaped to `\\u2014`. These tests hold the two
properties that fixed: the report round-trips non-ASCII, and the notification
reads as text.
"""

from __future__ import annotations

import json

from check_commit import notify
from check_commit.model import DEFECT, KNOWN_UNRESOLVED, Finding, Report
from check_commit.policy import POLICY_DIR, Policy

import pytest


@pytest.fixture
def policy():
    return Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence")


# A detail string of the kind the packages actually produce: em dash, section
# sign, accented character, backticks.
DETAIL = (
    "issue Status is 'open — q9 program closed through `021.29`' and 04a-précis.md "
    "is cited; §5 requires exactly open|closed"
)


def report(findings=(), notes=(), error=None) -> Report:
    return Report(
        package="occurrence/outcome",
        registry="s3://protology",
        tophash="a7298617b2de67e6359f348c0ee5eb4554249d2d3c49b51c1a784c298ceebcaa",
        pointer="1789230106",
        prev_tophash="1a5fb3c986f63dab37f223f22d6e4e3bfcd7ffd27fe381de97446671e7fe50f5",
        engine_version="0.3.4",
        regime="current",
        checks_run=["workflow-stamp"] * 12,
        findings=list(findings),
        notes=list(notes),
        error=error,
    )


DEFECT_F = Finding("issue-readme", DEFECT, "bad-status", ("issues/021-x/README.md",), DETAIL)
KU_F = Finding(
    "turn-form",
    KNOWN_UNRESOLVED,
    "malformed-turn-name",
    ("issues/009-x/009.09b-GPT-note.md",),
    "turn filename is not <issue>.<turn>-<contributor>-<slug>.md; §5 states that "
    "form as a SHOULD, so this is recorded, not faulted",
)


# --- the JSON keeps its characters ------------------------------------------

def test_report_json_does_not_escape_non_ascii():
    raw = report([DEFECT_F]).to_json()
    assert "—" in raw and "é" in raw and "§" in raw
    assert "\\u2014" not in raw and "\\u00e9" not in raw
    # still valid JSON, and still the same string after a round trip
    assert json.loads(raw)["findings"][0]["detail"] == DETAIL


# --- the notification reads as text -----------------------------------------

def test_subject_says_what_happened(policy):
    assert notify.subject(report([DEFECT_F])).startswith("[check-commit] 1 defect — ")
    assert "2 defects" in notify.subject(report([DEFECT_F, DEFECT_F]))
    assert "1 known-unresolved" in notify.subject(report([KU_F]))
    assert "pass" in notify.subject(report())
    assert "engine error" in notify.subject(report(error="boom"))
    # SNS caps the subject at 100 characters
    assert len(notify.subject(report([DEFECT_F]))) <= 100


def test_render_is_prose_not_json(policy):
    body = notify.render(report([DEFECT_F, KU_F], notes=["key-drift: 04a-précis.md moved"]), policy)
    assert not body.lstrip().startswith("{")
    assert '"schema"' not in body
    # the substance a reader wants, unescaped and not nested
    assert DETAIL in body
    assert "\\u2014" not in body
    # sections, counts, and the distinction between them
    assert "## Defects (1)" in body
    assert "## Known-unresolved (1)" in body
    assert "## Notes" in body
    assert "not defects" in body
    assert "issues/021-x/README.md" in body


def test_render_omits_empty_sections(policy):
    body = notify.render(report([DEFECT_F]), policy)
    assert "## Defects (1)" in body
    assert "Known-unresolved" not in body
    assert "## Notes" not in body


def test_render_links_the_catalog(policy):
    body = notify.render(report([DEFECT_F]), policy)
    assert (
        "https://open.quiltdata.com/b/protology/packages/occurrence/outcome/tree/"
        "a7298617b2de67e6359f348c0ee5eb4554249d2d3c49b51c1a784c298ceebcaa" in body
    )


def test_render_carries_an_engine_error(policy):
    body = notify.render(report(error="check issue-routes crashed: KeyError"), policy)
    assert "## Engine error" in body
    assert "KeyError" in body


def test_render_falls_back_to_json_without_a_template(policy, tmp_path):
    """A missing template must not cost the alert."""
    body = notify.render(report([DEFECT_F]), policy, template_path=tmp_path / "absent.md")
    assert "no notify template" in body
    assert '"schema"' in body  # the JSON, so nothing is lost


def test_render_is_deterministic(policy):
    r = report([DEFECT_F, KU_F])
    assert notify.render(r, policy) == notify.render(r, policy)
