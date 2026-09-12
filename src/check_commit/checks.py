"""The T0 checks.

Every check takes (prev, cur, ctx) and returns findings. prev is None only for
the first revision of a package, where diff-based checks are vacuous.

Checks are grouped by the regime whose contract they speak for. The current
regime is `spec:protocol/occurrence.md` §§3-8: three-field package metadata
with an optional bounded routing namespace, stable issue folders, immutable
turns, closure in place, and revision-pinned cross-package citation. The
pre-migration regime is the retired flat-issue and message-folder model; its
checks read metadata fields the registered schema now forbids, so they are
sound only against the historical corpus (§9) and never run against a
current-regime package.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any
from urllib.parse import unquote, urlparse

from . import policy
from .model import DEFECT, KNOWN_UNRESOLVED, Finding, RevisionView
from .policy import CURRENT, PRE_MIGRATION

PATH_TOKEN_RE = re.compile(r"[\w][\w./-]*\.(?:md|py|json|yaml|yml|png|txt|csv|sh)")

# `- **Opened:** ...` / `- **Status**: open` — the provenance list of a
# current-model issue README or turn (spec:protocol/occurrence.md §5).
PROVENANCE_RE = re.compile(r"^-\s*\*\*(?P<field>[^*:]+?):?\*\*:?\s*(?P<value>.*?)\s*$")

# `expected entry-count delta -6`, `entry-count delta of +2` (§4).
DELTA_CLAIM_RE = re.compile(r"entry[-\s]?count\s+delta(?:\s+of)?\s*([+-]?\d+)", re.IGNORECASE)
# `(+1 entry)`, `(-3 entries)` — the older shorthand for the same claim.
COUNT_CLAIM_RE = re.compile(r"\(\s*([+-]\d+)\s+entr(?:y|ies)\s*\)", re.IGNORECASE)
# A write that says it relocated entries has claimed a net-zero delta.
RELOCATION_RE = re.compile(r"\b(?:relocat\w*|mov(?:e|ed|es|ing))\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _meta_text(meta: dict[str, Any]) -> str:
    """The retired `delta` prose field. Pre-migration regime only."""
    delta = meta.get("delta")
    return delta if isinstance(delta, str) else json.dumps(delta) if delta else ""


def _declaration_text(cur: RevisionView, ctx) -> str:
    """Where this regime puts a writer's account of the change.

    §3 moved commit rationale out of package metadata and into the commit
    message, so the current regime reads the message only.
    """
    message = cur.message or ""
    if ctx.regime == PRE_MIGRATION:
        return f"{_meta_text(cur.meta or {})} {message}"
    return message


def _mention_tokens(path: str) -> set[str]:
    """Strings whose presence in the declaration counts as naming this file."""
    base = posixpath.basename(path)
    stem = base.rsplit(".", 1)[0]
    tokens = {path, base, stem}
    m = policy.DOTTED_MESSAGE_RE.match(base) or policy.BARE_MESSAGE_RE.match(base)
    if m:
        # the message id, e.g. "02.20C" or "31C"
        tokens.add(base.split("-", 1)[0])
    folder = policy.issue_folder_of(path)
    if folder:
        tokens.add(folder)
        tokens.add(folder.split("/", 1)[1])
        num = policy.issue_number(folder)
        if num:
            tokens.add(num)
    m = policy.OPEN_ISSUE_RE.match(path)
    if m:
        tokens.add(f"issues/{m.group(1)}")
        tokens.add(m.group(1))  # issues are routinely cited by bare number
    m = policy.CLOSED_ISSUE_RE.match(path)
    if m:
        tokens.add(f"issues/closed/{m.group(1)}")
        tokens.add(f"closed/{m.group(1)}")
        tokens.add(m.group(1))
    return tokens


def _extract_paths(value: Any) -> list[str]:
    """Path-like strings from a structured metadata value."""
    if isinstance(value, str):
        return PATH_TOKEN_RE.findall(value)
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(_extract_paths(v))
        return out
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out.extend(_extract_paths(k))
            out.extend(_extract_paths(v))
        return out
    return []


def _mentioned(path: str, text: str) -> bool:
    for tok in _mention_tokens(path):
        if tok.isdigit():
            if re.search(rf"\b{tok}\b", text):
                return True
        elif tok in text:
            return True
    return False


def _matches_actual(claimed: str, pool: list[str]) -> bool:
    """A claimed path matches an actual one exactly or by basename."""
    base = posixpath.basename(claimed)
    return any(p == claimed or posixpath.basename(p) == base for p in pool)


def _provenance(text: str) -> dict[str, str]:
    """Provenance fields of a current-model README or turn.

    Scanning stops at the first `##` heading: below that is body prose, which
    §5 leaves free-form and which must not be mined for header fields.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            break
        m = PROVENANCE_RE.match(line)
        if m:
            fields[m.group("field").strip()] = m.group("value").strip()
    return fields


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _status_token(raw: str | None, *, fold_case: bool = True) -> str | None:
    """The state token of a §5 `Status` field, or None if it names no state.

    §5 as amended: "`Status` begins with a state token, exactly `open` or
    `closed`. An optional annotation may follow the token." So the state is the
    leading word and everything after it is prose. Markdown emphasis around the
    token is ignored, since `**closed** — promoted` is a closure.

    The rule that makes only the *leading* word admissible is §5's other
    sentence: a reader "must not infer from a `closed` appearing later in the
    annotation that the issue is closed", because `open — q9 closed through
    021.29` is open.

    `fold_case` separates two questions that want different answers. Reading an
    issue's state is lenient: a badly-cased `Closed` still tells you the loop is
    shut, and treating it as unknown would lose a closure. Checking the grammar
    is strict, because §5 says `open | closed` exactly — so `bad-status` passes
    `fold_case=False` and reports the casing while the state still reads.
    """
    if not raw:
        return None
    head = raw.strip().lstrip("*_ ").split()
    if not head:
        return None
    token = head[0].strip("*_:;,.—-")
    if fold_case:
        token = token.lower()
    return token if token in ("open", "closed") else None


