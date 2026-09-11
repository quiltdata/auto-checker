"""Unit tests for the checks against synthetic revisions.

Organized by regime: the current contract (spec:protocol/occurrence.md §§2-8)
first, then the retired one, whose checks are sound only against the
historical corpus.
"""

import json

from check_commit import checks
from check_commit.model import DEFECT, KNOWN_UNRESOLVED, Entry
from check_commit.policy import POLICY_DIR

from conftest import META, STAMP, FakeCtx, rev


def kinds(findings):
    return sorted((f.check, f.kind) for f in findings)


# A minimal conforming current-model package: one issue folder with a README
# and one filed turn.
FOLDER = "issues/007-measure-selection-scope"
BASE = {
    "README.md": (100, "h0"),
    f"{FOLDER}/README.md": (200, "hr"),
    f"{FOLDER}/007.01-Owner-opening.md": (50, "h1"),
}

OPEN_README = b"""# 007 - Measure selection scope

- **Opened:** 2026-09-04T00:00:00Z
- **Originator:** owner.example.occurrence
- **Status:** open

Context and current summary.
"""

CLOSED_README = b"""# 007 - Measure selection scope

- **Opened:** 2026-09-04T00:00:00Z
- **Originator:** owner.example.occurrence
- **Status:** closed
- **Closed:** 2026-09-06T00:00:00Z
- **Closed-By:** owner.example.occurrence

## Result

Disposed.
"""


# ==========================================================================
# current regime
# ==========================================================================

# --- workflow-stamp ---------------------------------------------------------

def test_unstamped_revision_flagged(ctx):
    cur = rev("b" * 64, BASE, meta=META, workflow=None)
    fs = checks.check_workflow_stamp(None, cur, ctx)
    assert kinds(fs) == [("workflow-stamp", "unstamped-revision")]
    assert "never validated" in fs[0].detail


def test_wrong_workflow_flagged(ctx):
    cur = rev("b" * 64, BASE, meta=META, workflow={"id": "something-else"})
    assert kinds(checks.check_workflow_stamp(None, cur, ctx)) == [
        ("workflow-stamp", "wrong-workflow")
    ]


def test_stamped_revision_passes(ctx):
    cur = rev("b" * 64, BASE, meta=META)
    assert checks.check_workflow_stamp(None, cur, ctx) == []


# --- metadata-shape ---------------------------------------------------------

def test_required_metadata_fields(ctx):
    cur = rev("b" * 64, BASE, meta={"status": "active"})
    assert kinds(checks.check_metadata_shape(None, cur, ctx)) == [
        ("metadata-shape", "missing-required-field")
    ]


def test_bad_package_status(ctx):
    cur = rev("b" * 64, BASE, meta={"related_packages": {}, "status": "open"})
    assert kinds(checks.check_metadata_shape(None, cur, ctx)) == [("metadata-shape", "bad-status")]


def test_retired_field_returning_is_flagged(ctx):
    """The regression #7 named: a retired field back via an unvalidated patch."""
    cur = rev("b" * 64, BASE, meta={**META, "delta": "x", "messages_added": []})
    fs = checks.check_metadata_shape(None, cur, ctx)
    assert kinds(fs) == [("metadata-shape", "forbidden-field")] * 2
    assert "additionalProperties" in fs[0].detail


def test_route_key_is_not_a_forbidden_field(ctx):
    cur = rev("b" * 64, BASE, meta={**META, FOLDER: "spec"})
    assert checks.check_metadata_shape(None, cur, ctx) == []


# --- issue-routes -----------------------------------------------------------

def test_dangling_route_flagged(policy):
    cur = rev("b" * 64, BASE, meta={**META, "issues/099-nonexistent": "spec"})
    ctx = FakeCtx(policy, contents={f"{FOLDER}/README.md": OPEN_README})
    assert kinds(checks.check_issue_routes(None, cur, ctx)) == [
        ("issue-routes", "dangling-route")
    ]


