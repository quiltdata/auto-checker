"""Unit tests for compose: envelope from context, body from template."""

import datetime

import pytest

from check_commit import __version__
from check_commit.compose import ComposeError, compose, next_counter, render_body, target_folder
from check_commit.model import DEFECT, KNOWN_UNRESOLVED, Finding, Report

from conftest import rev

NOW = datetime.datetime(2026, 8, 10, 22, 41, 3, tzinfo=datetime.timezone.utc)


def make_report(findings):
    return Report(
        package="occurrence/testpkg",
        registry="s3://b",
        tophash="e" * 64,
        pointer="2",
        prev_tophash="d" * 64,
        engine_version=__version__,
        findings=findings,
        checks_run=["delta-set"],
    )


FINDING = Finding(
    check="metadata-hygiene",
    severity=DEFECT,
    kind="stale-inherited-field",
    paths=("02-measure-selection/02.01K-start.md",),
    detail="metadata field 'changes' is byte-identical to the prior revision's",
)
KU = Finding(
    check="filename-form",
    severity=KNOWN_UNRESOLVED,
    kind="adjudicated-collision",
    paths=("02-measure-selection/02.21K-y.md", "02-measure-selection/02.21P-x.md"),
    detail="counter 21 shared",
)

PREV = rev("d" * 64, {"README.md": (100, "h0"), "02-measure-selection/02.01K-start.md": (50, "h1")})
CUR = rev(
    "e" * 64,
    {
        "README.md": (100, "h0"),
        "02-measure-selection/02.01K-start.md": (52, "h1x"),
        "02-measure-selection/02.02M-reply.md": (30, "h2"),
    },
)


def test_body_is_deterministic(policy):
    report = make_report([FINDING, KU])
    assert render_body(report, policy) == render_body(report, policy)


def test_body_contains_findings_and_counts(policy):
    body = render_body(make_report([FINDING, KU]), policy)
    assert "1 defect(s), 1 known-unresolved" in body
    assert "stale-inherited-field" in body
    assert "issues/closed/030" in body  # adjudication cite from policy
    assert "CP certifies nothing" in body


def test_envelope_and_filename(policy):
    msg = compose(make_report([FINDING]), CUR, PREV, policy, now=NOW)
    assert msg.logical_key == "02-measure-selection/02.03CP-t0-check-of-eeeeeeee.md"
    fields = dict(msg.envelope)
    assert fields["From"] == "CP"
    assert fields["Timestamp"] == "2026-08-10T22:41:03Z"
    assert fields["In-Reply-To"] == "02.02M-reply.md"
    assert msg.text.startswith("- From: CP\n- To: all\n")


def test_counter_spans_bare_and_dotted(policy):
    view = rev("f" * 64, {
        "01-backstory/36C-old-bare.md": (10, "a"),
        "01-backstory/01.37M-first-dotted.md": (10, "b"),
    })
    assert next_counter(view, "01-backstory") == 38


def test_target_folder_votes_by_diff(policy):
    folder = target_folder(make_report([FINDING]), CUR, PREV)
    assert folder == "02-measure-selection"


def test_clean_pass_refuses_to_compose(policy):
    with pytest.raises(ComposeError):
        compose(make_report([]), CUR, PREV, policy, now=NOW)


def test_engine_error_refuses_to_compose(policy):
    report = make_report([FINDING])
    report.error = "boom"
    with pytest.raises(ComposeError):
        compose(report, CUR, PREV, policy, now=NOW)
