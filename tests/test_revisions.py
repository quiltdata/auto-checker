"""One manifest may sit under several pointers, and the engine must know it.

Re-publishing identical content reuses the content hash and takes a fresh
pointer, so a package's revision list can name the same top hash twice.
`occurrence/theory` does it seven times. Every place that reasoned about
revisions by counting list entries got that wrong, in three different ways:

  - a pinned citation to a duplicated hash read as a revision that does not
    exist, because two entries looked like an ambiguous prefix — this reported
    `uri-resolution/unresolvable-pin` against a full 64-character hash, on the
    revision that had just repaired a real defect;
  - a revision whose parent is itself has an empty diff, so every diff-scoped
    check silently became a no-op while `prev_tophash` still implied a
    comparison had happened;
  - a re-publication carries the message of the write it re-publishes, so a
    message-versus-diff check faulted a correct `+1` claim against a diff of
    nothing.
"""

from __future__ import annotations

import pytest

from check_commit import checks, notify
from check_commit.corpus import PackageHistory
from check_commit.engine import Context, run
from check_commit.model import Entry, RevisionView
from check_commit.policy import POLICY_DIR, Policy

# The shape seen live: occurrence/theory published this manifest at 1789234378
# and re-published it at 1789234473.
TOPHASH = "7530034e" + "b" * 56
POINTERS = [("1789234378", TOPHASH), ("1789234473", TOPHASH)]


@pytest.fixture
def policy():
    return Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence")


class FakeHistory:
    registry = "s3://protology"
    bucket = "protology"
    package = "occurrence/theory"

    def view(self, tophash, pointer=None):
        return RevisionView(
            tophash=tophash,
            pointer=pointer,
            message="",
            meta={},
            entries={"43-pauli-fano.md": Entry(size=1, hash="h", physical_key=None)},
        )

    def content(self, view, path):
        return None

    def read_s3_uri(self, uri):
        return None


# --- a duplicated top hash is not an ambiguous citation ----------------------

def test_republished_tophash_still_resolves_a_pin(policy):
    ctx = Context(FakeHistory(), POINTERS, online=False, policy=policy)
    assert ctx.resolve_same_package(TOPHASH) is not None
    assert ctx.resolve_same_package(TOPHASH[:12]) is not None


def test_a_prefix_over_two_manifests_is_still_ambiguous(policy):
    """The guard the fix must not remove: a short hash that could mean two
    different revisions resolves to neither."""
    ctx = Context(
        FakeHistory(),
        [("1", "7530034e" + "b" * 56), ("2", "7530034e" + "c" * 56)],
        online=False,
        policy=policy,
    )
    assert ctx.resolve_same_package("7530034e") is None


def test_an_absent_hash_resolves_to_nothing(policy):
    ctx = Context(FakeHistory(), POINTERS, online=False, policy=policy)
    assert ctx.resolve_same_package("deadbeef") is None


def test_find_revision_prefers_the_introducing_pointer():
    """The earliest pointer naming a manifest is the publication that
    introduced it, so its message describes the change and its parent is the
    previous distinct manifest. It is also what lambda_handler picks, which
    keeps the CLI reproducing what the deployment reported."""
    h = PackageHistory.__new__(PackageHistory)  # no cache dirs needed
    assert h.find_revision("7530034e", POINTERS) == ("1789234378", TOPHASH)
    assert h.find_revision("dead", POINTERS) is None


def test_cli_selects_the_introducing_pointer():
    from check_commit.cli import _select

    assert _select(POINTERS, "7530034e") == 0
    assert _select(POINTERS, TOPHASH) == 0
    assert _select([("1", "aa" * 32), ("2", "ab" * 32)], "a") is None
    assert _select(POINTERS, "dead") is None


# --- a revision whose parent is itself --------------------------------------

def test_identical_republish_is_noted(policy):
    """`prev_tophash` equal to `tophash` would otherwise leave a reader
    thinking a comparison had happened."""
    cur = RevisionView(
        tophash=TOPHASH,
        pointer="1789234473",
        message="",
        meta={"related_packages": {}, "status": "active"},
        entries={},
        workflow={"id": "occurrence", "schemas": {}},
    )
    prev = RevisionView(**{**cur.__dict__, "pointer": "1789234378"})
    report = run(prev, cur, Context(FakeHistory(), POINTERS, online=False, policy=policy))
    assert any("re-publishes the manifest" in n for n in report.notes)
    assert "re-publishes the manifest" in notify.render(report, policy)


def test_a_real_parent_is_not_noted(policy):
    cur = RevisionView(
        tophash=TOPHASH,
        pointer="1789234473",
        message="",
        meta={"related_packages": {}, "status": "active"},
        entries={},
        workflow={"id": "occurrence", "schemas": {}},
    )
    prev = RevisionView(**{**cur.__dict__, "tophash": "aa" * 32, "pointer": "1789234378"})
    report = run(prev, cur, Context(FakeHistory(), POINTERS, online=False, policy=policy))
    assert not any("re-publishes" in n for n in report.notes)


def test_republication_makes_no_entry_count_claim():
    """A re-publication carries the message of the write it re-publishes, whose
    claim was about that write."""
    entries = {"a.md": Entry(size=1, hash="h", physical_key=None)}
    cur = RevisionView(
        tophash=TOPHASH,
        pointer="1789234473",
        message="Open Theory issue 071. Expected entry-count delta +1.",
        meta={},
        entries=entries,
    )
    prev = RevisionView(**{**cur.__dict__, "pointer": "1789234378"})
    assert checks.check_entry_count(prev, cur, None) == []


def test_a_real_parent_still_has_its_claim_verified():
    """The check the standdown must not disable."""
    cur = RevisionView(
        tophash=TOPHASH,
        pointer="1789234473",
        message="Open Theory issue 071. Expected entry-count delta +1.",
        meta={},
        entries={"a.md": Entry(size=1, hash="h", physical_key=None)},
    )
    prev = RevisionView(**{**cur.__dict__, "tophash": "aa" * 32, "pointer": "1789234378"})
    fs = checks.check_entry_count(prev, cur, None)
    assert [(f.check, f.kind) for f in fs] == [("entry-count", "entry-count-mismatch")]
