"""Policy: protocol-level forms in code, per-prefix tunables from YAML.

The protocol forms (issue folders, immutable turn filenames, route keys,
quilt+s3 URI syntax) are properties of the spec, not of any one package — they
live here. The per-prefix tunables (watchlist, workflow identity, vendored
schema, legacy adjudications) load from `policies/<prefix>.yaml`, selected by
the same package prefix that scopes the deployed stack.

Two regimes coexist. `spec:protocol/occurrence.md` §5 replaced the flat-issue,
message-folder model with stable issue folders and immutable turns; §9 keeps
the historical revisions valid evidence rather than normalizing them. A check
therefore declares which regime it speaks for, and the engine runs only the
checks that regime authorizes. Regime-scoped tunables live under
`pre_migration:` so nothing retired can be mistaken for live config.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

# --- regimes ----------------------------------------------------------------

CURRENT = "current"  # spec:protocol/occurrence.md §5 folder model
PRE_MIGRATION = "pre-migration"  # flat issues, message folders, metadata annotation
REGIMES = (CURRENT, PRE_MIGRATION)


# --- current-model forms (spec:protocol/occurrence.md §5, §8) ----------------

# `issues/NNN-slug` — the stable issue folder, and the form a route key takes
# in the current model (no trailing slash, no README.md).
ISSUE_FOLDER_RE = re.compile(r"^issues/(\d{3,})-[^/]+$")
ISSUE_README_RE = re.compile(r"^issues/((\d{3,})-[^/]+)/README\.md$")

# `<issue>.<turn>-<contributor>-<slug>.md`, zero-padded as needed. The
# contributor label and slug are navigational only (§5) — they carry no
# provenance authority, so nothing here validates them against a registry.
TURN_RE = re.compile(r"^(\d{2,})\.(\d{2,})-([A-Za-z][A-Za-z0-9]*)-(.+)\.md$")

# `001.md` — noncanonical for new turns, valid as existing evidence (§5).
NUMERIC_TURN_RE = re.compile(r"^(\d{2,})\.md$")

# The bounded metadata namespace the registered schema opens for routing:
# `^issues/[^/]+$`. Legacy flat keys ending in `.md` also match, deliberately,
# so a package can migrate on its next current-model write.
ROUTE_KEY_RE = re.compile(r"^issues/[^/]+$")

# Fixed package-metadata fields (§3). Everything else at top level must be a
# route key; the registered schema's additionalProperties:false says so.
FIXED_META_FIELDS = ("related_packages", "status")


# --- pre-migration forms (retained for the historical corpus, §9) -----------

MESSAGE_FOLDER_RE = re.compile(r"^(\d{2})-[\w-]+$")
DOTTED_MESSAGE_RE = re.compile(r"^(\d{2})\.(\d{2})([A-Z]{1,2})-.+\.md$")
BARE_MESSAGE_RE = re.compile(r"^(\d{2})([A-Z]{1,2})-.+\.md$")

OPEN_ISSUE_RE = re.compile(r"^issues/(\d{3})-[^/]+$")
CLOSED_ISSUE_RE = re.compile(r"^issues/closed/(\d{3})-[^/]+$")


# --- regime-independent forms ------------------------------------------------

QUILT_URI_RE = re.compile(r"quilt\+s3://[^\s\)\]\"'`<>]+")

POLICY_DIR = pathlib.Path(__file__).parent / "policies"


class PolicyError(Exception):
    pass


def issue_folder_of(path: str) -> str | None:
    """The `issues/NNN-slug` folder a logical key sits in, if any."""
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "issues":
        return None
    folder = "issues/" + parts[1]
    return folder if ISSUE_FOLDER_RE.match(folder) else None


def issue_number(folder: str) -> str | None:
    m = ISSUE_FOLDER_RE.match(folder)
    return m.group(1) if m else None


def turn_parts(basename: str):
    """(issue, turn, contributor, slug) for a canonical turn, else None."""
    m = TURN_RE.match(basename)
    return m.groups() if m else None


def turn_number(basename: str) -> int | None:
    """The turn ordinal of a canonical or legacy-numeric turn filename."""
    m = TURN_RE.match(basename)
    if m:
        return int(m.group(2))
    m = NUMERIC_TURN_RE.match(basename)
    return int(m.group(1)) if m else None


@dataclasses.dataclass(frozen=True)
class Legacy:
    """Pre-migration tunables. Read only by pre-migration-regime checks."""

    watchlist: tuple[re.Pattern, ...] = ()
    structured_file_fields: dict[str, str] = dataclasses.field(default_factory=dict)
    grandfathered_bare_folders: frozenset[str] = frozenset()
    adjudicated_collisions: dict[tuple[str, str], frozenset[str]] = dataclasses.field(
        default_factory=dict
    )
    adjudication_cite: str = ""


@dataclasses.dataclass
class Policy:
    """Per-prefix tunables. See policies/<prefix>.yaml for provenance."""

    prefix: str
    author: str  # who the commit message says wrote the revision
    contributor: str  # navigational turn-filename label (§5), no authority
    regime: str  # which contract this prefix is checked against
    workflow: str  # the workflow id every revision must be stamped with (§2)
    package_schema_path: str  # where the package vendors the workflow schema
    vendored_schema: str  # our copy of the registered schema, in policies/
    watchlist: tuple[re.Pattern, ...]
    decrease_markers: tuple[str, ...]
    float_ok_packages: frozenset[str]  # §7: current normative guidance may float
    legacy: Legacy

    def is_watchlisted(self, path: str, regime: str | None = None) -> bool:
        pats = self.legacy.watchlist if (regime or self.regime) == PRE_MIGRATION else self.watchlist
        return any(p.search(path) for p in pats)

    @property
    def vendored_schema_path(self) -> pathlib.Path:
        return POLICY_DIR / self.vendored_schema

    def response_slug(self, tophash: str) -> str:
        """The descriptive slug of the turn this checker files for a revision."""
        return f"t0-check-of-{tophash[:8]}"

    def is_own_turn(self, path: str) -> bool:
        """True if `path` is a turn this checker composed.

        Package metadata no longer carries revision attribution (§3), so the
        checker recognizes its own writes by the shape of the turn it files:
        one turn whose contributor label is ours and whose slug is our own.
        """
        parts = turn_parts(path.rpartition("/")[2])
        if parts is None:
            return False
        _, _, contributor, slug = parts
        return contributor == self.contributor and slug.startswith("t0-check-of-")

    @classmethod
    def load(cls, path: pathlib.Path, prefix: str = "") -> "Policy":
        import yaml

        try:
            raw = yaml.safe_load(path.read_text())
        except FileNotFoundError:
            raise PolicyError(f"policy file not found: {path}")
        regime = raw.get("regime", CURRENT)
        if regime not in REGIMES:
            raise PolicyError(f"unknown regime {regime!r} in {path}; expected one of {REGIMES}")
        legacy_raw = raw.get("pre_migration") or {}
        return cls(
            prefix=prefix or path.stem,
            author=raw.get("author", ""),
            contributor=raw.get("contributor", ""),
            regime=regime,
            workflow=raw.get("workflow", ""),
            package_schema_path=raw.get("package_schema_path", ""),
            vendored_schema=raw.get("vendored_schema", ""),
            watchlist=tuple(re.compile(p) for p in raw.get("watchlist", [])),
            decrease_markers=tuple(raw.get("decrease_markers", [])),
            float_ok_packages=frozenset(raw.get("float_ok_packages", [])),
            legacy=Legacy(
                watchlist=tuple(re.compile(p) for p in legacy_raw.get("watchlist", [])),
                structured_file_fields=dict(legacy_raw.get("structured_file_fields", {})),
                grandfathered_bare_folders=frozenset(
                    legacy_raw.get("grandfathered_bare_folders", [])
                ),
                adjudicated_collisions={
                    (c["folder"], str(c["counter"])): frozenset(str(x) for x in c["letters"])
                    for c in legacy_raw.get("adjudicated_collisions", [])
                },
                adjudication_cite=legacy_raw.get("adjudication_cite", ""),
            ),
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
