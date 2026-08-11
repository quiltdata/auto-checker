"""Unit tests for the six checks against synthetic revisions."""

from check_commit import checks
from check_commit.model import DEFECT, KNOWN_UNRESOLVED

from conftest import FakeCtx, rev


def kinds(findings):
    return sorted((f.check, f.kind) for f in findings)


BASE = {
    "README.md": (100, "h0"),
    "02-measure-selection/02.01K-start.md": (50, "h1"),
    "issues/closed/007-old.md": (10, "h2"),
}


# --- delta-set ---------------------------------------------------------------

def test_undeclared_change_flagged(ctx):
    prev = rev("a" * 64, BASE)
    cur = rev("b" * 64, {**BASE, "README.md": (120, "h0x")},
              message="unrelated message", meta={"delta": "something about nothing"})
    fs = checks.check_delta_set(prev, cur, ctx)
    assert ("delta-set", "undeclared-change") in kinds(fs)


def test_mentioned_change_passes(ctx):
    prev = rev("a" * 64, BASE)
    cur = rev("b" * 64, {**BASE, "README.md": (120, "h0x")},
              meta={"delta": "Amend README.md tracking section."})
    assert checks.check_delta_set(prev, cur, ctx) == []


def test_fresh_structured_claim_not_performed(ctx):
    prev = rev("a" * 64, BASE, meta={"messages_added": []})
    cur = rev("b" * 64, {**BASE, "README.md": (120, "h0x")},
              meta={"delta": "Add 02.02M and amend README.md.",
                    "messages_added": ["02-measure-selection/02.02M-new.md"]})
    fs = checks.check_delta_set(prev, cur, ctx)
    assert ("delta-set", "claimed-not-performed") in kinds(fs)


def test_set_contains_exactly_mismatch(ctx):
    prev = rev("a" * 64, BASE)
    cur = rev("b" * 64, {**BASE, "issues/closed/007-old.md": (12, "h2x")},
              meta={"delta": "SET CONTAINS EXACTLY: issues/007-old.md. Appends the check."})
    fs = checks.check_delta_set(prev, cur, ctx)
    assert ("delta-set", "set-exact-mismatch") in kinds(fs)


def test_stale_claim_left_to_metadata_check(ctx):
    stale = {"changes": ["nonexistent/file.md"]}
    prev = rev("a" * 64, BASE, meta=stale)
    cur = rev("b" * 64, {**BASE, "README.md": (120, "h0x")},
              meta={**stale, "delta": "Amend README.md."})
    assert ("delta-set", "claimed-not-performed") not in kinds(checks.check_delta_set(prev, cur, ctx))
    assert ("metadata-hygiene", "stale-inherited-field") in kinds(checks.check_metadata(prev, cur, ctx))


# --- watchlist-size ----------------------------------------------------------

WATCHED = {**BASE, "02-measure-selection/02-results-summary.md": (14784, "hs")}


def test_undeclared_shrink_flagged(ctx):
    prev = rev("a" * 64, WATCHED)
    cur = rev("b" * 64, {**WATCHED, "02-measure-selection/02-results-summary.md": (9459, "hsx")},
              meta={"delta": "Amend interpretation of the 02 cycle in 02-results-summary.md."})
    assert ("watchlist-size", "undeclared-shrink") in kinds(checks.check_watchlist(prev, cur, ctx))


def test_declared_shrink_in_same_sentence_passes(ctx):
    prev = rev("a" * 64, WATCHED)
    cur = rev("b" * 64, {**WATCHED, "02-measure-selection/02-results-summary.md": (9459, "hsx")},
              meta={"delta": "Condense 02-results-summary.md around the corrected projector."})
    assert checks.check_watchlist(prev, cur, ctx) == []


def test_marker_in_other_sentence_does_not_declare(ctx):
    prev = rev("a" * 64, WATCHED)
    cur = rev("b" * 64, {**WATCHED, "02-measure-selection/02-results-summary.md": (9459, "hsx")},
              meta={"delta": "Rewrite issues/009 to the reduced question. Amend 02-results-summary.md."})
    assert ("watchlist-size", "undeclared-shrink") in kinds(checks.check_watchlist(prev, cur, ctx))


def test_replacement_pair_not_a_removal(ctx):
    watched = {**BASE, "01-backstory/08P-structural-geometry.md": (500, "hp")}
    prev = rev("a" * 64, watched)
    entries = {**BASE, "01-backstory/08P-rank2.md": (900, "hp2")}
    cur = rev("b" * 64, entries, meta={"delta": "Replace provisional 08P with 08P-rank2.md synthesis."})
    assert checks.check_watchlist(prev, cur, ctx) == []


# --- filename-form -----------------------------------------------------------

def test_bare_name_in_dotted_folder(ctx):
    prev = rev("a" * 64, BASE)
    cur = rev("b" * 64, {**BASE, "02-measure-selection/37C-audit.md": (10, "hn")},
              meta={"delta": "Add 02-measure-selection/37C-audit.md."})
    assert ("filename-form", "bare-form") in kinds(checks.check_filenames(prev, cur, ctx))


