"""Hermetic fixtures: synthetic revisions, no AWS, no network."""

from __future__ import annotations

import pytest

from check_commit.model import Entry, RevisionView
from check_commit.policy import Policy, POLICY_DIR


def rev(tophash: str, entries: dict[str, tuple[int, str]], message: str = "", meta=None, pointer="1"):
    """entries: logical_key -> (size, hash)"""
    return RevisionView(
        tophash=tophash,
        pointer=pointer,
        message=message,
        meta=meta or {},
        entries={k: Entry(size=s, hash=h, physical_key=f"s3://b/pkg/{k}") for k, (s, h) in entries.items()},
    )


@pytest.fixture
def policy():
    return Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence")


class FakeCtx:
    """Just enough Context for the checks."""

    def __init__(self, policy, contents=None, tophashes=(), views=None):
        self.policy = policy
        self.bucket = "b"
        self.package = "occurrence/testpkg"
        self.online = False
        self.notes = []
        self._contents = contents or {}
        self._tophashes = list(tophashes)
        self._views = views or {}

    def content(self, view, path):
        return self._contents.get(path)

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
