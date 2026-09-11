"""Unit tests for compose: the turn's path and header from context, body from
the template."""

import datetime

import pytest

from check_commit import __version__
from check_commit import policy as forms
from check_commit.compose import ComposeError, compose, next_turn, render_body, target_folder
from check_commit.model import DEFECT, KNOWN_UNRESOLVED, Finding, Report

from conftest import META, rev

NOW = datetime.datetime(2026, 9, 10, 22, 41, 3, tzinfo=datetime.timezone.utc)

FOLDER = "issues/007-measure-selection-scope"


def make_report(findings):
    return Report(
        package="occurrence/testpkg",
        registry="s3://test-registry",
        tophash="e" * 64,
        pointer="2",
        prev_tophash="d" * 64,
        engine_version=__version__,
        regime="current",
        findings=findings,
        checks_run=["entry-count", "issue-routes"],
    )


FINDING = Finding(
    check="issue-routes",
    severity=DEFECT,
    kind="route-survives-closure",
    paths=(FOLDER, f"{FOLDER}/README.md"),
    detail="README is Status: closed but the route is still set",
)
KU = Finding(
    check="issue-readme",
    severity=KNOWN_UNRESOLVED,
    kind="unreadable-readme",
    paths=(f"{FOLDER}/README.md",),
    detail="could not fetch the README",
)

PREV = rev(
    "d" * 64,
    {"README.md": (100, "h0"), f"{FOLDER}/007.01-Owner-opening.md": (50, "h1")},
    meta=META,
)
CUR = rev(
    "e" * 64,
    {
        "README.md": (100, "h0"),
        f"{FOLDER}/007.01-Owner-opening.md": (50, "h1"),
        f"{FOLDER}/007.02-PM-review.md": (30, "h2"),
    },
    meta=META,
)


def test_body_is_deterministic(policy):
    report = make_report([FINDING, KU])
    assert render_body(report, policy) == render_body(report, policy)


def test_body_contains_findings_and_counts(policy):
    body = render_body(make_report([FINDING, KU]), policy)
    assert "1 defect(s), 1 known-unresolved" in body
    assert "route-survives-closure" in body
    assert "`current` contract" in body
    assert "`Checker` certifies\nnothing and closes nothing" in body


def test_body_renders_for_a_first_revision(policy):
    """A report with no parent must still render; the template used to index
    prev_tophash unconditionally."""
    report = make_report([FINDING])
    report.prev_tophash = None
    assert "first revision" in render_body(report, policy)


def test_turn_path_and_header(policy):
    turn = compose(make_report([FINDING]), CUR, PREV, policy, now=NOW)
    assert turn.logical_key == f"{FOLDER}/007.03-Checker-t0-check-of-eeeeeeee.md"
    fields = dict(turn.provenance)
    assert fields["Opened"] == "2026-09-10T22:41:03Z"
    assert fields["Originator"] == "commit-protocol"
    assert fields["Timestamp-Source"] == "provided"
    # §5: H1 on the first line, provenance list immediately after
    lines = turn.text.splitlines()
    assert lines[0].startswith("# T0 check of revision eeeeeeee")
    assert lines[1] == ""
    assert lines[2].startswith("- **Opened:**")


def test_composed_turn_conforms_to_the_turn_grammar(policy):
    """The turn we file must pass the check we apply to everyone else."""
    turn = compose(make_report([FINDING]), CUR, PREV, policy, now=NOW)
    folder, _, base = turn.logical_key.rpartition("/")
    issue, number, contributor, slug = forms.turn_parts(base)
    assert issue == forms.issue_number(folder)
    assert contributor == policy.contributor
    assert slug == policy.response_slug("e" * 64)
    assert policy.is_own_turn(turn.logical_key)


def test_next_turn_spans_canonical_and_legacy_numeric(policy):
    view = rev(
        "f" * 64,
        {
            f"{FOLDER}/README.md": (10, "a"),
            f"{FOLDER}/003.md": (10, "b"),
            f"{FOLDER}/007.04-Owner-later.md": (10, "c"),
        },
        meta=META,
    )
    assert next_turn(view, FOLDER) == 5


def test_target_folder_votes_by_diff(policy):
    assert target_folder(make_report([FINDING]), CUR, PREV) == FOLDER


def test_target_folder_falls_back_to_the_newest_issue(policy):
    """Nothing in the diff touches an issue, so the turn goes to the most
    recently opened loop — by issue number, not lexically."""
    issues = {"issues/007-first/README.md": (10, "a"), "issues/012-latest/README.md": (10, "b")}
    prev = rev("e" * 64, issues, meta=META)
    cur = rev("f" * 64, {**issues, "packages.md": (5, "z")}, meta=META)
    assert target_folder(make_report([FINDING]), cur, prev) == "issues/012-latest"


def test_package_with_no_issue_folder_refuses(policy):
    flat = rev("f" * 64, {"README.md": (10, "a"), "issues/closed/041-gate.md": (10, "b")},
               meta=META)
    with pytest.raises(ComposeError, match="no issues/NNN-slug"):
        compose(make_report([FINDING]), flat, None, policy, now=NOW)


def test_clean_pass_refuses_to_compose(policy):
    with pytest.raises(ComposeError):
        compose(make_report([]), CUR, PREV, policy, now=NOW)


def test_engine_error_refuses_to_compose(policy):
    report = make_report([FINDING])
    report.error = "boom"
    with pytest.raises(ComposeError):
        compose(report, CUR, PREV, policy, now=NOW)
