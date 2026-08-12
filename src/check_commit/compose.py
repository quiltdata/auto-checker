"""Compose an anaimail message from a check report.

The seam, per 04 §5: the *body* is a pure rendering of the deterministic
report through the per-prefix template (`policies/<prefix>-body.md`) — the
part spec:rubric/ may later reshape. The *envelope* (From/To/Timestamp/
In-Reply-To) and the filename are anaimail protocol plus context — who
speaks, when, into which thread — and are code, not template.

Nothing here writes anywhere.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
import re

from . import policy as forms
from .model import DEFECT, KNOWN_UNRESOLVED, Report, RevisionView
from .policy import Policy

TEMPLATE_DIR = pathlib.Path(__file__).parent / "policies"


class ComposeError(Exception):
    pass


@dataclasses.dataclass
class ComposedMessage:
    logical_key: str  # where the message file would go in the package
    envelope: list[tuple[str, str]]  # ordered header fields
    body: str

    @property
    def text(self) -> str:
        header = "\n".join(f"- {k}: {v}" for k, v in self.envelope)
        return f"{header}\n\n{self.body.strip()}\n"


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


def _message_folders(view: RevisionView) -> list[str]:
    return sorted(
        {p.rpartition("/")[0] for p in view.entries if forms.MESSAGE_FOLDER_RE.match(p.rpartition("/")[0])}
    )


def target_folder(report: Report, cur: RevisionView, prev: RevisionView | None) -> str:
    """The investigation folder the response belongs to: the message folder
    most touched by the checked revision; falls back to the highest-numbered
    folder in the package (the current investigation)."""
    added, removed, changed = cur.diff(prev)
    votes: dict[str, int] = {}
    for p in [*added, *removed, *changed]:
        folder = p.rpartition("/")[0]
        if forms.MESSAGE_FOLDER_RE.match(folder):
            votes[folder] = votes.get(folder, 0) + 1
    if votes:
        return max(votes, key=lambda f: (votes[f], f))
    folders = _message_folders(cur)
    if not folders:
        raise ComposeError("package has no NN- message folders to reply into")
    return folders[-1]


def next_counter(cur: RevisionView, folder: str) -> int:
    """One past the highest counter in the folder, bare or dotted — new
    messages always take the dotted form (E's ruling in the package README:
    'new 01-backstory messages take 01.NNL')."""
    top = 0
    for p in cur.entries:
        f, _, base = p.rpartition("/")
        if f != folder:
            continue
        m = forms.DOTTED_MESSAGE_RE.match(base) or forms.BARE_MESSAGE_RE.match(base)
        if m:
            counter = m.group(2) if m.re is forms.DOTTED_MESSAGE_RE else m.group(1)
            top = max(top, int(counter))
    return top + 1


def in_reply_to(cur: RevisionView, prev: RevisionView | None) -> list[str]:
    """The message file(s) the checked revision added — its immediate parents."""
    added, _, _ = cur.diff(prev)
    out = []
    for p in added:
        folder, _, base = p.rpartition("/")
        if forms.MESSAGE_FOLDER_RE.match(folder) and (
            forms.DOTTED_MESSAGE_RE.match(base) or forms.BARE_MESSAGE_RE.match(base)
        ):
            out.append(base)
    return out


def compose(
    report: Report,
    cur: RevisionView,
    prev: RevisionView | None,
    pol: Policy,
    now: datetime.datetime | None = None,
    template_path: pathlib.Path | None = None,
) -> ComposedMessage:
    if not report.findings:
        raise ComposeError("clean pass: no message is composed for a revision without findings")
    if report.error:
        raise ComposeError("engine error: a half-informed message is never composed")

    folder = target_folder(report, cur, prev)
    folder_code = forms.MESSAGE_FOLDER_RE.match(folder).group(1)
    counter = next_counter(cur, folder)
    slug = f"t0-check-of-{report.tophash[:8]}"
    logical_key = f"{folder}/{folder_code}.{counter:02d}{pol.cast_label}-{slug}.md"

    ts = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_source = "provided" if now else "measured (system clock)"
    defects = sum(1 for f in report.findings if f.severity == DEFECT)
    kus = sum(1 for f in report.findings if f.severity == KNOWN_UNRESOLVED)

    envelope = [
        ("From", pol.cast_label),
        ("To", "all"),
        ("Timestamp", ts),
        ("Timestamp-Source", ts_source),
        ("Subject", f"T0 check of revision {report.tophash[:8]} — {defects} defect(s), {kus} known-unresolved"),
    ]
    parents = in_reply_to(cur, prev)
    if parents:
        envelope.append(("In-Reply-To", ", ".join(parents)))

    return ComposedMessage(
        logical_key=logical_key,
        envelope=envelope,
        body=render_body(report, pol, template_path),
    )