def test_bare_name_in_new_folder_tolerated(ctx):
    prev = rev("a" * 64, {"README.md": (100, "h0")})
    cur = rev("b" * 64, {"README.md": (100, "h0"), "05-new-topic/01P-framing.md": (10, "hn")},
              meta={"delta": "Start 05-new-topic with 01P-framing.md."})
    assert checks.check_filenames(prev, cur, ctx) == []


def test_counter_collision_defect(ctx):
    prev = rev("a" * 64, {**BASE, "02-measure-selection/02.31M-x.md": (10, "hm")})
    cur = rev("b" * 64, {**BASE, "02-measure-selection/02.31M-x.md": (10, "hm"),
                         "02-measure-selection/02.31C-y.md": (10, "hc")},
              meta={"delta": "Add 02.31C-y.md."})
    fs = checks.check_filenames(prev, cur, ctx)
    assert ("filename-form", "counter-collision") in kinds(fs)
    assert all(f.severity == DEFECT for f in fs)


def test_adjudicated_collision_is_known_unresolved(ctx):
    prev = rev("a" * 64, {**BASE, "02-measure-selection/02.21P-x.md": (10, "hp")})
    cur = rev("b" * 64, {**BASE, "02-measure-selection/02.21P-x.md": (10, "hp"),
                         "02-measure-selection/02.21K-y.md": (10, "hk")},
              meta={"delta": "Add 02.21K-y.md."})
    fs = checks.check_filenames(prev, cur, ctx)
    assert [f.severity for f in fs] == [KNOWN_UNRESOLVED]


# --- issue-paths -------------------------------------------------------------

def test_resurrection_flagged(ctx):
    prev = rev("a" * 64, BASE)
    cur = rev("b" * 64, {**BASE, "issues/007-old.md": (10, "hz")},
              meta={"delta": "Reopen issues/007."})
    assert ("issue-paths", "resurrected-closed-issue") in kinds(checks.check_issue_paths(prev, cur, ctx))


def test_closure_vacated_flagged(ctx):
    prev = rev("a" * 64, BASE)
    entries = {k: v for k, v in BASE.items() if k != "issues/closed/007-old.md"}
    cur = rev("b" * 64, entries, meta={"delta": "Remove the 007 closure."})
    assert ("issue-paths", "closure-vacated") in kinds(checks.check_issue_paths(prev, cur, ctx))


def test_legitimate_close_passes(ctx):
    prev = rev("a" * 64, {**BASE, "issues/008-live.md": (10, "hl")})
    entries = {**BASE, "issues/closed/008-live.md": (10, "hl")}
    cur = rev("b" * 64, entries, meta={"delta": "Close 008: move issues/008-live.md to issues/closed/."})
    assert checks.check_issue_paths(prev, cur, ctx) == []


# --- uri-resolution ----------------------------------------------------------

def test_malformed_and_unresolvable_uris(policy):
    prev = rev("a" * 64, BASE)
    doc = "02-measure-selection/02.02C-note.md"
    text = (
        "See quilt+s3://b#package=occurrence/testpkg@ffff1111&path=README.md "
        "and the broken quilt+s3://b#nopackage=here one."
    )
    cur = rev("b" * 64, {**BASE, doc: (len(text), "hd")}, meta={"delta": f"Add {doc}."})
    ctx = FakeCtx(policy, contents={doc: text.encode()}, tophashes=["b" * 64], views={})
    fs = checks.check_uris(prev, cur, ctx)
    assert ("uri-resolution", "malformed-uri") in kinds(fs)
    assert ("uri-resolution", "unresolvable-pin") in kinds(fs)


def test_unreadable_document_is_known_unresolved(policy):
    prev = rev("a" * 64, BASE)
    doc = "02-measure-selection/02.02C-note.md"
    cur = rev("b" * 64, {**BASE, doc: (5, "hd")}, meta={"delta": f"Add {doc}."})
    ctx = FakeCtx(policy)  # no contents: content() returns None
    fs = checks.check_uris(prev, cur, ctx)
    assert ("uri-resolution", "unreadable-document") in kinds(fs)
    assert all(f.severity == "known-unresolved" for f in fs)


def test_resolving_pin_passes(policy):
    prev = rev("a" * 64, BASE)
    doc = "02-measure-selection/02.02C-note.md"
    pinned = "c" * 64
    text = f"See quilt+s3://b#package=occurrence/testpkg@{pinned[:8]}&path=README.md."
    cur = rev("b" * 64, {**BASE, doc: (len(text), "hd")}, meta={"delta": f"Add {doc}."})
    ctx = FakeCtx(
        policy,
        contents={doc: text.encode()},
        tophashes=[pinned],
        views={pinned: rev(pinned, {"README.md": (5, "h")})},
    )
    assert checks.check_uris(prev, cur, ctx) == []