def test_route_on_open_issue_passes(policy):
    cur = rev("b" * 64, BASE, meta={**META, FOLDER: "spec"})
    ctx = FakeCtx(policy, contents={f"{FOLDER}/README.md": OPEN_README})
    assert checks.check_issue_routes(None, cur, ctx) == []


def test_route_surviving_closure_flagged(policy):
    """§8 step 2. Reproduces the defect the pin of the current-regime corpus
    repairs: `remove stale route after closed disposition`."""
    cur = rev("b" * 64, BASE, meta={**META, FOLDER: "spec"})
    ctx = FakeCtx(policy, contents={f"{FOLDER}/README.md": CLOSED_README})
    fs = checks.check_issue_routes(None, cur, ctx)
    assert kinds(fs) == [("issue-routes", "route-survives-closure")]
    assert fs[0].severity == DEFECT


def test_legacy_flat_route_resolves_to_its_file(policy):
    """The schema keeps flat `.md` route keys valid during migration."""
    flat = "issues/047-packet-fence-review.md"
    entries = {**BASE, flat: (10, "hf")}
    cur = rev("b" * 64, entries, meta={**META, flat: "owner.spec.occurrence"})
    ctx = FakeCtx(policy, contents={flat: OPEN_README})
    assert checks.check_issue_routes(None, cur, ctx) == []


def test_unreadable_routed_readme_is_known_unresolved(policy):
    cur = rev("b" * 64, BASE, meta={**META, FOLDER: "spec"})
    ctx = FakeCtx(policy)  # content() returns None
    fs = checks.check_issue_routes(None, cur, ctx)
    assert [f.severity for f in fs] == [KNOWN_UNRESOLVED]


# --- issue-readme -----------------------------------------------------------

def _readme_ctx(policy, body):
    return FakeCtx(policy, contents={f"{FOLDER}/README.md": body})


def test_readme_missing_status_flagged(policy):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/README.md": (201, "hr2")}, meta=META)
    body = b"# 007 - x\n\n- **Opened:** 2026-09-04T00:00:00Z\n- **Originator:** o\n"
    fs = checks.check_issue_readme(prev, cur, _readme_ctx(policy, body))
    assert kinds(fs) == [("issue-readme", "missing-field")]
    assert "'Status'" in fs[0].detail


def test_closed_readme_without_provenance_flagged(policy):
    """Reproduces d883aaff in the current-regime corpus: `Close 052 after
    Executive acceptance` set Status: closed and nothing else."""
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/README.md": (201, "hr2")}, meta=META)
    body = b"# 007 - x\n\n- **Opened:** 2026-09-04T00:00:00Z\n- **Originator:** o\n- **Status:** closed\n"
    fs = checks.check_issue_readme(prev, cur, _readme_ctx(policy, body))
    assert kinds(fs) == [("issue-readme", "closure-provenance-missing")]
    assert "Closed, Closed-By" in fs[0].detail


def test_bad_issue_status_flagged(policy):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/README.md": (201, "hr2")}, meta=META)
    body = b"# 007 - x\n\n- **Opened:** o\n- **Originator:** o\n- **Status:** pending\n"
    assert kinds(checks.check_issue_readme(prev, cur, _readme_ctx(policy, body))) == [
        ("issue-readme", "bad-status")
    ]


def test_conforming_readme_passes(policy):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/README.md": (201, "hr2")}, meta=META)
    assert checks.check_issue_readme(prev, cur, _readme_ctx(policy, CLOSED_README)) == []
    assert checks.check_issue_readme(prev, cur, _readme_ctx(policy, OPEN_README)) == []


def test_new_readme_must_lead_with_its_h1(policy):
    """§5 as amended by spec:053, which the 053 README itself got wrong first."""
    other = "issues/008-new-loop"
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{other}/README.md": (10, "hn")}, meta=META)
    header_first = b"- **Opened:** o\n- **Originator:** o\n- **Status:** open\n\n# 008 - x\n"
    ctx = FakeCtx(policy, contents={f"{other}/README.md": header_first})
    assert kinds(checks.check_issue_readme(prev, cur, ctx)) == [
        ("issue-readme", "header-before-h1")
    ]


