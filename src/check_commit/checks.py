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
from urllib.parse import urlparse

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


def _issue_status(ctx, view: RevisionView, readme_path: str) -> str | None:
    """`open` / `closed` from an issue README, or None if unreadable."""
    data = ctx.content(view, readme_path)
    if data is None:
        return None
    fields = _provenance(data.decode("utf-8", errors="replace"))
    status = fields.get("Status")
    return status.lower() if status else None


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
        status = (fields.get("Status") or "").lower()
        if status and status not in ("open", "closed"):
            findings.append(
                Finding(
                    check="issue-readme",
                    severity=DEFECT,
                    kind="bad-status",
                    paths=(path,),
                    detail=f"issue Status is {fields['Status']!r}; §5 requires exactly "
                    f"open|closed",
                )
            )
        if status == "closed":
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
            continue
        base = posixpath.basename(path)
        if base == "README.md" or path != f"{folder}/{base}":
            continue  # the one unnumbered entry, or a nested non-turn artifact
        parts = policy.turn_parts(base)
        if parts is None:
            numeric = policy.NUMERIC_TURN_RE.match(base)
            findings.append(
                Finding(
                    check="turn-form",
                    severity=DEFECT,
                    kind="numeric-turn-name" if numeric else "malformed-turn-name",
                    paths=(path,),
                    detail=(
                        "pure numeric turn filename; §5 makes these noncanonical for "
                        "new turns, which take <issue>.<turn>-<contributor>-<slug>.md"
                        if numeric
                        else "turn filename is not <issue>.<turn>-<contributor>-<slug>.md (§5)"
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
                    severity=DEFECT,
                    kind="wrong-issue-prefix",
                    paths=(path,),
                    detail=f"turn names issue {issue} but sits in {folder}; §5 has the "
                    f"issue component repeat the containing issue identifier",
                )
            )
        for other in sorted(cur.entries):
            if other == path or posixpath.dirname(other) != folder:
                continue
            oparts = policy.turn_parts(posixpath.basename(other))
            if oparts and int(oparts[1]) == int(turn):
                findings.append(
                    Finding(
                        check="turn-form",
                        severity=DEFECT,
                        kind="turn-collision",
                        paths=(path, other),
                        detail=f"turn {turn} already taken in {folder}; §5 makes the "
                        f"highest turn number the end of the issue sequence",
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
    added, removed, _ = cur.diff(prev)
    if RELOCATION_RE.search(message) and added and removed and len(removed) > len(added):
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


# -- logical keys are backed at their own physical path ---------------------

def _physical_path(physical_key: str):
    """(bucket, key) of an s3 physical key, ignoring the version query."""
    u = urlparse(physical_key)
    if u.scheme != "s3":
        return None
    return u.netloc, u.path


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
            findings.append(
                Finding(
                    check="key-drift",
                    severity=DEFECT,
                    kind="logical-physical-drift",
                    paths=(path,),
                    detail=f"logical key was written but its object still lives at "
                    f"{key.lstrip('/')!r}: the relocation is logical-only",
                )
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
    pol = ctx.policy
    if not pol.vendored_schema:
        return []
    findings = []
    try:
        vendored = json.loads(pol.vendored_schema_path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"policy names a vendored schema that is missing: {pol.vendored_schema_path}")

    if pol.package_schema_path and pol.package_schema_path in cur.entries:
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
            findings.append(
                Finding(
                    check="schema-drift",
                    severity=DEFECT,
                    kind="package-schema-drift",
                    paths=(pol.package_schema_path,),
                    detail=f"the package's {pol.package_schema_path} differs from the "
                    f"schema vendored at policies/{pol.vendored_schema}; §2 makes a "
                    f"stale schema a defect in its own right",
                )
            )

    # The stamp names the exact registered schema object this revision was
    # validated against, version included — a better source than any constant.
    uri = ((cur.workflow or {}).get("schemas") or {}).get(pol.workflow)
    if uri and ctx.online:
        registered = _load_json(ctx.read_s3_uri(uri))
        if registered is None:
            ctx.note(f"schema-drift: could not fetch the registered schema at {uri}")
        elif registered != vendored:
            findings.append(
                Finding(
                    check="schema-drift",
                    severity=DEFECT,
                    kind="registered-schema-drift",
                    paths=(),
                    detail=f"the registered schema at {uri} differs from the schema "
                    f"vendored at policies/{pol.vendored_schema}",
                )
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
    &path=...`. Such a template arrives here either with its parts elided as
    `...` or truncated at the `@`, because QUILT_URI_RE stops at the `<`. It is
    neither an unresolvable pin nor an unpinned citation, and reporting it as
    either is noise. Deliberately narrow: two signals, both observed in the
    corpus, so no plausible real URI is skipped.
    """
    body = uri[len("quilt+s3://"):].rstrip(".,;:")
    return "..." in body or body.endswith("@")


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
