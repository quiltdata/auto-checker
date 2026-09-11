"""Lambda adapter routing tests — hermetic, all AWS clients faked."""

import json

import pytest

import check_commit.lambda_handler as lh
from check_commit.model import Entry

from conftest import BUCKET, META, PACKAGE, STAMP, rev


class FakeClient:
    def __init__(self, existing_keys=()):
        self.calls = []
        self.existing_keys = set(existing_keys)

    def __getattr__(self, name):
        def record(**kwargs):
            self.calls.append((name, kwargs))
            if name == "head_object" and kwargs.get("Key") not in self.existing_keys:
                raise KeyError("404")  # stands in for botocore ClientError
            return {}

        return record


class FakeHistory:
    def __init__(self, pairs, views):
        self._pairs = pairs
        self._views = views
        self.bucket = BUCKET
        self.package = PACKAGE
        self.registry = f"s3://{BUCKET}"

    def revisions(self):
        return self._pairs

    def view(self, tophash, pointer=None):
        return self._views[tophash]

    def content(self, view, path):
        return b""

    def read_s3_uri(self, uri):
        return None


ENV = {
    "PACKAGE_PREFIX": "occurrence",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:1:t",
    "PACKAGER_QUEUE_URL": "https://sqs/q",
    "WRITE_BACK": "true",
}

FOLDER = "issues/007-measure-selection-scope"
BASE = {
    "README.md": (100, "h0"),
    f"{FOLDER}/README.md": (200, "hr"),
    f"{FOLDER}/007.01-Owner-opening.md": (50, "h1"),
}
RESPONSE = f"{FOLDER}/007.03-Checker-t0-check-of-{'b' * 8}.md"


def _handler(monkeypatch, pairs, views):
    monkeypatch.setattr(lh, "PackageHistory", lambda *a, **k: FakeHistory(pairs, views))
    return lh.Handler(
        env=ENV, s3=FakeClient(), sns=FakeClient(), sqs=FakeClient(), cloudwatch=FakeClient()
    )


@pytest.fixture
def wired(monkeypatch):
    """Handler with fake clients and a conforming two-revision history."""
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev(
        "b" * 64,
        {**BASE, f"{FOLDER}/007.02-PM-review.md": (20, "h2")},
        message="Add the 007.02 review; expected entry-count delta +1",
        meta=META,
    )
    pairs = [("1", "a" * 64), ("2", "b" * 64)]
    views = {"a" * 64: prev, "b" * 64: cur}
    return _handler(monkeypatch, pairs, views), views


def detail(tophash):
    return {"version": "0.1", "type": "created", "bucket": BUCKET,
            "handle": PACKAGE, "topHash": tophash}


def make_defective(views):
    """An entry-count claim the manifest does not bear out."""
    views["b" * 64].message = "Add the 007.02 review; expected entry-count delta +4"


def test_outside_prefix_skipped(wired):
    h, _ = wired
    out = h.handle_detail({"bucket": BUCKET, "handle": "other/pkg", "topHash": "x" * 64})
    assert out.action == "skipped"
    assert not h.s3.calls and not h.sqs.calls


def test_clean_revision_notify_nothing_write_nothing(wired):
    h, _ = wired
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert out.report.verdict == "pass"
    assert out.report.regime == "current"
    assert not h.s3.calls and not h.sqs.calls and not h.sns.calls


def test_defect_writes_a_turn_and_no_package_metadata(wired):
    h, views = wired
    make_defective(views)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert out.report.verdict == "defect"
    assert any(c[0] == "publish" for c in h.sns.calls)

    puts = [c for c in h.s3.calls if c[0] == "put_object"]
    assert len(puts) == 1
    assert puts[0][1]["Key"] == f"{PACKAGE}/{RESPONSE}"
    assert puts[0][1]["Body"].startswith(b"# T0 check of revision bbbbbbbb")

    sends = [c for c in h.sqs.calls if c[0] == "send_message"]
    assert len(sends) == 1
    body = json.loads(sends[0][1]["MessageBody"])
    assert body["package_name"] == PACKAGE
    # quilt-specs#39: absent metadata preserves the parent's, which is already
    # valid. Sending any of the old fields would now fail schema validation.
    assert "metadata" not in body
    assert set(body) == {"source_prefix", "package_name", "commit_message"}
    # §3 puts rationale and the entry-count claim in the commit message
    assert RESPONSE in body["commit_message"]
    assert "expected entry-count delta +1" in body["commit_message"]