def test_existing_header_first_readme_is_not_retrofitted(policy):
    """spec:053: artifacts written under the former grammar stay valid. Only a
    README the revision creates is held to H1-first."""
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/README.md": (201, "hr2")}, meta=META)
    header_first = b"- **Opened:** o\n- **Originator:** o\n- **Status:** open\n\n# 007 - x\n"
    assert checks.check_issue_readme(prev, cur, _readme_ctx(policy, header_first)) == []


# --- turn-form --------------------------------------------------------------

def test_numeric_turn_name_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/002.md": (10, "hn")}, meta=META)
    fs = checks.check_turn_form(prev, cur, ctx)
    assert kinds(fs) == [("turn-form", "numeric-turn-name")]


def test_malformed_turn_name_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/notes.md": (10, "hn")}, meta=META)
    assert kinds(checks.check_turn_form(prev, cur, ctx)) == [
        ("turn-form", "malformed-turn-name")
    ]


def test_turn_naming_the_wrong_issue_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/009.02-PM-review.md": (10, "hn")}, meta=META)
    assert kinds(checks.check_turn_form(prev, cur, ctx)) == [
        ("turn-form", "wrong-issue-prefix")
    ]


def test_turn_collision_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/007.01-PM-also-first.md": (10, "hn")}, meta=META)
    assert kinds(checks.check_turn_form(prev, cur, ctx)) == [("turn-form", "turn-collision")]


