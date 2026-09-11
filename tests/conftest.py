"""Hermetic fixtures: synthetic revisions, no AWS, no network."""

from __future__ import annotations

import pytest

from check_commit.model import Entry, RevisionView
from check_commit.policy import CURRENT, POLICY_DIR, Policy

BUCKET = "b"
PACKAGE = "occurrence/testpkg"

# The manifest workflow stamp a conforming revision carries.
STAMP = {
    "id": "occurrence",
    "config": f"s3://{BUCKET}/.quilt/workflows/config.yml?versionId=v1",
    "schemas": {"occurrence": f"s3://{BUCKET}/.quilt/workflows/occurrence.json?versionId=v1"},
}

# The three-field package metadata the registered schema requires.
META = {"related_packages": {}, "status": "active"}


def rev(
    tophash: str,
    entries: dict[str, tuple[int, str]],
    message: str = "",
    meta=None,
    pointer="1",
    workflow=STAMP,
):
    """entries: logical_key -> (size, hash).

    Physical keys are aligned with logical keys by default, so key-drift only
    fires where a test deliberately misaligns one.
    """
    return RevisionView(
        tophash=tophash,
        pointer=pointer,
        message=message,
        meta={} if meta is None else meta,
        entries={
            k: Entry(size=s, hash=h, physical_key=f"s3://{BUCKET}/{PACKAGE}/{k}")
            for k, (s, h) in entries.items()
        },
        workflow=workflow,
    )


@pytest.fixture
def policy():
    return Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence")


class FakeCtx:
    """Just enough Context for the checks."""

    def __init__(
        self,
        policy,
        contents=None,
        tophashes=(),
        views=None,
        regime=CURRENT,
        objects=None,
        online=False,
    ):
        self.policy = policy
        self.bucket = BUCKET
        self.package = PACKAGE
        self.online = online
        self.regime = regime
        self.notes = []
        self._contents = contents or {}
        self._tophashes = list(tophashes)
        self._views = views or {}
        self._objects = objects or {}

    def content(self, view, path):
        return self._contents.get(path)

    def read_s3_uri(self, uri):
        return self._objects.get(uri)

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def resolve_same_package(self, prefix):
        matches = [t for t in self._tophashes if t.startswith(prefix)]
        if len(matches) != 1:
            return None
        return self._views[matches[0]]

    def resolve_foreign(self, bucket, package, tophash, path):
        return False, "offline test"


@pytest.fixture
def ctx(policy):
    return FakeCtx(policy)


@pytest.fixture
def legacy_ctx(policy):
    """A context for the retired contract, used against the historical corpus."""
    return FakeCtx(policy, regime="pre-migration")
