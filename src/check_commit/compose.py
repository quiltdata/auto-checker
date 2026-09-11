"""Compose a current-model issue turn from a check report.

`spec:protocol/occurrence.md` §5: a contribution to an issue is an immutable
turn in that issue's folder, named `<issue>.<turn>-<contributor>-<slug>.md`,
beginning with its H1 and a provenance list. There is no `Kind:` taxonomy, no
`Responds to`, and no anaimail envelope — folder membership establishes issue
membership, and the turn sequence establishes order.

The seam: the *body* is a pure rendering of the deterministic report through
the per-prefix template (`policies/<prefix>-body.md`). The turn's path,
title, and provenance are protocol plus context and are code, not template.

Nothing here writes anywhere.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib

from . import policy as forms
from .model import DEFECT, KNOWN_UNRESOLVED, Report, RevisionView
from .policy import Policy

TEMPLATE_DIR = pathlib.Path(__file__).parent / "policies"


class ComposeError(Exception):
    pass


@dataclasses.dataclass
class ComposedTurn:
    logical_key: str  # where the turn would go in the package
    title: str  # the H1, on the first line
    provenance: list[tuple[str, str]]  # the list immediately after the H1
    body: str

    @property
    def text(self) -> str:
        head = "\n".join(f"- **{k}:** {v}" for k, v in self.provenance)
        return f"# {self.title}\n\n{head}\n\n{self.body.strip()}\n"


def render_body(report: Report, pol: Policy, template_path: pathlib.Path | None = None) -> str:
    """Deterministic: same report + template in, same body out."""
    import jinja2

    path = template_path or (TEMPLATE_DIR / f"{pol.prefix}-body.md")
    try:
        source = path.read_text()
    except FileNotFoundError:
        raise ComposeError(f"body template not found: {path}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    findings = [f.to_dict() for f in report.sorted_findings()]
    return env.from_string(source).render(
        report=report.to_dict(),
        findings=findings,
        defect_count=sum(1 for f in findings if f["severity"] == DEFECT),
        ku_count=sum(1 for f in findings if f["severity"] == KNOWN_UNRESOLVED),
        policy=dataclasses.asdict(pol) | {"prefix": pol.prefix},
    )


def _issue_folders(view: RevisionView) -> list[str]:
    """Every `issues/NNN-slug` folder in the manifest, lowest number first."""
    folders = {f for p in view.entries if (f := forms.issue_folder_of(p))}
    return sorted(folders, key=lambda f: (int(forms.issue_number(f)), f))


def target_folder(report: Report, cur: RevisionView, prev: RevisionView | None) -> str:
    """The issue folder the turn belongs to: the one most touched by the
    checked revision; falls back to the highest-numbered issue folder in the
    package (the most recently opened loop)."""
    added, removed, changed = cur.diff(prev)
    votes: dict[str, int] = {}
    for p in [*added, *removed, *changed]:
        folder = forms.issue_folder_of(p)
        if folder:
            votes[folder] = votes.get(folder, 0) + 1
    if votes:
        return max(votes, key=lambda f: (votes[f], f))
    folders = _issue_folders(cur)
    if not folders:
        raise ComposeError("package has no issues/NNN-slug/ folder to file a turn into")
    return folders[-1]


def next_turn(cur: RevisionView, folder: str) -> int:
    """One past the highest turn in the folder, canonical or legacy numeric.

    §5: "By default the highest turn number is the current end of the issue
    sequence." Legacy numeric turns still hold a number and still count.
    """
    top = 0
    for p in cur.entries:
        head, _, base = p.rpartition("/")
        if head != folder or base == "README.md":
            continue
        n = forms.turn_number(base)
        if n is not None:
            top = max(top, n)
    return top + 1


def compose(
    report: Report,
    cur: RevisionView,
    prev: RevisionView | None,
    pol: Policy,
    now: datetime.datetime | None = None,
    template_path: pathlib.Path | None = None,
) -> ComposedTurn:
    if not report.findings:
        raise ComposeError("clean pass: no turn is composed for a revision without findings")
    if report.error:
        raise ComposeError("engine error: a half-informed turn is never composed")
    if not pol.contributor:
        raise ComposeError("policy declares no contributor label to file a turn under")

    folder = target_folder(report, cur, prev)
    issue = forms.issue_number(folder)
    turn = next_turn(cur, folder)
    slug = pol.response_slug(report.tophash)
    logical_key = f"{folder}/{issue}.{turn:02d}-{pol.contributor}-{slug}.md"

    ts = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    defects = sum(1 for f in report.findings if f.severity == DEFECT)
    kus = sum(1 for f in report.findings if f.severity == KNOWN_UNRESOLVED)

    return ComposedTurn(
        logical_key=logical_key,
        title=f"T0 check of revision {report.tophash[:8]} — "
        f"{defects} defect(s), {kus} known-unresolved",
        # §5 requires Opened and Originator; the timestamp source is recorded
        # because an author-reported timestamp is advisory testimony.
        provenance=[
            ("Opened", ts),
            ("Originator", pol.author),
            ("Timestamp-Source", "provided" if now else "measured (system clock)"),
        ],
        body=render_body(report, pol, template_path),
    )