def test_conforming_turn_passes(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/007.02-PM-closure-audit.md": (10, "hn")}, meta=META)
    assert checks.check_turn_form(prev, cur, ctx) == []


def test_readme_is_the_one_unnumbered_entry(ctx):
    """A newly created issue folder's README is not a malformed turn."""
    other = "issues/008-new-loop"
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{other}/README.md": (10, "hn")}, meta=META)
    assert checks.check_turn_form(prev, cur, ctx) == []


# --- turn-immutability ------------------------------------------------------

def test_mutated_turn_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/007.01-Owner-opening.md": (80, "h1x")}, meta=META)
    fs = checks.check_turn_immutability(prev, cur, ctx)
    assert kinds(fs) == [("turn-immutability", "turn-mutated")]
    assert "50 -> 80" in fs[0].detail


def test_mutated_readme_allowed(ctx):
    """The README is expressly mutable: it carries current loop state."""
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/README.md": (150, "hrx")}, meta=META)
    assert checks.check_turn_immutability(prev, cur, ctx) == []


def test_legacy_numeric_turn_is_also_immutable(ctx):
    entries = {**BASE, f"{FOLDER}/001.md": (10, "hn")}
    prev = rev("a" * 64, entries, meta=META)
    cur = rev("b" * 64, {**entries, f"{FOLDER}/001.md": (11, "hnx")}, meta=META)
    assert kinds(checks.check_turn_immutability(prev, cur, ctx)) == [
        ("turn-immutability", "turn-mutated")
    ]


# --- entry-count ------------------------------------------------------------

def test_entry_count_claim_verified(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev(
        "b" * 64,
        {**BASE, f"{FOLDER}/007.02-PM-x.md": (10, "hn")},
        message="Add the 007.02 audit; expected entry-count delta +1",
        meta=META,
    )
    assert checks.check_entry_count(prev, cur, ctx) == []


def test_entry_count_mismatch_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev(
        "b" * 64,
        {**BASE, f"{FOLDER}/007.02-PM-x.md": (10, "hn")},
        message="Migrate the thread (expected entry-count delta -3)",
        meta=META,
    )
    fs = checks.check_entry_count(prev, cur, ctx)
    assert kinds(fs) == [("entry-count", "entry-count-mismatch")]
    assert "-3" in fs[0].detail and "+1" in fs[0].detail


def test_short_entry_count_form_also_parsed(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev(
        "b" * 64,
        {**BASE, f"{FOLDER}/007.02-PM-x.md": (10, "hn")},
        message="Open 007a (+1 entry)",
        meta=META,
    )
    assert checks.check_entry_count(prev, cur, ctx) == []


def test_no_claim_is_not_a_finding(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/007.02-PM-x.md": (10, "hn")},
              message="Add the audit", meta=META)
    assert checks.check_entry_count(prev, cur, ctx) == []


# The 4ba6ce73 manifest loss, from spec:issues/closed/041 Incident 1: closing
# 039 deleted nine issue paths and re-added one, dropping the manifest 43 -> 35
# while the commit message asserted the relocation had happened. Paths and
# message are the real ones, read from the manifest in quilt-ernest-staging.
RELOCATED = [
    "issues/039-canonical-job-model.md",
    "issues/closed/039-inventory.md",
    "issues/closed/039a-program-management-gate.md",
    "issues/closed/039b-program-management-rereview.md",
    "issues/closed/039c-program-management-release-review.md",
    "issues/closed/039d-occurrence-workflow-schema.md",
    "issues/closed/039e-program-management-1a-release.md",
    "issues/closed/039f-program-management-3a-review.md",
    "issues/closed/039g-program-management-3a-release.md",
]
LOSS_MESSAGE = (
    "Close 039: all five step-4 roster issues filed. Relocate the parent and "
    "its eight response and support artifacts to issues/closed/ in a single "
    "revision per the thread-membership rule."
)


def test_relocation_that_loses_entries_flagged(ctx):
    filler = {f"protocol/doc{i:02d}.md": (10, f"f{i}") for i in range(34)}
    prev = rev("a" * 64, {**filler, **{p: (10, "hx") for p in RELOCATED}}, meta=META)
    cur = rev(
        "b" * 64,
        {**filler, "issues/closed/039-canonical-job-model.md": (10, "hx")},
        message=LOSS_MESSAGE,
        meta=META,
    )
    assert len(prev.entries) == 43 and len(cur.entries) == 35
    fs = checks.check_entry_count(prev, cur, ctx)
    assert kinds(fs) == [("entry-count", "relocation-not-net-zero")]
    assert "9 entries left and only 1 arrived" in fs[0].detail


def test_honest_relocation_passes(ctx):
    moved = {**BASE}
    del moved[f"{FOLDER}/007.01-Owner-opening.md"]
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev(
        "b" * 64,
        {**moved, f"{FOLDER}/007.01-Owner-renamed.md": (50, "h1")},
        message="Move the opening turn to a human-readable filename.",
        meta=META,
    )
    assert checks.check_entry_count(prev, cur, ctx) == []


# --- pinned-citation --------------------------------------------------------

DOC = f"{FOLDER}/007.02-PM-evidence.md"


def _doc_ctx(policy, text, **kw):
    return FakeCtx(policy, contents={DOC: text.encode()}, **kw)


def _with_doc(text):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, DOC: (len(text), "hd")}, meta=META)
    return prev, cur


def test_unpinned_cross_package_citation_flagged(policy):
    text = "Evidence: quilt+s3://b#package=occurrence/theory&path=README.md"
    prev, cur = _with_doc(text)
    fs = checks.check_pinned_citation(prev, cur, _doc_ctx(policy, text))
    assert kinds(fs) == [("pinned-citation", "unpinned-citation")]


def test_latest_is_not_a_pin(policy):
    text = "Evidence: quilt+s3://b#package=occurrence/theory@latest"
    prev, cur = _with_doc(text)
    assert kinds(checks.check_pinned_citation(prev, cur, _doc_ctx(policy, text))) == [
        ("pinned-citation", "unpinned-citation")
    ]


def test_pinned_cross_package_citation_passes(policy):
    text = f"Evidence: quilt+s3://b#package=occurrence/theory@{'c' * 64}"
    prev, cur = _with_doc(text)
    assert checks.check_pinned_citation(prev, cur, _doc_ctx(policy, text)) == []


def test_current_guidance_may_float(policy):
    """§7: normative Spec guidance may float; every other package may not."""
    text = "Rules: quilt+s3://b#package=occurrence/spec&path=protocol/occurrence.md"
    prev, cur = _with_doc(text)
    ctx = _doc_ctx(policy, text)
    assert checks.check_pinned_citation(prev, cur, ctx) == []
    assert any("floats occurrence/spec" in n for n in ctx.notes)


def test_citation_form_example_is_not_a_citation(policy):
    """protocol/recruitment.md shows the form; QUILT_URI_RE truncates it at the
    `<`, leaving a bucket-less template that is neither pinned nor unpinned."""
    text = "Task: quilt+s3://...#package=...@<revision>&path=..."
    prev, cur = _with_doc(text)
    ctx = _doc_ctx(policy, text)
    assert checks.check_pinned_citation(prev, cur, ctx) == []
    assert checks.check_uris(prev, cur, ctx) == []


def test_same_package_uri_is_not_cross_package(policy):
    text = "See quilt+s3://b#package=occurrence/testpkg&path=README.md"
    prev, cur = _with_doc(text)
    assert checks.check_pinned_citation(prev, cur, _doc_ctx(policy, text)) == []


# --- key-drift --------------------------------------------------------------

def test_logical_physical_drift_flagged(ctx):
    """Reproduces the class recorded in auto-checker#12: a relocation that
    moved the logical key and left the object where it was."""
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/001.md": (10, "hn")}, meta=META)
    cur.entries[f"{FOLDER}/001.md"] = Entry(
        size=10, hash="hn", physical_key="s3://b/occurrence/testpkg/issues/007aO-legacy.md"
    )
    fs = checks.check_key_drift(prev, cur, ctx)
    assert kinds(fs) == [("key-drift", "logical-physical-drift")]
    assert "issues/007aO-legacy.md" in fs[0].detail


def test_foreign_backing_flagged(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, "notes.md": (10, "hn")}, meta=META)
    cur.entries["notes.md"] = Entry(
        size=10, hash="hn", physical_key="s3://other-bucket/occurrence/testpkg/notes.md"
    )
    assert kinds(checks.check_key_drift(prev, cur, ctx)) == [("key-drift", "foreign-backing")]