def _issue_status(ctx, view: RevisionView, readme_path: str) -> str | None:
    """The issue's state token, or None if unreadable or no state is named.

    Callers cannot distinguish "unreadable" from "names no state" here, and
    must not: both mean the loop state is undetermined, and §5 says an
    automated reader must not guess it. `check_issue_readme/bad-status` is what
    reports a Status that names no state.
    """
    data = ctx.content(view, readme_path)
    if data is None:
        return None
    return _status_token(_provenance(data.decode("utf-8", errors="replace")).get("Status"))


def _route_target(key: str, view: RevisionView):
    """(exists, readme_path) for a route key, per the schema's two accepted forms."""
    if policy.ISSUE_FOLDER_RE.match(key):
        # current model: the key is the issue folder's logical path
        exists = any(k == key or k.startswith(key + "/") for k in view.entries)
        return exists, f"{key}/README.md"
    if key.endswith(".md"):
        # legacy flat route, schema-valid during migration
        return key in view.entries, key
    return False, None


# ==========================================================================
# current regime — spec:protocol/occurrence.md §§2-8
# ==========================================================================

# -- the revision was validated at all (§2) --------------------------------

def check_workflow_stamp(prev, cur, ctx) -> list[Finding]:
    want = ctx.policy.workflow
    if not want:
        return []
    got = cur.workflow_id
    if got == want:
        return []
    if got is None:
        return [
            Finding(
                check="workflow-stamp",
                severity=DEFECT,
                kind="unstamped-revision",
                paths=(),
                detail=f"revision written with no workflow: its package metadata was "
                f"never validated against the registered {want!r} schema "
                f"(spec:protocol/occurrence.md §2)",
            )
        ]
    return [
        Finding(
            check="workflow-stamp",
            severity=DEFECT,
            kind="wrong-workflow",
            paths=(),
            detail=f"revision stamped workflow {got!r}, not the registered {want!r}",
        )
    ]


# -- package metadata is the three-field shape the schema declares (§3) -----

def check_metadata_shape(prev, cur, ctx) -> list[Finding]:
    findings = []
    meta = cur.meta or {}
    for field in policy.FIXED_META_FIELDS:
        if field not in meta:
            findings.append(
                Finding(
                    check="metadata-shape",
                    severity=DEFECT,
                    kind="missing-required-field",
                    paths=(),
                    detail=f"package metadata omits required field {field!r} "
                    f"(spec:protocol/occurrence.md §3)",
                )
            )
    status = meta.get("status")
    if status is not None and status not in ("active", "closed"):
        findings.append(
            Finding(
                check="metadata-shape",
                severity=DEFECT,
                kind="bad-status",
                paths=(),
                detail=f"package status {status!r} is not active|closed",
            )
        )
    for key in sorted(meta):
        if key in policy.FIXED_META_FIELDS or policy.ROUTE_KEY_RE.match(key):
            continue
        findings.append(
            Finding(
                check="metadata-shape",
                severity=DEFECT,
                kind="forbidden-field",
                paths=(),
                detail=f"package metadata carries {key!r}, which is neither a fixed "
                f"field nor an issues/ route key; the registered schema sets "
                f"additionalProperties: false",
            )
        )
    # The checks above name the §3 conditions in the spec's own terms, which is
    # worth more than a validator's message. Everything else the schema says —
    # value types, the route-key pattern, the shape of related_packages — is
    # checked by validating against it, as a backstop when nothing above fired
    # so a single fault is not reported twice.
    #
    # This is what `schema-drift/registered-schema-drift` was standing in for.
    # Comparing schema *versions* declared five packages defective while all
    # nine conformed; asking whether the metadata conforms answers the question
    # the proxy was approximating, and names the offending field when it fails.
    if not findings:
        findings.extend(_schema_violations(cur, ctx))
    return findings


