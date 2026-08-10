"""Policy: protocol-level forms in code, per-prefix tunables from YAML.

The protocol forms (anaimail file naming, issue paths, quilt+s3 URI syntax)
are properties of the spec, not of any one package — they live here. The
per-prefix tunables (watchlist, adjudicated collisions, grandfathered
folders, metadata field conventions) load from `policies/<prefix>.yaml`,
selected by the same package prefix that scopes the deployed stack.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

# --- protocol-level forms (spec:protocol/anaimail.md; issue-tracker layout) ---

MESSAGE_FOLDER_RE = re.compile(r"^(\d{2})-[\w-]+$")
DOTTED_MESSAGE_RE = re.compile(r"^(\d{2})\.(\d{2})([A-Z]{1,2})-.+\.md$")
BARE_MESSAGE_RE = re.compile(r"^(\d{2})([A-Z]{1,2})-.+\.md$")

OPEN_ISSUE_RE = re.compile(r"^issues/(\d{3})-[^/]+$")
CLOSED_ISSUE_RE = re.compile(r"^issues/closed/(\d{3})-[^/]+$")

QUILT_URI_RE = re.compile(r"quilt\+s3://[^\s\)\]\"'`<>]+")

POLICY_DIR = pathlib.Path(__file__).parent / "policies"


class PolicyError(Exception):
    pass


@dataclasses.dataclass
class Policy:
    """Per-prefix tunables. See policies/<prefix>.yaml for provenance."""

    prefix: str
    watchlist: list[re.Pattern]
    decrease_markers: tuple[str, ...]
    structured_file_fields: dict[str, str]  # field -> added|removed|changed|any
    grandfathered_bare_folders: set[str]
    adjudicated_collisions: dict[tuple[str, str], frozenset[str]]
    adjudication_cite: str

    def is_watchlisted(self, path: str) -> bool:
        return any(p.search(path) for p in self.watchlist)

    @classmethod
    def load(cls, path: pathlib.Path, prefix: str = "") -> "Policy":
        import yaml

        try:
            raw = yaml.safe_load(path.read_text())
        except FileNotFoundError:
            raise PolicyError(f"policy file not found: {path}")
        return cls(
            prefix=prefix or path.stem,
            watchlist=[re.compile(p) for p in raw.get("watchlist", [])],
            decrease_markers=tuple(raw.get("decrease_markers", [])),
            structured_file_fields=dict(raw.get("structured_file_fields", {})),
            grandfathered_bare_folders=set(raw.get("grandfathered_bare_folders", [])),
            adjudicated_collisions={
                (c["folder"], str(c["counter"])): frozenset(str(x) for x in c["letters"])
                for c in raw.get("adjudicated_collisions", [])
            },
            adjudication_cite=raw.get("adjudication_cite", ""),
        )

    @classmethod
    def for_package(cls, package: str, override: str | None = None) -> "Policy":
        """Resolve policy from the package's prefix, or an explicit path.

        A package with no policy is an engine error, not a silent pass — a
        checker that cannot know what to check must say so (exit 2).
        """
        if override:
            return cls.load(pathlib.Path(override))
        prefix = package.split("/", 1)[0]
        return cls.load(POLICY_DIR / f"{prefix}.yaml", prefix=prefix)
