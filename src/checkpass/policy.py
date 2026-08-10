"""Package-specific policy for occurrence/probability.

Everything here is derived from the governed package's own record — its README,
its closed issues, and the conventions its folders actually follow. Citations
are given per item so a reader can audit the policy against the package.
"""

from __future__ import annotations

import re

# --- check 2: size watchlist -------------------------------------------------
# Per the package README: 01-backstory/01P–10P and 01-backstory-summary.md,
# 02-measure-selection/02-results-summary.md, 03-conditional-kernel/03.01P.
WATCHLIST_PATTERNS = [
    re.compile(r"^01-backstory/(0[1-9]|10)P-"),
    re.compile(r"^01-backstory/01-backstory-summary\.md$"),
    re.compile(r"^02-measure-selection/02-results-summary\.md$"),
    re.compile(r"^03-conditional-kernel/03\.01P-"),
]

# A size decrease on a watchlisted artifact counts as declared only if the
# revision's delta/message acknowledges a reduction in one of these stems.
DECREASE_MARKERS = (
    "compress",
    "shrink",
    "trim",
    "condens",
    "shorten",
    "reduc",
    "truncat",
    "prune",
    "strik",
)


def is_watchlisted(path: str) -> bool:
    return any(p.search(path) for p in WATCHLIST_PATTERNS)


# --- check 1 / check 6: structured metadata fields ---------------------------
# Revision user_meta fields whose values name files the patch claims to touch.
STRUCTURED_FILE_FIELDS = (
    "adds",
    "deletes",
    "changes",
    "messages_added",
    "renames",
    "updates",
)

# Which part of the actual diff each structured claim must land in.
FIELD_TO_DIFF = {
    "adds": "added",
    "messages_added": "added",
    "deletes": "removed",
    "changes": "changed",
    "renames": "any",
    "updates": "any",
}

# --- check 3: message-file naming --------------------------------------------
# Message folders are NN-slug at the package root. Inside them, the anaimail
# Structure §3 form is PARENT.NNL-title-slug.md. 01-backstory predates the rule
# and is recorded in the package README as a deviation that is not retrofitted
# (revision f6c34d02); bare NNL is accepted there and only there.
MESSAGE_FOLDER_RE = re.compile(r"^(\d{2})-[\w-]+$")
GRANDFATHERED_BARE_FOLDERS = {"01-backstory"}

DOTTED_MESSAGE_RE = re.compile(r"^(\d{2})\.(\d{2})([A-Z]{1,2})-.+\.md$")
BARE_MESSAGE_RE = re.compile(r"^(\d{2})([A-Z]{1,2})-.+\.md$")

# Counter collisions adjudicated by the package as a known-unresolved spec
# condition rather than a defect: both pairs were written against the same
# head and neither party is recorded as at fault (issues/closed/030).
ADJUDICATED_COLLISIONS = {
    ("02-measure-selection", "21"): frozenset({"K", "P"}),
    ("03-conditional-kernel", "05"): frozenset({"M", "P"}),
}
ADJUDICATION_CITE = "issues/closed/030"

# --- check 4: issue paths -----------------------------------------------------
OPEN_ISSUE_RE = re.compile(r"^issues/(\d{3})-[^/]+$")
CLOSED_ISSUE_RE = re.compile(r"^issues/closed/(\d{3})-[^/]+$")

# --- check 5: quilt+s3 URIs ----------------------------------------------------
QUILT_URI_RE = re.compile(r"quilt\+s3://[^\s\)\]\"'`<>]+")
