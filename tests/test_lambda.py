"""Lambda adapter routing tests — hermetic, all AWS clients faked."""

import io
import json

import pytest

import check_commit.lambda_handler as lh
from check_commit.model import Entry
from check_commit.registry import Registry

from conftest import BUCKET, META, PACKAGE, STAMP, rev


class FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(**kwargs):
            self.calls.append((name, kwargs))
            return {"VersionId": "v-staged"} if name == "put_object" else {}

        return record


class FakeRegistryHTTP:
    """The registry's GraphQL endpoint: records each request, answers `result`
    as packagePatch's result, or as the whole response if it carries errors."""

    def __init__(self, result):
        self.result = result
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        body = self.result if "errors" in self.result else {"data": {"packagePatch": self.result}}
        return io.BytesIO(json.dumps(body).encode())

    def variables(self):
        return [json.loads(r.data)["variables"] for r in self.requests]


PATCHED = {"__typename": "PackagePushSuccess", "revision": {"hash": "d" * 64}}


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
    "WRITE_BACK": "true",
}

FOLDER = "issues/007-measure-selection-scope"
BASE = {
    "README.md": (100, "h0"),
    f"{FOLDER}/README.md": (200, "hr"),
    f"{FOLDER}/007.01-Owner-opening.md": (50, "h1"),
}
RESPONSE = f"{FOLDER}/007.03-Checker-t0-check-of-{'b' * 8}.md"


def _handler(monkeypatch, pairs, views, result=PATCHED):
    monkeypatch.setattr(lh, "PackageHistory", lambda *a, **k: FakeHistory(pairs, views))
    http = FakeRegistryHTTP(result)
    monkeypatch.setattr("urllib.request.urlopen", http)
    h = lh.Handler(
        env=ENV,
        s3=FakeClient(),
        sns=FakeClient(),
        cloudwatch=FakeClient(),
        registry=Registry("https://registry.example", "qk_test"),
    )
    h.http = http
    return h


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
    assert not h.s3.calls and not h.http.requests


def test_clean_revision_notify_nothing_write_nothing(wired):
    h, _ = wired
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert out.report.verdict == "pass"
    assert out.report.regime == "current"
    assert not h.s3.calls and not h.http.requests and not h.sns.calls


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

    [sent] = h.http.variables()
    # Absent userMeta preserves the parent's metadata, which is already valid;
    # any field we might send is forbidden by the registered schema.
    # §3 puts rationale and the entry-count claim in the commit message, whose
    # author prefix is how the checker later recognizes its own revision.
    assert sent["params"] == {
        "bucket": BUCKET,
        "name": PACKAGE,
        "message": f"commit-protocol: T0 check of {'b' * 12} — 1 finding(s) filed at "
        f"{RESPONSE}; expected entry-count delta +1",
        "workflow": "occurrence",
    }
    assert out.detail == f"filed {RESPONSE} in {PACKAGE}@{'d' * 12}"


def test_write_back_patches_only_the_response_onto_the_checked_revision(wired):
    """quilt-specs#39: the patch names the response and nothing else under the
    prefix, pinned to the revision the turn number was allocated against."""
    h, views = wired
    make_defective(views)
    h.handle_detail(detail("b" * 64))

    [sent] = h.http.variables()
    assert sent["src"] == {
        "parent": "b" * 64,
        "set": [
            {
                "logicalKey": RESPONSE,
                "physicalKey": f"s3://{BUCKET}/{PACKAGE}/{RESPONSE}?versionId=v-staged",
            }
        ],
    }
    assert h.http.requests[0].get_header("Authorization") == "Bearer qk_test"


def test_notify_only_mode_never_writes(wired):
    h, views = wired
    h.write_back = False
    make_defective(views)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert "notify-only" in out.detail
    assert not h.s3.calls and not h.http.requests


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
    assert not h.s3.calls and not h.http.requests  # never writes


