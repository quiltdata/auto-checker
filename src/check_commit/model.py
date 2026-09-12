"""Data model: revision views, findings, reports."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

DEFECT = "defect"
KNOWN_UNRESOLVED = "known-unresolved"

VERDICT_ORDER = ["pass", KNOWN_UNRESOLVED, DEFECT, "error"]


@dataclasses.dataclass(frozen=True)
class Entry:
    size: int | None
    hash: str | None
    physical_key: str | None
    # The digest algorithm the manifest recorded, e.g. `sha2-256-chunked` or
    # `CRC64NVME`. Two entries' digests are only comparable under one
    # algorithm, and a registry may migrate algorithms between revisions.
    hash_type: str | None = None

    @property
    def version_id(self) -> str | None:
        """The S3 version the physical key pins, if it pins one."""
        if not self.physical_key:
            return None
        from urllib.parse import parse_qs, urlparse

        vid = parse_qs(urlparse(self.physical_key).query).get("versionId")
        return vid[0] if vid else None

    def same_content_as(self, other: "Entry") -> bool | None:
        """True / False if content identity is decidable, None if it is not.

        Comparable digests settle it. When two revisions were written under
        different hash algorithms the digests carry no information about each
        other, and a versioned physical key settles it instead: an S3 object
        version is immutable, so the same version is the same bytes. With
        neither a common algorithm nor a shared object version, content
        identity cannot be decided from the manifests alone — a differing
        size still proves a difference, but equal sizes prove nothing.
        """
        if self.hash and other.hash and self.hash_type == other.hash_type:
            return self.hash == other.hash and self.size == other.size
        if self.size != other.size:
            return False
        vid = self.version_id
        if vid is not None and vid == other.version_id:
            return True
        return None


@dataclasses.dataclass
class RevisionView:
    """One package revision as the checks see it."""

    tophash: str
    pointer: str | None
    message: str
    meta: dict[str, Any]  # user_meta: the package metadata the schema governs
    entries: dict[str, Entry]
    # The manifest's workflow stamp, e.g. {"id": "occurrence", "schemas": {...}}.
    # None means the revision was written without a workflow, so its metadata
    # was never validated against the registered schema.
    workflow: dict[str, Any] | None = None

    @property
    def workflow_id(self) -> str | None:
        return (self.workflow or {}).get("id")

    def diff(self, prev: "RevisionView | None"):
        """Return (added, removed, changed) logical keys vs prev.

        An entry whose content identity is undecidable (see
        `Entry.same_content_as`) counts as changed, so every check that
        re-reads changed content still re-reads it. Only
        `check_turn_immutability` treats a change as a violation in itself,
        and it asks `content_changed` for the tri-state rather than inferring
        a mutation from this list.
        """
        pe = prev.entries if prev else {}
        added = sorted(k for k in self.entries if k not in pe)
        removed = sorted(k for k in pe if k not in self.entries)
        changed = sorted(
            k for k, e in self.entries.items() if k in pe and e.same_content_as(pe[k]) is not True
        )
        return added, removed, changed

    def content_changed(self, prev: "RevisionView | None", path: str) -> bool | None:
        """True / False if `path`'s content changed, None if undecidable."""
        if prev is None or path not in prev.entries or path not in self.entries:
            return None
        same = self.entries[path].same_content_as(prev.entries[path])
        return None if same is None else not same


@dataclasses.dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # DEFECT | KNOWN_UNRESOLVED
    kind: str  # short slug for the failure class within the check
    paths: tuple[str, ...]
    detail: str

    def to_dict(self):
        return {
            "check": self.check,
            "severity": self.severity,
            "kind": self.kind,
            "paths": list(self.paths),
            "detail": self.detail,
        }


@dataclasses.dataclass
class Report:
    package: str
    registry: str
    tophash: str
    pointer: str | None
    prev_tophash: str | None
    engine_version: str
    regime: str = ""  # which contract the checks were run against
    findings: list[Finding] = dataclasses.field(default_factory=list)
    checks_run: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None

    @property
    def verdict(self) -> str:
        if self.error:
            return "error"
        if any(f.severity == DEFECT for f in self.findings):
            return DEFECT
        if any(f.severity == KNOWN_UNRESOLVED for f in self.findings):
            return KNOWN_UNRESOLVED
        return "pass"

    @property
    def exit_code(self) -> int:
        return {"pass": 0, KNOWN_UNRESOLVED: 0, DEFECT: 1, "error": 2}[self.verdict]

    def sorted_findings(self):
        return sorted(self.findings, key=lambda f: (f.check, f.kind, f.paths))

    def to_dict(self):
        return {
            "schema": "check-commit-report/1",
            "engine_version": self.engine_version,
            "package": self.package,
            "registry": self.registry,
            "tophash": self.tophash,
            "pointer": self.pointer,
            "prev_tophash": self.prev_tophash,
            "regime": self.regime,
            "checks_run": self.checks_run,
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.sorted_findings()],
            "notes": self.notes,
            "error": self.error,
        }

    def to_json(self):
        # ensure_ascii=False: findings quote package prose, which is full of em
        # dashes and accented characters. Escaping them to \u2014 and \u00e9
        # makes the one field a human reads — `detail` — the hardest part of the
        # report to read. The output is UTF-8; every consumer here handles it.
        return json.dumps(self.to_dict(), indent=2, sort_keys=False, ensure_ascii=False)
