"""Render a check report as prose for the findings topic.

SNS email delivery is plain text, so the body a person actually receives was
the report's JSON: `detail` — the one field carrying the finding's substance —
arrived as the deepest-nested string in the payload, with every em dash and
accent escaped to `\\u2014` and `\\u00e9`. The report was legible to a program
and hostile to its reader.

This renders the same report as text that reads as text and happens to be valid
markdown. The JSON is not lost: `lambda_handler` prints it to the log, where a
machine consumer belongs.

Same seam as `compose`: the rendering is a pure function of the report through a
per-prefix template (`policies/<prefix>-notify.md`), so what a notification says
is policy rather than code.
"""

from __future__ import annotations

import dataclasses
import pathlib

from .model import DEFECT, KNOWN_UNRESOLVED, Report
from .policy import Policy

TEMPLATE_DIR = pathlib.Path(__file__).parent / "policies"

# The open catalog's revision view. A finding names a logical path; this is how
# its reader gets to the bytes without assembling a URL by hand.
CATALOG_BASE = "https://open.quiltdata.com"


def catalog_url(report: Report, base: str = CATALOG_BASE) -> str:
    bucket = (report.registry or "").replace("s3://", "").strip("/")
    if not bucket or not report.package:
        return ""
    return f"{base}/b/{bucket}/packages/{report.package}/tree/{report.tophash}"


def subject(report: Report) -> str:
    """One line that says what happened without being opened."""
    defects = sum(1 for f in report.findings if f.severity == DEFECT)
    kus = sum(1 for f in report.findings if f.severity == KNOWN_UNRESOLVED)
    if report.error:
        head = "engine error"
    elif defects:
        head = f"{defects} defect{'s' if defects != 1 else ''}"
    elif kus:
        head = f"{kus} known-unresolved"
    else:
        head = "pass"
    return f"[check-commit] {head} — {report.package} @ {report.tophash[:12]}"


def render(report: Report, pol: Policy, template_path: pathlib.Path | None = None) -> str:
    """Deterministic: same report + template in, same text out."""
    import jinja2

    path = template_path or (TEMPLATE_DIR / f"{pol.prefix}-notify.md")
    try:
        source = path.read_text()
    except FileNotFoundError:
        # A missing template must not cost the notification. Falling back to the
        # JSON keeps the alert flowing and makes the omission visible.
        return f"(no notify template at {path})\n\n{report.to_json()}"
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    findings = [f.to_dict() for f in report.sorted_findings()]
    return env.from_string(source).render(
        report=report.to_dict(),
        defects=[f for f in findings if f["severity"] == DEFECT],
        kus=[f for f in findings if f["severity"] == KNOWN_UNRESOLVED],
        catalog_url=catalog_url(report),
        policy=dataclasses.asdict(pol) | {"prefix": pol.prefix},
    )
