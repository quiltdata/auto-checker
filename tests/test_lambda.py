"""Lambda adapter routing tests — hermetic, all AWS clients faked."""

import json

import pytest

import check_commit.lambda_handler as lh
from check_commit.model import Entry, RevisionView

from conftest import rev


class FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(**kwargs):
            self.calls.append((name, kwargs))
            return {}

        return record


class FakeHistory:
    def __init__(self, pairs, views):
        self._pairs = pairs
        self._views = views
        self.bucket = "b"
        self.package = "occurrence/testpkg"
        self.registry = "s3://b"

    def revisions(self):
        return self._pairs

    def view(self, tophash, pointer=None):
        return self._views[tophash]

    def content(self, view, path):
        return b""


ENV = {
    "PACKAGE_PREFIX": "occurrence",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:1:t",
    "PACKAGER_QUEUE_URL": "https://sqs/q",
    "WRITE_BACK": "true",
}


@pytest.fixture
def wired(monkeypatch):
    """Handler with fake clients and a two-revision history."""
    prev = rev("a" * 64, {"README.md": (100, "h0"), "02-measure-selection/02.01K-x.md": (10, "h1")})
    cur = rev(
        "b" * 64,
        {
            "README.md": (100, "h0"),
            "02-measure-selection/02.01K-x.md": (10, "h1"),
            "02-measure-selection/02.02M-y.md": (20, "h2"),
        },
        message="Add 02.02M",
        meta={"delta": "Add 02-measure-selection/02.02M-y.md.", "author": "mathematician"},
    )
    pairs = [("1", "a" * 64), ("2", "b" * 64)]
    views = {"a" * 64: prev, "b" * 64: cur}
    monkeypatch.setattr(lh, "PackageHistory", lambda *a, **k: FakeHistory(pairs, views))
    h = lh.Handler(env=ENV, s3=FakeClient(), sns=FakeClient(), sqs=FakeClient(), cloudwatch=FakeClient())
    return h, views


def detail(tophash):
    return {"version": "0.1", "type": "created", "bucket": "b",
            "handle": "occurrence/testpkg", "topHash": tophash}


def test_outside_prefix_skipped(wired):
    h, _ = wired
    out = h.handle_detail({"bucket": "b", "handle": "other/pkg", "topHash": "x" * 64})
    assert out.action == "skipped"
    assert not h.s3.calls and not h.sqs.calls


def test_clean_revision_notify_nothing_write_nothing(wired):
    h, _ = wired
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert out.report.verdict == "pass"
    assert not h.s3.calls and not h.sqs.calls and not h.sns.calls


def make_defective(views):
    """Undeclared change: neither delta nor message names the added file."""
    views["b" * 64].meta = {"delta": "something unrelated", "author": "mathematician"}
    views["b" * 64].message = "routine update"


def test_defect_writes_back_and_notifies(wired):
    h, views = wired
    make_defective(views)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert out.report.verdict == "defect"
    assert any(c[0] == "publish" for c in h.sns.calls)
    puts = [c for c in h.s3.calls if c[0] == "put_object"]
    assert len(puts) == 1
    key = puts[0][1]["Key"]
    assert key.startswith("occurrence/testpkg/02-measure-selection/02.03CP-t0-check-of-")
    sends = [c for c in h.sqs.calls if c[0] == "send_message"]
    assert len(sends) == 1
    body = json.loads(sends[0][1]["MessageBody"])
    assert body["package_name"] == "occurrence/testpkg"
    assert body["metadata"]["author"] == "commit-protocol"
    assert "SET CONTAINS EXACTLY" in body["metadata"]["delta"]


def test_notify_only_mode_never_writes(wired, monkeypatch):
    h, views = wired
    h.write_back = False
    make_defective(views)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert "notify-only" in out.detail
    assert not h.s3.calls and not h.sqs.calls


def test_idempotent_skip_when_response_already_filed(wired):
    h, views = wired
    make_defective(views)
    slugged = f"02-measure-selection/02.03CP-t0-check-of-{'b' * 8}.md"
    views["b" * 64].entries[slugged] = Entry(size=1, hash="hf", physical_key=None)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "skipped"
    assert not h.s3.calls and not h.sqs.calls


def test_own_revision_routes_to_self_application(wired):
    h, views = wired
    cur = views["b" * 64]
    cp_file = "02-measure-selection/02.03CP-t0-check-of-cccccccc.md"
    cur.meta = {
        "author": "commit-protocol",
        "delta": f"SET CONTAINS EXACTLY: {cp_file}. T0 findings for cccccccc.",
    }
    cur.entries = {
        **views["a" * 64].entries,
        cp_file: Entry(size=5, hash="hcp", physical_key=None),
    }
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "self-applied"
    assert not h.s3.calls and not h.sqs.calls


def test_own_revision_bad_shape_alerts(wired):
    h, views = wired
    cur = views["b" * 64]
    cur.meta = {"author": "commit-protocol", "delta": "x"}
    # claims our authorship but the diff is not one CP message file
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "error"
    assert any(c[0] == "publish" for c in h.sns.calls)
    assert not h.s3.calls and not h.sqs.calls


def test_sqs_batch_reports_bad_records(wired):
    h, _ = wired
    event = {"Records": [
        {"messageId": "good", "body": json.dumps({"detail": detail("b" * 64)})},
        {"messageId": "bad", "body": "not json"},
    ]}
    resp = h.handle_sqs(event)
    assert resp["batchItemFailures"] == [{"itemIdentifier": "bad"}]
