"""The six T0 checks (03 §1, 04 §4).

Every check takes (prev, cur, ctx) and returns findings. prev is None only for
the first revision of a package, where diff-based checks are vacuous.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from . import policy
from .model import DEFECT, KNOWN_UNRESOLVED, Finding, RevisionView

PATH_TOKEN_RE = re.compile(r"[\w][\w./-]*\.(?:md|py|json|yaml|yml|png|txt|csv|sh)")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _meta_text(meta: dict[str, Any]) -> str:
    delta = meta.get("delta")
    return delta if isinstance(delta, str) else json.dumps(delta) if delta else ""


def _mention_tokens(path: str) -> set[str]:
    """Strings whose presence in the delta/message counts as naming this file."""
    base = posixpath.basename(path)
    stem = base.rsplit(".", 1)[0]
    tokens = {path, base, stem}
    m = policy.DOTTED_MESSAGE_RE.match(base) or policy.BARE_MESSAGE_RE.match(base)
    if m:
        # the message id, e.g. "02.20C" or "31C"
        tokens.add(base.split("-", 1)[0])
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


# --------------------------------------------------------------------------
# check 1 — delta names exactly the files in set
# --------------------------------------------------------------------------

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

    # (a) every actually-touched file must be named by the revision
    blob = " ".join(
        [delta_text, cur.message or ""]
        + [json.dumps(meta.get(f)) for f in ctx.policy.structured_file_fields if meta.get(f)]
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
    for field, target in ctx.policy.structured_file_fields.items():
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


# --------------------------------------------------------------------------
# check 2 — no undeclared size decrease on a watchlisted artifact
# --------------------------------------------------------------------------

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
    _, removed, changed = cur.diff(prev)
    text = (_meta_text(cur.meta or {})) + " " + (cur.message or "")
    markers = ctx.policy.decrease_markers

    for path in changed:
        if not ctx.policy.is_watchlisted(path):
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
                    f"reduction declared in delta or message",
                )
            )
    added, _, _ = cur.diff(prev)
    for path in removed:
        if not ctx.policy.is_watchlisted(path):
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


# --------------------------------------------------------------------------
# check 3 — filename conforms to the folder's form; counters do not collide
# --------------------------------------------------------------------------

def _message_parts(folder: str, base: str):
    """(counter, letters) if base is a message file, else None."""
    m = policy.DOTTED_MESSAGE_RE.match(base)
    if m:
        return m.group(2), m.group(3), m.group(1)  # counter, letters, parent
    m = policy.BARE_MESSAGE_RE.match(base)
    if m:
        return m.group(1), m.group(2), None
    return None


def check_filenames(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
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
        if parent is None and folder_is_dotted and folder not in ctx.policy.grandfathered_bare_folders:
            findings.append(
                Finding(
                    check="filename-form",
                    severity=DEFECT,
                    kind="bare-form",
                    paths=(path,),
                    detail=f"bare NNL name in {folder}, which uses the PARENT.NNL form "
                    f"required by spec:protocol/anaimail.md Structure §3",
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
            allowed = ctx.policy.adjudicated_collisions.get((folder, counter))
            if allowed and pair_letters <= allowed:
                findings.append(
                    Finding(
                        check="filename-form",
                        severity=KNOWN_UNRESOLVED,
                        kind="adjudicated-collision",
                        paths=(path, other),
                        detail=f"counter {counter} shared in {folder}; recorded as a "
                        f"known-unresolved spec condition ({ctx.policy.adjudication_cite}), "
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


# --------------------------------------------------------------------------
# check 4 — no closed issue resurrected at a vacated issues/ path
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# check 5 — every quilt+s3:// URI in a changed document resolves
# --------------------------------------------------------------------------

def _parse_quilt_uri(uri: str):
    """-> (bucket, package, tophash|None, path|None) or None if malformed."""
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


# --------------------------------------------------------------------------
# check 6 — no metadata field present that the patch did not set
# --------------------------------------------------------------------------

def check_metadata(prev, cur, ctx) -> list[Finding]:
    if prev is None:
        return []
    findings = []
    added, removed, changed = cur.diff(prev)
    all_actual = sorted(set(added) | set(removed) | set(changed))
    prev_meta, meta = prev.meta or {}, cur.meta or {}

    for field in tuple(ctx.policy.structured_file_fields) + ("delta",):
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


ALL_CHECKS = [
    ("delta-set", check_delta_set),
    ("watchlist-size", check_watchlist),
    ("filename-form", check_filenames),
    ("issue-paths", check_issue_paths),
    ("uri-resolution", check_uris),
    ("metadata-hygiene", check_metadata),
]