def test_aligned_keys_pass(ctx):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, f"{FOLDER}/007.02-PM-x.md": (10, "hn")}, meta=META)
    assert checks.check_key_drift(prev, cur, ctx) == []


# --- schema-drift -----------------------------------------------------------

VENDORED = (POLICY_DIR / "occurrence-workflow-schema.json").read_bytes()
SCHEMA_PATH = "protocol/occurrence-workflow-schema.json"


def test_package_schema_matching_the_vendored_copy_passes(policy):
    cur = rev("b" * 64, {**BASE, SCHEMA_PATH: (len(VENDORED), "hs")}, meta=META)
    ctx = FakeCtx(policy, contents={SCHEMA_PATH: VENDORED})
    assert checks.check_schema_drift(None, cur, ctx) == []


def test_package_schema_drift_flagged(policy):
    stale = json.loads(VENDORED)
    stale["required"] = ["related_packages", "open_issues", "status"]
    cur = rev("b" * 64, {**BASE, SCHEMA_PATH: (10, "hs")}, meta=META)
    ctx = FakeCtx(policy, contents={SCHEMA_PATH: json.dumps(stale).encode()})
    fs = checks.check_schema_drift(None, cur, ctx)
    assert kinds(fs) == [("schema-drift", "package-schema-drift")]


