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

import argparse

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


# --- an object version is proof only as a whole identity ---------------------

def _entry(size, hash_, hash_type, pk):
    return Entry(size=size, hash=hash_, physical_key=pk, hash_type=hash_type)


PK = "s3://protology/occurrence/theory/00-x.md?versionId=abc123"


def test_one_object_version_proves_identical_content():
    """The case the tri-state exists for: algorithms differ, same object
    version, so the bytes are the same bytes."""
    a = _entry(10, "sha", "sha2-256-chunked", PK)
    b = _entry(10, "crc", "CRC64NVME", PK)
    assert a.same_content_as(b) is True


def test_a_matching_version_on_a_different_object_proves_nothing():
    """A versionId is only meaningful against the object it belongs to, and an
    entry may be backed at a different key from one revision to the next."""
    a = _entry(10, "sha", "sha2-256-chunked", PK)
    b = _entry(10, "crc", "CRC64NVME", PK.replace("00-x.md", "01-y.md"))
    assert a.same_content_as(b) is None
    other_bucket = _entry(10, "crc", "CRC64NVME", PK.replace("protology", "elsewhere"))
    assert a.same_content_as(other_bucket) is None


def test_a_null_version_is_not_a_pin():
    """S3 reports versionId=null for an object written while versioning was
    suspended, and every such object carries it, so it proves nothing."""
    null_pk = "s3://protology/occurrence/theory/00-x.md?versionId=null"
    a = _entry(10, "sha", "sha2-256-chunked", null_pk)
    b = _entry(10, "crc", "CRC64NVME", null_pk)
    assert a.object_version is None
    assert a.same_content_as(b) is None


def test_an_unversioned_key_is_not_a_pin():
    a = _entry(10, "sha", "sha2-256-chunked", "s3://protology/occurrence/theory/00-x.md")
    b = _entry(10, "crc", "CRC64NVME", "s3://protology/occurrence/theory/00-x.md")
    assert a.object_version is None
    assert a.same_content_as(b) is None


def test_object_version_decodes_a_percent_encoded_key():
    """So the same object is recognised as the same whichever form its key
    arrives in."""
    a = _entry(10, "sha", "sha2-256-chunked", "s3://b/p/04a-pr%C3%A9cis.md?versionId=v1")
    assert a.object_version == ("b", "/p/04a-précis.md", "v1")


# --- the backtest keeps the introducing publication's findings ---------------

def test_backtest_does_not_overwrite_findings_with_a_republication(tmp_path, monkeypatch, capsys):
    """`by_hash` is keyed by top hash, so checking a re-published pointer would
    diff a manifest against itself and store the empty result over the findings
    the corpus is written about — failing a `must_flag` expectation for a reason
    the corpus never intended."""
    import yaml

    from check_commit import cli

    INTRODUCED = "aa" * 32
    REPUBLISHED = "bb" * 32  # published twice, below

    class StubHistory:
        registry = "s3://protology"
        bucket = "protology"
        package = "occurrence/spec"

        def __init__(self, *a, **k):
            pass

        def revisions(self):
            return [
                ("1", INTRODUCED),
                ("2", REPUBLISHED),
                ("3", REPUBLISHED),  # same manifest, fresh pointer
            ]

        def view(self, tophash, pointer=None):
            # One entry more at REPUBLISHED, so its introducing publication has a
            # real diff and a defect to report.
            entries = {"a.md": Entry(size=1, hash="h", physical_key=None)}
            if tophash == REPUBLISHED:
                entries["issues/12-short/README.md"] = Entry(
                    size=1, hash="h2", physical_key=None
                )
            return RevisionView(
                tophash=tophash,
                pointer=pointer,
                message="",
                meta={"related_packages": {}, "status": "active"},
                entries=entries,
                workflow={"id": "occurrence", "schemas": {}},
            )

        def content(self, view, path):
            return None

        def read_s3_uri(self, uri):
            return None

    monkeypatch.setattr(cli, "PackageHistory", StubHistory)

    exp = tmp_path / "exp.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "regime": "current",
                "package": "occurrence/spec",
                "registry": "s3://protology",
                "pin": REPUBLISHED,
                "must_flag": {
                    REPUBLISHED[:8]: {
                        "hash": REPUBLISHED[:8],
                        "checks": ["turn-form"],
                        "class": "malformed-issue-folder",
                    }
                },
            }
        )
    )

    args = argparse.Namespace(
        expectations=str(exp),
        online=False,
        report=None,
        cache=str(tmp_path / "cache"),
        policy=None,
        regime=None,
    )
    rc = cli.cmd_backtest(args)
    out = capsys.readouterr().out
    assert "re-published a manifest already checked" in out
    assert "BACKTEST PASSED" in out
    assert rc == 0