def test_head_moving_before_the_patch_lands_files_nothing_and_raises_nothing(wired):
    """ParentNotLatest naming another latest: another writer won. Its revision
    gets its own check; retrying ours would only fail the same way."""
    h, views = wired
    make_defective(views)
    h.http.result = {
        "__typename": "OperationError",
        "name": "ParentNotLatest",
        "message": "parent is not latest",
        "context": {"parent": "b" * 64, "latest": "c" * 64},
    }
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"
    assert f"head moved to {'c' * 12}" in out.detail
    assert not any("FAILED" in c[1]["Subject"] for c in h.sns.calls)
    assert not any(c[1]["MetricData"][0]["MetricName"] == "EngineErrors" for c in h.cloudwatch.calls)


@pytest.mark.parametrize(
    "rejection",
    [
        {
            "__typename": "InvalidInput",
            "errors": [{"path": "params", "name": "WorkflowValidationError", "message": "no"}],
        },
        {"__typename": "OperationError", "name": "InsufficientPermissions", "message": "no", "context": None},
        # a stack whose registry predates the patch API
        {"errors": [{"message": "Cannot query field 'packagePatch' on type 'Mutation'."}]},
        # lost only write contention: latest still names the checked revision
        {
            "__typename": "OperationError",
            "name": "ParentNotLatest",
            "message": "parent is not latest",
            "context": {"parent": "b" * 64, "latest": "b" * 64},
        },
    ],
)
def test_failed_patch_alerts_and_a_redelivery_files_the_response(wired, rejection):
    """The event goes back to the queue, and the redelivery patches again
    rather than skipping the object the failed run staged."""
    h, views = wired
    make_defective(views)
    record = {"messageId": "m1", "body": json.dumps({"detail": detail("b" * 64)})}

    h.http.result = rejection
    assert h.handle_sqs({"Records": [record]})["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert any(c[1]["Subject"].startswith("[check-commit] write-back FAILED") for c in h.sns.calls)
    assert any(c[1]["MetricData"][0]["MetricName"] == "EngineErrors" for c in h.cloudwatch.calls)

    h.http.result = PATCHED
    assert h.handle_sqs({"Records": [record]})["batchItemFailures"] == []
    assert len(h.http.requests) == 2


def test_failed_staging_alerts_like_a_failed_patch(wired):
    h, views = wired
    make_defective(views)

    def denied(**kwargs):
        raise PermissionError("AccessDenied")

    h.s3.put_object = denied
    with pytest.raises(PermissionError):
        h.handle_detail(detail("b" * 64))
    assert any(c[1]["Subject"].startswith("[check-commit] write-back FAILED") for c in h.sns.calls)
    assert not h.http.requests


def test_idempotent_skip_when_response_already_filed(wired):
    h, views = wired
    make_defective(views)
    views["b" * 64].entries[RESPONSE] = Entry(size=1, hash="hf", physical_key=None)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "skipped"
    assert not h.s3.calls and not h.http.requests


def _own_revision(views, workflow=STAMP):
    """Our own patched turn coming back as a revision event."""
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
    assert not h.s3.calls and not h.http.requests


def test_own_unstamped_revision_alerts(wired):
    """A response revision the registry did not stamp shows up here as a
    self-application failure rather than passing silently."""
    h, views = wired
    _own_revision(views, workflow=None)
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "error"
    assert "self-application failed" in out.detail
    assert any("workflow-stamp" in str(c) for c in h.sns.calls)
    assert not h.s3.calls and not h.http.requests


def test_a_foreign_turn_is_not_mistaken_for_ours(wired):
    """Someone else's single-turn write must be checked, not self-applied."""
    h, views = wired
    cur = views["b" * 64]
    cur.entries = dict(views["a" * 64].entries)
    other = f"{FOLDER}/007.02-Owner-disposition.md"
    cur.entries[other] = Entry(size=5, hash="ho", physical_key=f"s3://{BUCKET}/{PACKAGE}/{other}")
    out = h.handle_detail(detail("b" * 64))
    assert out.action == "checked"


def test_our_turn_shape_alone_is_not_enough(wired):
    """§5 makes the contributor label navigational, so the turn name is only
    one signal. Without the commit message our patch carries, the
    revision is checked like anyone else's."""
    h, views = wired
    _own_revision(views)
    views["b" * 64].message = "someone else's commit; expected entry-count delta +1"
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