def test_registered_schema_drift_flagged_when_online(policy):
    uri = STAMP["schemas"]["occurrence"]
    stale = json.loads(VENDORED)
    del stale["additionalProperties"]
    cur = rev("b" * 64, BASE, meta=META)
    ctx = FakeCtx(policy, objects={uri: json.dumps(stale).encode()}, online=True)
    assert kinds(checks.check_schema_drift(None, cur, ctx)) == [
        ("schema-drift", "registered-schema-drift")
    ]


def test_registered_schema_unverified_offline(policy):
    cur = rev("b" * 64, BASE, meta=META)
    ctx = FakeCtx(policy)
    assert checks.check_schema_drift(None, cur, ctx) == []
    assert any("offline" in n for n in ctx.notes)


# --- watchlist-size, current regime ----------------------------------------

WATCHED = {**BASE, "protocol/occurrence.md": (29206, "hw")}


def test_current_regime_reads_only_the_commit_message(ctx):
    """§3 moved rationale into the commit message, so a `delta` field — which
    the schema forbids anyway — no longer declares anything."""
    prev = rev("a" * 64, WATCHED, meta=META)
    shrunk = {**WATCHED, "protocol/occurrence.md": (7142, "hwx")}
    cur = rev("b" * 64, shrunk, message="Execute the migration.",
              meta={**META, "delta": "Condense protocol/occurrence.md."})
    assert kinds(checks.check_watchlist(prev, cur, ctx)) == [
        ("watchlist-size", "undeclared-shrink")
    ]
    declared = rev("b" * 64, shrunk, message="Condense protocol/occurrence.md around §5.",
                   meta=META)
    assert checks.check_watchlist(prev, declared, ctx) == []


# --- uri-resolution ---------------------------------------------------------

def test_malformed_and_unresolvable_uris(policy):
    text = (
        "See quilt+s3://b#package=occurrence/testpkg@ffff1111&path=README.md "
        "and the broken quilt+s3://b#nopackage=here one."
    )
    prev, cur = _with_doc(text)
    ctx = _doc_ctx(policy, text, tophashes=["b" * 64])
    fs = checks.check_uris(prev, cur, ctx)
    assert ("uri-resolution", "malformed-uri") in kinds(fs)
    assert ("uri-resolution", "unresolvable-pin") in kinds(fs)


def test_unreadable_document_is_known_unresolved(policy):
    prev = rev("a" * 64, BASE, meta=META)
    cur = rev("b" * 64, {**BASE, DOC: (5, "hd")}, meta=META)
    ctx = FakeCtx(policy)  # no contents: content() returns None
    fs = checks.check_uris(prev, cur, ctx)
    assert kinds(fs) == [("uri-resolution", "unreadable-document")]
    assert all(f.severity == KNOWN_UNRESOLVED for f in fs)


def test_resolving_pin_passes(policy):
    pinned = "c" * 64
    text = f"See quilt+s3://b#package=occurrence/testpkg@{pinned[:8]}&path=README.md."
    prev, cur = _with_doc(text)
    ctx = _doc_ctx(
        policy,
        text,
        tophashes=[pinned],
        views={pinned: rev(pinned, {"README.md": (5, "h")})},
    )
    assert checks.check_uris(prev, cur, ctx) == []


# --- the registry itself ----------------------------------------------------

def test_regime_selects_the_checks():
    from check_commit.checks import checks_for

    current = {n for n, _ in checks_for("current")}
    legacy = {n for n, _ in checks_for("pre-migration")}
    # the dead checks are unreachable under the current contract
    assert not {"delta-set", "metadata-hygiene", "filename-form", "issue-paths"} & current
    # and the current checks never judge the historical corpus
    assert not {"turn-form", "issue-routes", "entry-count", "metadata-shape"} & legacy
    assert {"watchlist-size", "uri-resolution"} <= current & legacy


# ==========================================================================
# pre-migration regime — retired contract, historical corpus only
# ==========================================================================

LEGACY_BASE = {
    "README.md": (100, "h0"),
    "02-measure-selection/02.01K-start.md": (50, "h1"),
    "issues/closed/007-old.md": (10, "h2"),
}