def _schema_violations(cur, ctx) -> list[Finding]:
    pol = ctx.policy
    if not pol.vendored_schema:
        return []
    try:
        import jsonschema
    except ImportError:
        ctx.note("metadata-shape: jsonschema unavailable, conformance unverified")
        return []
    try:
        schema = json.loads(pol.vendored_schema_path.read_text())
    except (OSError, ValueError) as exc:
        ctx.note(f"metadata-shape: could not read the vendored schema ({exc})")
        return []
    validator = jsonschema.Draft202012Validator(schema)
    findings = []
    for err in sorted(validator.iter_errors(cur.meta or {}), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        findings.append(
            Finding(
                check="metadata-shape",
                severity=DEFECT,
                kind="nonconforming-metadata",
                paths=(),
                detail=f"package metadata does not satisfy the registered schema at "
                f"{where}: {err.message}",
            )
        )
    return findings


# -- routes name live issues, and no route survives closure (§8 step 2) -----

def check_issue_routes(prev, cur, ctx) -> list[Finding]:
    findings = []
    for key in sorted(cur.meta or {}):
        if key in policy.FIXED_META_FIELDS or not policy.ROUTE_KEY_RE.match(key):
            continue  # metadata-shape owns anything outside the namespace
        exists, readme = _route_target(key, cur)
        if not exists:
            findings.append(
                Finding(
                    check="issue-routes",
                    severity=DEFECT,
                    kind="dangling-route",
                    paths=(key,),
                    detail=f"route key {key!r} names no issue in the manifest",
                )
            )
            continue
        if readme and readme in cur.entries:
            status = _issue_status(ctx, cur, readme)
            if status is None:
                findings.append(
                    Finding(
                        check="issue-routes",
                        severity=KNOWN_UNRESOLVED,
                        kind="unreadable-readme",
                        paths=(readme,),
                        detail=f"could not fetch {readme}; whether its route should "
                        f"have been cleared is unverified",
                    )
                )
            elif status == "closed":
                findings.append(
                    Finding(
                        check="issue-routes",
                        severity=DEFECT,
                        kind="route-survives-closure",
                        paths=(key, readme),
                        detail=f"{readme} is Status: closed but the route {key!r} is "
                        f"still set; §8 step 2 requires closure to remove it",
                    )
                )
    return findings


# -- issue README grammar (§5) ----------------------------------------------

def check_issue_readme(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, _, changed = cur.diff(prev)
    for path in sorted(set(added) | set(changed)):
        if not policy.ISSUE_README_RE.match(path):
            continue
        data = ctx.content(cur, path)
        if data is None:
            findings.append(
                Finding(
                    check="issue-readme",
                    severity=KNOWN_UNRESOLVED,
                    kind="unreadable-readme",
                    paths=(path,),
                    detail=f"could not fetch {path}; its loop state is unverified",
                )
            )
            continue
        text = data.decode("utf-8", errors="replace")
        fields = _provenance(text)
        for field in ("Opened", "Originator", "Status"):
            if field not in fields:
                findings.append(
                    Finding(
                        check="issue-readme",
                        severity=DEFECT,
                        kind="missing-field",
                        paths=(path,),
                        detail=f"issue README has no {field!r} provenance field; §5 "
                        f"requires Opened, Originator, and Status",
                    )
                )
        # §5 as amended: the state is the leading token, exactly `open` or
        # `closed`, and an annotation may follow it. What is faulted is a Status
        # that names no state at all, because §5 then leaves the loop state
        # undefined and forbids an automated reader from guessing it — and
        # because §8's closure obligations are keyed to that token.
        raw_status = fields.get("Status")
        if raw_status and _status_token(raw_status, fold_case=False) is None:
            findings.append(
                Finding(
                    check="issue-readme",
                    severity=DEFECT,
                    kind="bad-status",
                    paths=(path,),
                    detail=f"issue Status is {raw_status!r}, whose leading token is not "
                    f"exactly 'open' or 'closed'; §5 admits an annotation after the "
                    f"token but the token itself is what carries the state, and §8's "
                    f"closure obligations are keyed to it",
                )
            )
        # The state read stays lenient, so a Status whose *grammar* is faulted
        # above is still held to its closure obligations here.
        if _status_token(raw_status) == "closed":
            absent = [f for f in ("Closed", "Closed-By") if f not in fields]
            if absent:
                findings.append(
                    Finding(
                        check="issue-readme",
                        severity=DEFECT,
                        kind="closure-provenance-missing",
                        paths=(path,),
                        detail=f"issue is Status: closed without {', '.join(absent)}; "
                        f"§5 requires closure provenance",
                    )
                )
        # H1 first, provenance immediately after (§5 as amended by spec:053).
        # Artifacts written under the former header-first grammar are valid
        # historical evidence and are not retrofitted, so this applies only to
        # a README the revision creates.
        if path in added and not _first_content_line(text).startswith("# "):
            findings.append(
                Finding(
                    check="issue-readme",
                    severity=DEFECT,
                    kind="header-before-h1",
                    paths=(path,),
                    detail="new issue README does not begin with its H1; §5 puts the "
                    "H1 on the first line and the provenance list immediately after",
                )
            )
    return findings


# -- turn filenames belong to their folder (§5) ------------------------------

def check_turn_form(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, _, _ = cur.diff(prev)
    for path in added:
        folder = policy.issue_folder_of(path)
        if not folder:
            # A path nested under issues/ whose own folder is not NNN-slug is
            # an issue folder nobody can route to or cite. `issues/closed/` is
            # the one exception: §9 keeps historical flat threads as they are.
            parts = path.split("/")
            if len(parts) > 2 and parts[0] == "issues" and parts[1] != "closed":
                findings.append(
                    Finding(
                        check="turn-form",
                        severity=DEFECT,
                        kind="malformed-issue-folder",
                        paths=(path,),
                        detail=f"issues/{parts[1]} is not the NNN-slug form §5 requires "
                        f"of an issue folder, so it cannot carry a route key",
                    )
                )
            continue
        base = posixpath.basename(path)
        if base == "README.md" or path != f"{folder}/{base}":
            continue  # the one unnumbered entry, or a nested non-turn artifact
        # §5 states the turn filename form as this document's only SHOULD, in
        # a document that otherwise reaches for MAY, MUST NOT and MUST exactly
        # once each. Severity follows that distinction: the issue-folder form
        # and the noncanonical-numeric rule are stated flatly and stay defects;
        # departures from the filename grammar are recorded as known-unresolved.
        parts = policy.turn_parts(base)
        if parts is None:
            numeric = policy.NUMERIC_TURN_RE.match(base)
            findings.append(
                Finding(
                    check="turn-form",
                    # "Pure numeric filenames such as `001.md` are noncanonical
                    # for new turns" is flat; the filename form is a SHOULD.
                    severity=DEFECT if numeric else KNOWN_UNRESOLVED,
                    kind="numeric-turn-name" if numeric else "malformed-turn-name",
                    paths=(path,),
                    detail=(
                        "pure numeric turn filename; §5 makes these noncanonical for "
                        "new turns, which take <issue>.<turn>-<contributor>-<slug>.md"
                        if numeric
                        else "turn filename is not <issue>.<turn>-<contributor>-<slug>.md; "
                        "§5 states that form as a SHOULD, so this is recorded, not faulted"
                    ),
                )
            )
            continue
        issue, turn, _, _ = parts
        num = policy.issue_number(folder)
        if num is not None and int(issue) != int(num):
            findings.append(
                Finding(
                    check="turn-form",
                    severity=KNOWN_UNRESOLVED,
                    kind="wrong-issue-prefix",
                    paths=(path,),
                    detail=f"turn names issue {issue} but sits in {folder}; §5 has the "
                    f"issue component repeat the containing issue identifier, within a "
                    f"filename form it states as a SHOULD",
                )
            )
        for other in sorted(cur.entries):
            if other == path or posixpath.dirname(other) != folder:
                continue
            oparts = policy.turn_parts(posixpath.basename(other))
            if oparts and int(oparts[1]) == int(turn):
                findings.append(
                    Finding(
                        # The prefix already adjudicated this class as a
                        # known-unresolved spec condition rather than a fault of
                        # either writer, in issues/closed/030 and
                        # auto-checker#6 — see `adjudicated_collisions` in
                        # policies/occurrence.yaml. The current regime is held
                        # to the same ruling.
                        check="turn-form",
                        severity=KNOWN_UNRESOLVED,
                        kind="turn-collision",
                        paths=(path, other),
                        detail=f"turn {turn} already taken in {folder}; §5 makes the "
                        f"highest turn number the end of the issue sequence, so the "
                        f"sequence position is ambiguous",
                    )
                )
    return findings


# -- a filed turn never changes (§5) ----------------------------------------

def check_turn_immutability(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    _, _, changed = cur.diff(prev)
    for path in changed:
        folder = policy.issue_folder_of(path)
        if not folder:
            continue
        base = posixpath.basename(path)
        if base == "README.md":
            continue  # expressly mutable: it carries current loop state
        if policy.turn_number(base) is None:
            continue
        old, new = prev.entries[path], cur.entries[path]
        # `changed` counts undecidable content identity as changed so the
        # content checks re-read the file. Here a change *is* the violation,
        # so an undecidable comparison must not be reported as a mutation.
        if cur.content_changed(prev, path) is None:
            findings.append(
                Finding(
                    check="turn-immutability",
                    severity=KNOWN_UNRESOLVED,
                    kind="incomparable-turn-digest",
                    paths=(path,),
                    detail=f"{path} is recorded as {old.hash_type or 'an unnamed digest'} in "
                    f"{prev.tophash[:12]} and {new.hash_type or 'an unnamed digest'} in "
                    f"{cur.tophash[:12]}, on differing object versions; whether the filed "
                    f"turn was mutated is unverified",
                )
            )
            continue
        findings.append(
            Finding(
                check="turn-immutability",
                severity=DEFECT,
                kind="turn-mutated",
                paths=(path,),
                detail=f"filed turn changed ({old.size} -> {new.size} bytes); §5 makes "
                f"turns immutable after filing and corrections new turns",
            )
        )
    return findings


# -- the commit message's entry-count claim is arithmetic (§4) ---------------

def _declared_delta(message: str) -> int | None:
    m = DELTA_CLAIM_RE.search(message) or COUNT_CLAIM_RE.search(message)
    return int(m.group(1)) if m else None


def check_entry_count(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    if prev.tophash == cur.tophash:
        # A re-publication of an identical manifest carries the message of the
        # write it re-publishes, and that message's claim was about that write.
        # Reading it as a claim about a diff of nothing would fault a correct
        # message: the engine notes the re-publication instead.
        return []
    message = cur.message or ""
    actual = len(cur.entries) - len(prev.entries)
    declared = _declared_delta(message)
    if declared is not None:
        if declared == actual:
            return []
        return [
            Finding(
                check="entry-count",
                severity=DEFECT,
                kind="entry-count-mismatch",
                paths=(),
                detail=f"commit message states an expected entry-count delta of "
                f"{declared:+d} but the manifest moved {len(prev.entries)} -> "
                f"{len(cur.entries)} ({actual:+d}); §4 requires the claim to be verified",
            )
        ]
    # No explicit claim. A message asserting a relocation has claimed net zero:
    # "a nine-path relocation cannot yield a net -8 entries"
    # (spec:issues/closed/041 Incident 1).
    # Scoped to loss: a relocation claim that removes more than it adds,
    # including one that adds nothing at all. A net *gain* under a relocation
    # claim is ordinarily "moved X, and also added Y", which §4 asks to be
    # declared but which is not the evidence-losing class recorded above.
    added, removed, _ = cur.diff(prev)
    if RELOCATION_RE.search(message) and removed and len(removed) > len(added):
        return [
            Finding(
                check="entry-count",
                severity=DEFECT,
                kind="relocation-not-net-zero",
                paths=tuple(sorted(removed)[:8]),
                detail=f"commit message asserts a relocation, which is net zero, but "
                f"{len(removed)} entries left and only {len(added)} arrived: the "
                f"manifest moved {len(prev.entries)} -> {len(cur.entries)} ({actual:+d})",
            )
        ]
    return []


# -- cross-package evidence is revision-pinned (§7) -------------------------

def check_pinned_citation(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, _, changed = cur.diff(prev)
    for doc in sorted(set(added) | set(changed)):
        if not doc.endswith(".md"):
            continue
        data = ctx.content(cur, doc)
        if data is None:
            continue  # uri-resolution already reports what it cannot read
        text = data.decode("utf-8", errors="replace")
        for uri in sorted(set(policy.QUILT_URI_RE.findall(text))):
            if _is_illustrative(uri):
                continue
            parsed = _parse_quilt_uri(uri)
            if parsed is None:
                continue  # uri-resolution owns malformed URIs
            bucket, pkg, tophash, _ = parsed
            if tophash:
                continue
            if bucket == ctx.bucket and pkg == ctx.package:
                continue  # a package citing itself is not cross-package evidence
            if pkg in ctx.policy.float_ok_packages:
                ctx.note(f"pinned-citation: {doc} floats {pkg} (§7 current-guidance exception)")
                continue
            findings.append(
                Finding(
                    check="pinned-citation",
                    severity=DEFECT,
                    kind="unpinned-citation",
                    paths=(doc,),
                    detail=f"cross-package evidence {uri!r} is not revision-pinned; §7 "
                    f"requires a pinned Quilt+ URI for upstream evidence",
                )
            )
    return findings


# -- entries are backed inside the registry bucket --------------------------
#
# What this check may assert is bounded by what the contract says, and
# spec:protocol/occurrence.md says nothing about physical placement: it speaks
# only of logical paths. A Quilt package is a manifest of references, and
# referencing an object in place — rather than copying it under the package
# prefix — is ordinary, supported use. So an entry backed at some other key
# inside the registry bucket is an observation, recorded as a note, not a
# defect. It was flagged as one because the class was first met as a botched
# closure relocation (auto-checker#12, and c29849f2/fc69cb94 in the current
# corpus), and §8 has since removed relocation from the model entirely:
# "There is no issues/closed/ relocation for current-model issues... Nothing
# moves." With nothing relocating, a mismatch no longer evidences a failed
# move.
#
# Backing *outside* the registry bucket stays a defect. That is data the
# registry may be unable to read or keep, which is a consequence with teeth
# rather than a naming preference.

def _physical_path(physical_key: str):
    """(bucket, key) of an s3 physical key, ignoring the version query.

    A physical key is a URI, so its path is percent-encoded: a logical key
    containing a space or a non-ASCII character arrives here as `%20` or
    `%C3%A9`. The comparison against the logical key is on S3 key names, not
    on URIs, so the path is decoded back to the name S3 actually holds.
    """
    u = urlparse(physical_key)
    if u.scheme != "s3":
        return None
    return u.netloc, unquote(u.path)


def check_key_drift(prev, cur, ctx) -> list[Finding]:
    findings = []
    added, _, changed = cur.diff(prev)
    for path in sorted(set(added) | set(changed)):
        entry = cur.entries[path]
        if not entry.physical_key:
            continue
        parsed = _physical_path(entry.physical_key)
        if parsed is None:
            continue
        bucket, key = parsed
        if bucket != ctx.bucket:
            findings.append(
                Finding(
                    check="key-drift",
                    severity=DEFECT,
                    kind="foreign-backing",
                    paths=(path,),
                    detail=f"logical key is backed in {bucket!r}, not the registry "
                    f"bucket {ctx.bucket!r}",
                )
            )
            continue
        if not key.endswith(f"/{ctx.package}/{path}"):
            ctx.note(
                f"key-drift: {path} is backed at {key.lstrip('/')!r} rather than at "
                f"its own logical path. In-bucket, version-pinned, and permitted — "
                f"the contract governs logical paths only"
            )
    return findings


# -- the registered schema and our copy of it agree (§2) --------------------

def _load_json(data: bytes | None):
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def check_schema_drift(prev, cur, ctx) -> list[Finding]:
    """Report on copies of the workflow schema, without asserting §2 requires them.

    §2 is five lines. It names the workflow id, gives the registered schema's
    *unversioned* canonical path, and places exactly one obligation on a
    package writer: use `workflow="occurrence"`. It does not require a package
    to vendor its own copy of the schema, and it cannot require a revision to
    have been validated against any particular schema *version*, because the
    path it names carries no version.

    The line "§2 makes a stale schema a defect in its own right" is not in §2.
    It originates in auto-checker#12's own framing and was quoted into this
    file as though it were spec text. So the comparisons below are notes.

    Two duties moved out of here rather than being dropped:
      - the vendored copy tracking the registered object is a fact about *this
        repo*, checked in CI (tests/test_registered_schema.py), where its
        `paths: []` finding was really pointing all along;
      - whether metadata actually satisfies the schema is checked by
        `metadata-shape`, which validates against it instead of inferring
        conformance from version equality.
    """
    pol = ctx.policy
    if not pol.vendored_schema:
        return []
    findings = []
    try:
        vendored = json.loads(pol.vendored_schema_path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"policy names a vendored schema that is missing: {pol.vendored_schema_path}")

    if (
        pol.package_schema_path
        and prev is not None
        and pol.package_schema_path in prev.entries
        and pol.package_schema_path not in cur.entries
    ):
        ctx.note(
            f"schema-drift: {pol.package_schema_path} was deleted. §2 does not "
            f"require a package to carry its own copy of the registered schema"
        )
    elif pol.package_schema_path and pol.package_schema_path in cur.entries:
        in_package = _load_json(ctx.content(cur, pol.package_schema_path))
        if in_package is None:
            findings.append(
                Finding(
                    check="schema-drift",
                    severity=KNOWN_UNRESOLVED,
                    kind="unreadable-schema",
                    paths=(pol.package_schema_path,),
                    detail="could not read the package's copy of the workflow schema",
                )
            )
        elif in_package != vendored:
            ctx.note(
                f"schema-drift: the package's {pol.package_schema_path} differs from "
                f"policies/{pol.vendored_schema}. §2 requires neither the copy nor "
                f"that it track any particular version"
            )

    # The stamp names the schema *version* this revision was validated against.
    # A revision stamped an older version was validated against the rules of
    # its day, which is not a defect its author committed — §2 names an
    # unversioned path, so "the registered schema" can only mean the current
    # one. What matters is whether the metadata satisfies today's schema, and
    # `metadata-shape` answers that directly.
    uri = ((cur.workflow or {}).get("schemas") or {}).get(pol.workflow)
    if uri and ctx.online:
        registered = _load_json(ctx.read_s3_uri(uri))
        if registered is None:
            ctx.note(f"schema-drift: could not fetch the registered schema at {uri}")
        elif registered != vendored:
            ctx.note(
                f"schema-drift: this revision was validated against {uri}, which is "
                f"not the schema vendored at policies/{pol.vendored_schema}; see "
                f"metadata-shape for whether the metadata satisfies the current one"
            )
    elif uri:
        ctx.note(f"schema-drift: offline, registered schema at {uri} unverified")
    return findings


# ==========================================================================
# both regimes
# ==========================================================================

# -- no undeclared size decrease on a watchlisted artifact ------------------

def _decrease_declared(path: str, text: str, markers) -> bool:
    """A shrink is declared only if a sentence names the file AND a reduction."""
    tokens = {t.lower() for t in _mention_tokens(path)}
    for sentence in re.split(r"(?<=[.;])\s+|\n", text.lower()):
        if any(t in sentence for t in tokens) and any(m in sentence for m in markers):
            return True
    return False


def check_watchlist(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, removed, changed = cur.diff(prev)
    text = _declaration_text(cur, ctx)
    markers = ctx.policy.decrease_markers

    for path in changed:
        if not ctx.policy.is_watchlisted(path, ctx.regime):
            continue
        old, new = prev.entries[path].size or 0, cur.entries[path].size or 0
        if new < old and not _decrease_declared(path, text, markers):
            findings.append(
                Finding(
                    check="watchlist-size",
                    severity=DEFECT,
                    kind="undeclared-shrink",
                    paths=(path,),
                    detail=f"watchlisted artifact shrank {old} -> {new} bytes with no "
                    f"reduction declared in the commit message",
                )
            )
    for path in removed:
        if not ctx.policy.is_watchlisted(path, ctx.regime):
            continue
        # a removal paired with an addition of the same message id is a
        # replacement/renumber, not a loss (e.g. provisional 08P -> final 08P)
        rid = posixpath.basename(path).split("-", 1)[0]
        if any(posixpath.basename(a).split("-", 1)[0] == rid for a in added):
            continue
        if not _decrease_declared(path, text, markers):
            findings.append(
                Finding(
                    check="watchlist-size",
                    severity=DEFECT,
                    kind="undeclared-removal",
                    paths=(path,),
                    detail="watchlisted artifact removed with no reduction declared",
                )
            )
    return findings


# -- every quilt+s3:// URI in a changed document resolves -------------------

def _is_illustrative(uri: str) -> bool:
    """A URI written as syntax, not as evidence.

    Protocol documents show the citation form rather than a citation:
    `protocol/recruitment.md` carries `quilt+s3://...#package=...@<revision>
    &path=...`. Such a template arrives here with whole components elided as
    `...`, or truncated at the `@` because QUILT_URI_RE stops at the `<`. It is
    neither an unresolvable pin nor an unpinned citation, and reporting it as
    either is noise.

    Deliberately narrow. Only a wholly elided bucket or package counts, so an
    ellipsis inside a real path — `&path=records/...` — does not buy an
    exemption from either URI check.
    """
    body = uri[len("quilt+s3://"):].rstrip(".,;:")
    if body.endswith("@"):
        return True
    bucket, _, frag = body.partition("#")
    if bucket == "...":
        return True
    for part in frag.split("&"):
        key, _, value = part.partition("=")
        if key == "package" and value.partition("@")[0] == "...":
            return True
    return False


def _parse_quilt_uri(uri: str):
    """-> (bucket, package, tophash|None, path|None) or None if malformed.

    `@latest` yields tophash None: it names no revision, which is exactly why
    §7 forbids it for cross-package evidence.
    """
    body = uri[len("quilt+s3://"):].rstrip(".,;:")
    bucket, _, frag = body.partition("#")
    if not bucket or not frag:
        return None
    params = {}
    for part in frag.split("&"):
        k, _, v = part.partition("=")
        params[k] = v
    pkg = params.get("package")
    if not pkg:
        return None
    tophash = None
    if "@" in pkg:
        pkg, _, tophash = pkg.partition("@")
        if tophash == "latest":
            tophash = None
    return bucket, pkg, tophash or None, params.get("path") or None


def _path_in(view: RevisionView, path: str) -> bool:
    return path in view.entries or any(k.startswith(path.rstrip("/") + "/") for k in view.entries)


def check_uris(prev, cur, ctx) -> list[Finding]:
    findings = []
    if prev is None:
        return []
    added, _, changed = cur.diff(prev)
    docs = [p for p in sorted(set(added) | set(changed)) if p.endswith(".md")]
    for doc in docs:
        data = ctx.content(cur, doc)
        if data is None:
            # a document we cannot read is a document we cannot clear
            findings.append(
                Finding(
                    check="uri-resolution",
                    severity=KNOWN_UNRESOLVED,
                    kind="unreadable-document",
                    paths=(doc,),
                    detail=f"could not fetch {doc}; any quilt+s3 URIs in it are unverified",
                )
            )
            continue
        text = data.decode("utf-8", errors="replace")
        for uri in sorted(set(policy.QUILT_URI_RE.findall(text))):
            if _is_illustrative(uri):
                continue  # a citation-form example is not a citation
            parsed = _parse_quilt_uri(uri)
            if parsed is None:
                findings.append(
                    Finding(
                        check="uri-resolution",
                        severity=DEFECT,
                        kind="malformed-uri",
                        paths=(doc,),
                        detail=f"unparseable quilt+s3 URI {uri!r}",
                    )
                )
                continue
            bucket, pkg, tophash, path = parsed
            if bucket == ctx.bucket and pkg == ctx.package:
                target = None
                if tophash:
                    target = ctx.resolve_same_package(tophash)
                    if target is None:
                        findings.append(
                            Finding(
                                check="uri-resolution",
                                severity=DEFECT,
                                kind="unresolvable-pin",
                                paths=(doc,),
                                detail=f"pinned revision {tophash[:12]} cited in {doc} "
                                f"does not exist in {pkg}",
                            )
                        )
                        continue
                else:
                    target = cur
                if path and not _path_in(target, path):
                    findings.append(
                        Finding(
                            check="uri-resolution",
                            severity=DEFECT,
                            kind="missing-path",
                            paths=(doc, path),
                            detail=f"URI in {doc} cites path {path!r} absent from "
                            f"{'revision ' + tophash[:12] if tophash else 'this revision'}",
                        )
                    )
            elif ctx.online:
                ok, why = ctx.resolve_foreign(bucket, pkg, tophash, path)
                if not ok:
                    findings.append(
                        Finding(
                            check="uri-resolution",
                            severity=DEFECT,
                            kind="unresolvable-pin" if tophash else "missing-path",
                            paths=(doc,),
                            detail=f"URI {uri!r} in {doc} does not resolve: {why}",
                        )
                    )
            else:
                ctx.note(f"uri-resolution: offline, foreign URI unverified: {uri}")
    return findings


# ==========================================================================
# pre-migration regime — retired contract, historical corpus only
# ==========================================================================

# -- delta names exactly the files in set ----------------------------------

def check_delta_set(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, removed, changed = cur.diff(prev)
    actual = {"added": added, "removed": removed, "changed": changed}
    all_actual = sorted(set(added) | set(removed) | set(changed))
    prev_meta = prev.meta or {}
    meta = cur.meta or {}
    delta_text = _meta_text(meta)
    fields = ctx.policy.legacy.structured_file_fields

    # (a) every actually-touched file must be named by the revision
    blob = " ".join(
        [delta_text, cur.message or ""] + [json.dumps(meta.get(f)) for f in fields if meta.get(f)]
    )
    for path in all_actual:
        if not _mentioned(path, blob):
            findings.append(
                Finding(
                    check="delta-set",
                    severity=DEFECT,
                    kind="undeclared-change",
                    paths=(path,),
                    detail=f"file is in the revision's set but neither delta, message, "
                    f"nor structured metadata names it",
                )
            )

    # (b) fresh structured claims must have been performed
    for field, target in fields.items():
        value = meta.get(field)
        if not value or value == prev_meta.get(field):
            # absent, or inherited verbatim from the prior revision: stale
            # metadata is metadata-hygiene's finding, not a fresh claim
            continue
        pool = all_actual if target == "any" else actual[target]
        for claimed in _extract_paths(value):
            if not _matches_actual(claimed, pool) and not _matches_actual(claimed, all_actual):
                findings.append(
                    Finding(
                        check="delta-set",
                        severity=DEFECT,
                        kind="claimed-not-performed",
                        paths=(claimed,),
                        detail=f"metadata field {field!r} claims {claimed!r} but the "
                        f"revision's set does not contain it",
                    )
                )

    # (c) an explicit "SET CONTAINS EXACTLY:" declaration is checked literally
    m = re.search(r"SET CONTAINS EXACTLY:?\s*([^\n]*)", delta_text, re.IGNORECASE)
    if m:
        declared = set(PATH_TOKEN_RE.findall(m.group(1).split(". ")[0]))
        if declared and declared != set(all_actual):
            missing = sorted(declared - set(all_actual))
            extra = sorted(set(all_actual) - declared)
            findings.append(
                Finding(
                    check="delta-set",
                    severity=DEFECT,
                    kind="set-exact-mismatch",
                    paths=tuple(missing + extra),
                    detail=f"delta declares SET CONTAINS EXACTLY {sorted(declared)} "
                    f"but the actual set is {all_actual}",
                )
            )
    return findings


# -- no metadata field present that the patch did not set ------------------

def check_metadata(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, removed, changed = cur.diff(prev)
    all_actual = sorted(set(added) | set(removed) | set(changed))
    prev_meta, meta = prev.meta or {}, cur.meta or {}

    for field in tuple(ctx.policy.legacy.structured_file_fields) + ("delta",):
        value = meta.get(field)
        if not value or value != prev_meta.get(field):
            continue
        # identical to the prior revision's value: only a defect if it names
        # files this patch did not touch (i.e. it describes the previous patch)
        stale = [c for c in _extract_paths(value) if not _matches_actual(c, all_actual)]
        if stale:
            findings.append(
                Finding(
                    check="metadata-hygiene",
                    severity=DEFECT,
                    kind="stale-inherited-field",
                    paths=tuple(sorted(set(stale))[:8]),
                    detail=f"metadata field {field!r} is byte-identical to the prior "
                    f"revision's and names files this patch did not touch — "
                    f"present without being set",
                )
            )
    return findings


# -- filename conforms to the folder's form; counters do not collide -------

def _message_parts(folder: str, base: str):
    """(counter, letters, parent) if base is a message file, else None."""
    m = policy.DOTTED_MESSAGE_RE.match(base)
    if m:
        return m.group(2), m.group(3), m.group(1)
    m = policy.BARE_MESSAGE_RE.match(base)
    if m:
        return m.group(1), m.group(2), None
    return None


def check_filenames(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    legacy = ctx.policy.legacy
    added, _, _ = cur.diff(prev)
    for path in added:
        folder, _, base = path.rpartition("/")
        fm = policy.MESSAGE_FOLDER_RE.match(folder)
        if not fm:
            continue
        parts = _message_parts(folder, base)
        if parts is None:
            continue
        counter, letters, parent = parts
        folder_code = fm.group(1)

        # a bare name is a defect only where the folder's convention is already
        # dotted — matching the record's own adjudication (37C was renamed;
        # the folder-opening 01P-problem-framing was not)
        folder_is_dotted = any(
            policy.DOTTED_MESSAGE_RE.match(p.rpartition("/")[2])
            for p in prev.entries
            if p.rpartition("/")[0] == folder
        )
        if parent is None and folder_is_dotted and folder not in legacy.grandfathered_bare_folders:
            findings.append(
                Finding(
                    check="filename-form",
                    severity=DEFECT,
                    kind="bare-form",
                    paths=(path,),
                    detail=f"bare NNL name in {folder}, which uses the PARENT.NNL form "
                    f"the retired message-folder grammar required",
                )
            )
        elif parent is not None and parent != folder_code:
            findings.append(
                Finding(
                    check="filename-form",
                    severity=DEFECT,
                    kind="wrong-parent",
                    paths=(path,),
                    detail=f"message parent code {parent!r} does not match folder {folder!r}",
                )
            )

        # counter collision within the folder
        for other in cur.entries:
            ofolder, _, obase = other.rpartition("/")
            if ofolder != folder or obase == base:
                continue
            oparts = _message_parts(ofolder, obase)
            if oparts is None or oparts[0] != counter:
                continue
            pair_letters = {letters, oparts[1]}
            allowed = legacy.adjudicated_collisions.get((folder, counter))
            if allowed and pair_letters <= allowed:
                findings.append(
                    Finding(
                        check="filename-form",
                        severity=KNOWN_UNRESOLVED,
                        kind="adjudicated-collision",
                        paths=(path, other),
                        detail=f"counter {counter} shared in {folder}; recorded as a "
                        f"known-unresolved spec condition ({legacy.adjudication_cite}), "
                        f"not a defect",
                    )
                )
            else:
                findings.append(
                    Finding(
                        check="filename-form",
                        severity=DEFECT,
                        kind="counter-collision",
                        paths=(path, other),
                        detail=f"counter {counter} already taken in {folder}",
                    )
                )
    return findings


# -- no closed issue resurrected at a vacated issues/ path -----------------

def check_issue_paths(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, removed, _ = cur.diff(prev)
    prev_closed = {
        m.group(1): p for p in prev.entries if (m := policy.CLOSED_ISSUE_RE.match(p))
    }
    cur_closed_nums = {
        m.group(1) for p in cur.entries if (m := policy.CLOSED_ISSUE_RE.match(p))
    }

    for path in added:
        m = policy.OPEN_ISSUE_RE.match(path)
        if m and m.group(1) in prev_closed:
            findings.append(
                Finding(
                    check="issue-paths",
                    severity=DEFECT,
                    kind="resurrected-closed-issue",
                    paths=(path, prev_closed[m.group(1)]),
                    detail=f"issue {m.group(1)} reopened at its vacated open path while "
                    f"its closure exists",
                )
            )
    for path in removed:
        m = policy.CLOSED_ISSUE_RE.match(path)
        if m and m.group(1) not in cur_closed_nums:
            findings.append(
                Finding(
                    check="issue-paths",
                    severity=DEFECT,
                    kind="closure-vacated",
                    paths=(path,),
                    detail=f"closure of issue {m.group(1)} removed from issues/closed/ "
                    f"without a replacement closure",
                )
            )
    return findings


# ==========================================================================
# registry
# ==========================================================================

# (check id, function, regimes that authorize it)
ALL_CHECKS = [
    ("workflow-stamp", check_workflow_stamp, (CURRENT,)),
    ("metadata-shape", check_metadata_shape, (CURRENT,)),
    ("issue-routes", check_issue_routes, (CURRENT,)),
    ("issue-readme", check_issue_readme, (CURRENT,)),
    ("turn-form", check_turn_form, (CURRENT,)),
    ("turn-immutability", check_turn_immutability, (CURRENT,)),
    ("entry-count", check_entry_count, (CURRENT,)),
    ("pinned-citation", check_pinned_citation, (CURRENT,)),
    ("key-drift", check_key_drift, (CURRENT,)),
    ("schema-drift", check_schema_drift, (CURRENT,)),
    ("watchlist-size", check_watchlist, (CURRENT, PRE_MIGRATION)),
    ("uri-resolution", check_uris, (CURRENT, PRE_MIGRATION)),
    ("delta-set", check_delta_set, (PRE_MIGRATION,)),
    ("metadata-hygiene", check_metadata, (PRE_MIGRATION,)),
    ("filename-form", check_filenames, (PRE_MIGRATION,)),
    ("issue-paths", check_issue_paths, (PRE_MIGRATION,)),
]


def checks_for(regime: str):
    """The checks a regime authorizes, in registry order."""
    return [(name, fn) for name, fn, regimes in ALL_CHECKS if regime in regimes]
