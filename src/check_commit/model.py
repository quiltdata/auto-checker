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


@dataclasses.dataclass
class RevisionView:
    """One package revision as the checks see it."""

    tophash: str
    pointer: str | None
    message: str
    meta: dict[str, Any]
    entries: dict[str, Entry]

    def diff(self, prev: "RevisionView | None"):
        """Return (added, removed, changed) logical keys vs prev."""
        pe = prev.entries if prev else {}
        added = sorted(k for k in self.entries if k not in pe)
        removed = sorted(k for k in pe if k not in self.entries)
        changed = sorted(
            k
            for k, e in self.entries.items()
            if k in pe and (e.hash != pe[k].hash or e.size != pe[k].size)
        )
        return added, removed, changed


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
            "schema": "check-commit-report/0",
            "engine_version": self.engine_version,
            "package": self.package,
            "registry": self.registry,
            "tophash": self.tophash,
            "pointer": self.pointer,
            "prev_tophash": self.prev_tophash,
            "checks_run": self.checks_run,
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.sorted_findings()],
            "notes": self.notes,
            "error": self.error,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)