# --- delta-set --------------------------------------------------------------

def test_undeclared_change_flagged(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE)
    cur = rev("b" * 64, {**LEGACY_BASE, "README.md": (120, "h0x")},
              message="unrelated message", meta={"delta": "something about nothing"})
    fs = checks.check_delta_set(prev, cur, legacy_ctx)
    assert ("delta-set", "undeclared-change") in kinds(fs)


def test_mentioned_change_passes(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE)
    cur = rev("b" * 64, {**LEGACY_BASE, "README.md": (120, "h0x")},
              meta={"delta": "Amend README.md tracking section."})
    assert checks.check_delta_set(prev, cur, legacy_ctx) == []


def test_fresh_structured_claim_not_performed(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE, meta={"messages_added": []})
    cur = rev("b" * 64, {**LEGACY_BASE, "README.md": (120, "h0x")},
              meta={"delta": "Add 02.02M and amend README.md.",
                    "messages_added": ["02-measure-selection/02.02M-new.md"]})
    fs = checks.check_delta_set(prev, cur, legacy_ctx)
    assert ("delta-set", "claimed-not-performed") in kinds(fs)


def test_set_contains_exactly_mismatch(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE)
    cur = rev("b" * 64, {**LEGACY_BASE, "issues/closed/007-old.md": (12, "h2x")},
              meta={"delta": "SET CONTAINS EXACTLY: issues/007-old.md. Appends the check."})
    fs = checks.check_delta_set(prev, cur, legacy_ctx)
    assert ("delta-set", "set-exact-mismatch") in kinds(fs)


def test_stale_claim_left_to_metadata_check(legacy_ctx):
    stale = {"changes": ["nonexistent/file.md"]}
    prev = rev("a" * 64, LEGACY_BASE, meta=stale)
    cur = rev("b" * 64, {**LEGACY_BASE, "README.md": (120, "h0x")},
              meta={**stale, "delta": "Amend README.md."})
    assert ("delta-set", "claimed-not-performed") not in kinds(
        checks.check_delta_set(prev, cur, legacy_ctx)
    )
    assert ("metadata-hygiene", "stale-inherited-field") in kinds(
        checks.check_metadata(prev, cur, legacy_ctx)
    )


# --- watchlist-size ---------------------------------------------------------

LEGACY_WATCHED = {**LEGACY_BASE, "02-measure-selection/02-results-summary.md": (14784, "hs")}
SUMMARY = "02-measure-selection/02-results-summary.md"


def test_undeclared_shrink_flagged(legacy_ctx):
    prev = rev("a" * 64, LEGACY_WATCHED)
    cur = rev("b" * 64, {**LEGACY_WATCHED, SUMMARY: (9459, "hsx")},
              meta={"delta": "Amend interpretation of the 02 cycle in 02-results-summary.md."})
    assert ("watchlist-size", "undeclared-shrink") in kinds(
        checks.check_watchlist(prev, cur, legacy_ctx)
    )


def test_declared_shrink_in_same_sentence_passes(legacy_ctx):
    prev = rev("a" * 64, LEGACY_WATCHED)
    cur = rev("b" * 64, {**LEGACY_WATCHED, SUMMARY: (9459, "hsx")},
              meta={"delta": "Condense 02-results-summary.md around the corrected projector."})
    assert checks.check_watchlist(prev, cur, legacy_ctx) == []


def test_marker_in_other_sentence_does_not_declare(legacy_ctx):
    prev = rev("a" * 64, LEGACY_WATCHED)
    cur = rev("b" * 64, {**LEGACY_WATCHED, SUMMARY: (9459, "hsx")},
              meta={"delta": "Rewrite issues/009 to the reduced question. Amend 02-results-summary.md."})
    assert ("watchlist-size", "undeclared-shrink") in kinds(
        checks.check_watchlist(prev, cur, legacy_ctx)
    )