def test_notify_only_mode_never_writes(wired):
    h, views = wired
    h.write_back = False
    make_defective(views)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert "notify-only" in out.detail
    assert not h.s3.calls and not h.sqs.calls


def test_stale_event_for_superseded_revision_never_writes(monkeypatch):
    """A late/redelivered event for a non-head revision notifies but does not
    compose against the stale snapshot (its turn number could collide with one
    the head has already claimed)."""
    prev = rev("a" * 64, BASE, meta=META)
    mid = rev(
        "b" * 64,
        {**BASE, f"{FOLDER}/007.02-PM-review.md": (20, "h2")},
        message="Add the review; expected entry-count delta +4",
        meta=META,
    )
    head = rev(
        "c" * 64,
        {**BASE, f"{FOLDER}/007.02-PM-review.md": (20, "h2"),
         f"{FOLDER}/007.03-Owner-disposition.md": (9, "h3")},
        message="Add the disposition; expected entry-count delta +1",
        meta=META,
    )
    pairs = [("1", "a" * 64), ("2", "b" * 64), ("3", "c" * 64)]
    h = _handler(monkeypatch, pairs, {"a" * 64: prev, "b" * 64: mid, "c" * 64: head})

    out = h.handle_detail(detail("b" * 64))  # defective, but no longer head
    assert out.action == "checked"
    assert "stale event" in out.detail
    assert any(c[0] == "publish" for c in h.sns.calls)  # still notified
    assert not h.s3.calls and not h.sqs.calls  # never writes


def test_idempotent_skip_when_response_staged_in_s3(wired):
    """Redelivery before the Packager has cut the response revision: the turn
    already sits in S3, so nothing is re-put or re-requested."""
    h, views = wired
    make_defective(views)
    h.s3 = FakeClient(existing_keys={f"{PACKAGE}/{RESPONSE}"})
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "skipped"
    assert "already staged" in out.detail
    assert not [c for c in h.s3.calls if c[0] == "put_object"] and not h.sqs.calls


def test_idempotent_skip_when_response_already_filed(wired):
    h, views = wired
    make_defective(views)
    views["b" * 64].entries[RESPONSE] = Entry(size=1, hash="hf", physical_key=None)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "skipped"
    assert not h.s3.calls and not h.sqs.calls


def _own_revision(views, workflow=STAMP):
    """Our own turn coming back from the Packager."""
    cur = views["b" * 64]
    cur.entries = dict(views["a" * 64].entries)
    cur.entries[RESPONSE] = Entry(
        size=5, hash="hcp", physical_key=f"s3://{BUCKET}/{PACKAGE}/{RESPONSE}"
    )
    cur.message = (
        f"commit-protocol: T0 check of {'b' * 12} — 1 finding(s) filed at "
        f"{RESPONSE}; expected entry-count delta +1"
    )
    cur.workflow = workflow
    return cur


def test_own_revision_routes_to_self_application(wired):
    """Recognized by the shape of the write, not by a metadata `author` field:
    §3 forbids attribution in package metadata."""
    h, views = wired
    _own_revision(views)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "self-applied"
    assert out.report.verdict == "pass"
    assert not h.s3.calls and not h.sqs.calls


def test_own_unstamped_revision_alerts(wired):
    """The Packager gap, if there is one: the queue contract carries no
    workflow field, so an unstamped write shows up here as a self-application
    failure rather than passing silently."""
    h, views = wired
    _own_revision(views, workflow=None)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "error"
    assert "self-application failed" in out.detail
    assert any("workflow-stamp" in str(c) for c in h.sns.calls)
    assert not h.s3.calls and not h.sqs.calls


def test_a_foreign_turn_is_not_mistaken_for_ours(wired):
    """Someone else's single-turn write must be checked, not self-applied."""
    h, views = wired
    cur = views["b" * 64]
    cur.entries = dict(views["a" * 64].entries)
    other = f"{FOLDER}/007.02-Owner-disposition.md"
    cur.entries[other] = Entry(size=5, hash="ho", physical_key=f"s3://{BUCKET}/{PACKAGE}/{other}")
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"


def test_sqs_batch_reports_bad_records(wired):
    h, _ = wired
    event = {"Records": [
        {"messageId": "good", "body": json.dumps({"detail": detail("b" * 64)})},
        {"messageId": "bad", "body": "not json"},
    ]}
    resp = h.handle_sqs(event)
    assert resp["batchItemFailures"] == [{"itemIdentifier": "bad"}]