def test_replacement_pair_not_a_removal(legacy_ctx):
    watched = {**LEGACY_BASE, "01-backstory/08P-structural-geometry.md": (500, "hp")}
    prev = rev("a" * 64, watched)
    cur = rev("b" * 64, {**LEGACY_BASE, "01-backstory/08P-rank2.md": (900, "hp2")},
              meta={"delta": "Replace provisional 08P with 08P-rank2.md synthesis."})
    assert checks.check_watchlist(prev, cur, legacy_ctx) == []


# --- filename-form ----------------------------------------------------------

def test_bare_name_in_dotted_folder(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE)
    cur = rev("b" * 64, {**LEGACY_BASE, "02-measure-selection/37C-audit.md": (10, "hn")},
              meta={"delta": "Add 02-measure-selection/37C-audit.md."})
    assert ("filename-form", "bare-form") in kinds(
        checks.check_filenames(prev, cur, legacy_ctx)
    )


def test_bare_name_in_new_folder_tolerated(legacy_ctx):
    prev = rev("a" * 64, {"README.md": (100, "h0")})
    cur = rev("b" * 64, {"README.md": (100, "h0"), "05-new-topic/01P-framing.md": (10, "hn")},
              meta={"delta": "Start 05-new-topic with 01P-framing.md."})
    assert checks.check_filenames(prev, cur, legacy_ctx) == []


def test_counter_collision_defect(legacy_ctx):
    prev = rev("a" * 64, {**LEGACY_BASE, "02-measure-selection/02.31M-x.md": (10, "hm")})
    cur = rev("b" * 64, {**LEGACY_BASE, "02-measure-selection/02.31M-x.md": (10, "hm"),
                         "02-measure-selection/02.31C-y.md": (10, "hc")},
              meta={"delta": "Add 02.31C-y.md."})
    fs = checks.check_filenames(prev, cur, legacy_ctx)
    assert ("filename-form", "counter-collision") in kinds(fs)
    assert all(f.severity == DEFECT for f in fs)


def test_adjudicated_collision_is_known_unresolved(legacy_ctx):
    prev = rev("a" * 64, {**LEGACY_BASE, "02-measure-selection/02.21P-x.md": (10, "hp")})
    cur = rev("b" * 64, {**LEGACY_BASE, "02-measure-selection/02.21P-x.md": (10, "hp"),
                         "02-measure-selection/02.21K-y.md": (10, "hk")},
              meta={"delta": "Add 02.21K-y.md."})
    fs = checks.check_filenames(prev, cur, legacy_ctx)
    assert [f.severity for f in fs] == [KNOWN_UNRESOLVED]


# --- issue-paths ------------------------------------------------------------

def test_resurrection_flagged(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE)
    cur = rev("b" * 64, {**LEGACY_BASE, "issues/007-old.md": (10, "hz")},
              meta={"delta": "Reopen issues/007."})
    assert ("issue-paths", "resurrected-closed-issue") in kinds(
        checks.check_issue_paths(prev, cur, legacy_ctx)
    )


def test_closure_vacated_flagged(legacy_ctx):
    prev = rev("a" * 64, LEGACY_BASE)
    entries = {k: v for k, v in LEGACY_BASE.items() if k != "issues/closed/007-old.md"}
    cur = rev("b" * 64, entries, meta={"delta": "Remove the 007 closure."})
    assert ("issue-paths", "closure-vacated") in kinds(
        checks.check_issue_paths(prev, cur, legacy_ctx)
    )


def test_legitimate_close_passes(legacy_ctx):
    prev = rev("a" * 64, {**LEGACY_BASE, "issues/008-live.md": (10, "hl")})
    cur = rev("b" * 64, {**LEGACY_BASE, "issues/closed/008-live.md": (10, "hl")},
              meta={"delta": "Close 008: move issues/008-live.md to issues/closed/."})
    assert checks.check_issue_paths(prev, cur, legacy_ctx) == []
